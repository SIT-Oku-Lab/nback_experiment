import csv
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from engine import Config
from physiology import PhysiologyCapture, Sample, SegmentWriter
from storage import Recorder


class FakeClock:
    perf_ns = 10_000_000_000

    def epoch(self, perf_ns):
        return 1_700_000_000_000_000_000 + perf_ns - self.perf_ns


class WriterTests(unittest.TestCase):
    def writer(self, folder):
        return SegmentWriter(Path(folder), FakeClock(), {"nominal_srate": 100, "channel_count": 2}, settle_s=1)

    def sample(self, index, ms):
        return Sample(index, FakeClock.perf_ns + round(ms * 1e6), 100 + ms / 1000, .01,
                      100.01 + ms / 1000, [index, -index])

    def trial(self, number, start, end, status="completed", closed=None):
        return {"block": 1, "n": 0, "trial": number, "status": status,
                "planned_onset_session_ms": start, "onset_session_ms": start + 2,
                "response_deadline_session_ms": end, "closed_session_ms": end if closed is None else closed}

    def rows(self, writer, filename):
        with (writer.path / filename).open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_trial_boundaries_are_half_open_and_block_joins_exactly(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            writer.start_block(1, 0, FakeClock.perf_ns, FakeClock.perf_ns + 2_000_000_000)
            writer.trial(self.trial(1, 0, 1000))
            writer.trial(self.trial(2, 1000, 2000))
            for index in range(201):
                writer.add(self.sample(index + 1, index * 10))
            writer.end_block(1, FakeClock.perf_ns + 2_000_000_000, "completed")
            writer.finalize_ready()
            first = self.rows(writer, "trials/block_01_trial_0001.csv")
            second = self.rows(writer, "trials/block_01_trial_0002.csv")
            block = self.rows(writer, "blocks/block_01_0back.csv")
            self.assertEqual((len(first), len(second), len(block)), (100, 100, 200))
            self.assertEqual(first[-1]["session_ms"], "990.0")
            self.assertEqual(second[0]["session_ms"], "1000.0")
            self.assertEqual(set(r["sample_index"] for r in first) & set(r["sample_index"] for r in second), set())
            self.assertEqual([r["sample_index"] for r in first + second], [r["sample_index"] for r in block])
            self.assertEqual(first[0]["relative_stimulus_ms"], "-2.0")
            self.assertTrue(all(r["quality"] == "complete" for r in writer.saved_segments))
            writer.close(FakeClock.perf_ns + 2_000_000_000)

    def test_save_waits_for_late_samples_and_is_available_before_session_end(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            for index in range(80):
                writer.add(self.sample(index + 1, index * 10))
            writer.trial(self.trial(1, 0, 1000))
            writer.finalize_ready()
            self.assertFalse((writer.path / "trials/block_01_trial_0001.csv").exists())
            for index in range(80, 101):
                writer.add(self.sample(index + 1, index * 10))
            writer.finalize_ready()
            self.assertEqual(len(self.rows(writer, "trials/block_01_trial_0001.csv")), 100)
            self.assertEqual(len(self.rows(writer, "segments.csv")), 1)
            writer.close(FakeClock.perf_ns + 1_000_000_000)

    def test_abort_truncates_trial_and_already_written_block_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            writer.start_block(1, 0, FakeClock.perf_ns, FakeClock.perf_ns + 3_000_000_000)
            for index in range(51):
                writer.add(self.sample(index + 1, index * 10))
            writer.trial(self.trial(1, 0, 3000, status="aborted", closed=450))
            writer.end_block(1, FakeClock.perf_ns + 450_000_000, "aborted")
            writer.finalize_ready()
            self.assertEqual(len(self.rows(writer, "trials/block_01_trial_0001.csv")), 45)
            self.assertEqual(len(self.rows(writer, "blocks/block_01_0back.csv")), 45)
            self.assertTrue(all(r["status"] == "aborted" for r in writer.saved_segments))
            writer.close(FakeClock.perf_ns + 450_000_000)

    def test_gap_and_timeout_are_flagged_as_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            for index, ms in enumerate((0, 10, 20, 700, 710)):
                writer.add(self.sample(index + 1, ms))
            writer.trial(self.trial(1, 0, 1000))
            writer.close(FakeClock.perf_ns + 1_000_000_000)
            record = writer.saved_segments[0]
            self.assertEqual(record["samples"], 5)
            self.assertEqual(record["gap_count"], 1)
            self.assertEqual(record["quality"], "incomplete")
            self.assertTrue(record["finalized_after_timeout"])

    def test_late_block_start_backfills_buffer(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            for index in range(101):
                writer.add(self.sample(index + 1, index * 10))
            writer.start_block(1, 0, FakeClock.perf_ns, FakeClock.perf_ns + 1_000_000_000)
            writer.end_block(1, FakeClock.perf_ns + 1_000_000_000, "completed")
            writer.finalize_ready()
            self.assertEqual(len(self.rows(writer, "blocks/block_01_0back.csv")), 100)
            writer.close(FakeClock.perf_ns + 1_000_000_000)

    def test_buffer_is_bounded_and_nonmonotonic_timestamps_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = self.writer(folder)
            for index in range(4000):
                writer.add(self.sample(index + 1, index * 10))
            self.assertLessEqual(len(writer.buffer), 3001)
            with self.assertRaises(ValueError):
                writer.add(self.sample(4001, 10))
            writer.close(FakeClock.perf_ns + 40_000_000_000)


class CaptureTests(unittest.TestCase):
    def test_stale_signal_is_an_error(self):
        capture = PhysiologyCapture.__new__(PhysiologyCapture)
        capture.error = ""
        capture.last_arrival_ns = 1_000_000_000
        with patch("physiology.time.perf_counter_ns", return_value=3_100_000_000):
            self.assertIn("2秒", capture.check_health())

    def test_signal_choice_required(self):
        with self.assertRaises(ValueError):
            replace(Config(), physiology=True).validate()

    def test_recorder_routes_trial_and_block_boundaries_without_sampling_on_gui(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = replace(Config(), physiology=True, physiology_uid="chosen")
            info = types.SimpleNamespace(uid=lambda: "chosen")
            with patch("physiology.PhysiologyCapture") as capture_type:
                capture = capture_type.return_value
                capture.stream = {"name": "OpenSignals"}
                capture.finish.return_value = {"error": "", "finished": True}
                rec = Recorder(Path(folder), cfg, info)
                start = rec.clock.perf_ns
                rec.event("block_start", start, block=1, n=0, planned_duration_ms=600000)
                capture.block_start.assert_called_once_with(1, 0, start, start + 600_000_000_000)
                rec.trial({"block": 1, "n": 0, "status": "aborted", "eligible": 1})
                capture.trial.assert_called_once()
                rec.event("block_end", start + 600_000_000_000, block=1, planned_end_session_ms=600000)
                capture.block_end.assert_called_once_with(1, start + 600_000_000_000, "completed")
                rec.finish("completed")
                capture.finish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
