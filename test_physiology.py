import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import queue
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from engine import Config
from physiology import ClockCorrection, PhysiologyCapture, Sample, SegmentWriter
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


class ClockCorrectionTests(unittest.TestCase):
    def test_offset_updates_do_not_reverse_microsecond_spaced_samples(self):
        correction = ClockCorrection(0)
        stamps = [100, 100.000004, 100.000008, 100.000012]
        measured = [0, -.000025, -.000025, .000025]
        mapped = [correction.apply(remote, offset) for remote, offset in zip(stamps, measured)]
        self.assertTrue(all(b > a for a, b in zip(mapped, mapped[1:])))
        for a, b, raw_a, raw_b in zip(mapped, mapped[1:], stamps, stamps[1:]):
            self.assertAlmostEqual((b - a) / (raw_b - raw_a), 1, delta=.010001)

    def test_small_update_converges_and_keeps_real_source_gaps(self):
        for target in (-.0002, .0002):
            correction = ClockCorrection(0)
            first = correction.apply(100, 0)
            second = correction.apply(100.01, target)
            self.assertAlmostEqual(correction.applied_s, target / 2)
            third = correction.apply(100.02, target)
            self.assertAlmostEqual(correction.applied_s, target)
            fourth = correction.apply(100.52, target)
            self.assertAlmostEqual(fourth - third, .5)
            self.assertGreater(second, first)

    def test_invalid_source_stamps_and_clock_discontinuities_still_fail(self):
        for remote, measured in ((100, 0), (99, 0), (math.nan, 0),
                                 (math.inf, 0), (101, math.nan), (101, math.inf),
                                 (101, .101), (101, -.101)):
            with self.subTest(remote=remote, measured=measured):
                correction = ClockCorrection(0)
                correction.apply(100, 0)
                with self.assertRaises(ValueError):
                    correction.apply(remote, measured)
        with self.assertRaises(ValueError):
            ClockCorrection(math.nan).apply(100, 0)


class CaptureTests(unittest.TestCase):
    def test_acquisition_saves_burst_across_negative_clock_offset_update(self):
        # Reproduce OpenSignals: consecutive source stamps 4 us apart and a
        # -25 us offset refresh. Direct replacement reverses the second stamp.
        with tempfile.TemporaryDirectory() as folder:
            capture = PhysiologyCapture.__new__(PhysiologyCapture)
            capture.parent, capture.clock = Path(folder), FakeClock()
            capture.commands = queue.Queue()
            capture.ready, capture.done, capture.cancel = (threading.Event() for _ in range(3))
            capture.error, capture.last_arrival_ns = "", None
            capture.stream = {"nominal_srate": 1000, "channel_count": 2}
            state = types.SimpleNamespace(elapsed=0, pulls=0)
            channel = types.SimpleNamespace(child_value=lambda key: "", next_sibling=lambda: channel)
            channel.child = lambda key: channel
            capture.info = types.SimpleNamespace(desc=lambda: channel, channel_count=lambda: 2,
                                                 as_xml=lambda: "<info/>")

            class Inlet:
                def open_stream(self, **kwargs):
                    pass

                def info(self, **kwargs):
                    return capture.info

                def time_correction(self, **kwargs):
                    return -.000025 if state.pulls >= 2 else 0

                def pull_chunk(self, **kwargs):
                    state.pulls += 1
                    state.elapsed += 1.01
                    if state.pulls == 2:
                        capture.commands.put(("finish", (FakeClock.perf_ns + 800_000_000,)))
                    return [[state.pulls, -state.pulls]], [100.8 + (state.pulls - 1) * .000004]

                def close_stream(self):
                    pass

            lsl = types.SimpleNamespace(StreamInlet=lambda *args, **kwargs: Inlet(), local_clock=lambda: 100)
            clock = types.SimpleNamespace(monotonic=lambda: state.elapsed,
                                          perf_counter_ns=lambda: FakeClock.perf_ns + round(state.elapsed * 1e9))
            with patch.dict("sys.modules", pylsl=lsl), patch("physiology.time", clock):
                capture.run()
            self.assertTrue(capture.done.is_set())
            self.assertEqual(capture.error, "")
            with (Path(folder) / "physiology/continuous.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual([float(row["lsl_remote_s"]) for row in rows], [100.8, 100.800004])
            self.assertEqual([row["ch_1"] for row in rows], ["1", "2"])
            self.assertGreater(float(rows[1]["session_ms"]), float(rows[0]["session_ms"]))
            self.assertAlmostEqual(float(rows[1]["lsl_measured_correction_s"]), -.000025)
            self.assertGreater(float(rows[1]["clock_correction_s"]), -.000025)
            for row in rows:
                self.assertAlmostEqual(float(row["lsl_local_s"]),
                                       float(row["lsl_remote_s"]) + float(row["clock_correction_s"]))
            summary = json.loads((Path(folder) / "physiology/summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["samples"], 2)
            self.assertEqual(summary["error"], "")

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
