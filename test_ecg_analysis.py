import csv
import bisect
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
import types

from ecg_analysis import analyze_recording, metrics, screen_rr, select_channel


STREAM = {"channel_count": 2, "nominal_srate": 250,
          "channels": [{"column": "ch_1", "label": "nSeq"}, {"column": "ch_2", "label": "ECGBIT0"}]}


def fixture(folder, *, seconds=70, polarity=1, corrupt=False, burst=False, variable=False):
    folder = Path(folder)
    (folder / "physiology").mkdir()
    positions = [.6]
    while positions[-1] < seconds+1:
        positions.append(positions[-1]+(.9 if len(positions) % 2 else 1.1))
    with (folder / "physiology/continuous.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_index", "session_ms", "unix_ns", "ch_1", "ch_2"])
        for i in range(250*seconds):
            t = i/250
            phase = (t-.6+.5) % 1 - .5
            if variable:
                candidate = bisect.bisect_left(positions, t)
                nearest = min(positions[max(0, candidate-1):candidate+1], key=lambda b: abs(b-t))
                phase = t-nearest
            ecg = (math.exp(-.5*(phase/.012)**2) - .20*math.exp(-.5*((phase-.025)/.01)**2)
                   + .25*math.exp(-.5*((phase-.22)/.045)**2)
                   + .02*math.sin(2*math.pi*.3*t) + .003*math.sin(2*math.pi*35*t))
            value = 1024 + polarity*200*ecg
            if corrupt and i == 8000:
                value = float("nan")
            stamp = (i//32)*32/250 + (i % 32)*.000004 if burst else t
            writer.writerow([i+1, stamp*1000, 1700000000000000000+round(stamp*1e9), i % 16, value])
    with (folder / "physiology/segments.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, ["kind", "block", "n", "trial", "start_session_ms", "end_session_ms"])
        writer.writeheader()
        writer.writerows([dict(kind="block", block=1, n=0, trial="", start_session_ms=0, end_session_ms=seconds*1000),
                          dict(kind="trial", block=1, n=0, trial=1, start_session_ms=0, end_session_ms=3000)])
    return folder


class MetricTests(unittest.TestCase):
    def beat_series(self, intervals):
        stamp, beats = 0, []
        for i, rr in enumerate(intervals):
            beats.append(dict(beat=i+1, run=1, previous_session_ms=stamp, session_ms=stamp+rr,
                              rr_ms=rr, timing_verified=True))
            stamp += rr
        return screen_rr(beats)

    def test_analytic_hr_and_hrv_and_short_trial_suppression(self):
        beats = self.beat_series([1000, 1100]*40)
        result = metrics(beats, 0, 84001, kind="block")
        self.assertAlmostEqual(result["mean_hr_bpm"], 60000/1050)
        self.assertAlmostEqual(result["sdnn_ms"], math.sqrt(80*2500/79))
        self.assertAlmostEqual(result["rmssd_ms"], 100)
        self.assertEqual(result["pnn50_percent"], 100)
        short = metrics(beats, 0, 3000, kind="trial")
        self.assertNotEqual(short["mean_hr_bpm"], "")
        self.assertEqual(short["rmssd_ms"], "")
        self.assertEqual(short["hrv_reason"], "duration_below_60s")

    def test_rejected_rr_does_not_create_false_adjacent_pairs(self):
        beats = self.beat_series([1000, 1000, 2500, 1000, 1000])
        result = metrics(beats, 0, 7000, kind="session")
        self.assertEqual(result["rejected_rr"], 1)
        self.assertEqual(result["adjacent_pairs"], 2)
        self.assertEqual(beats[2]["hr_bpm"], "")
        outlier = self.beat_series([1000]*5 + [700] + [1000]*5)
        self.assertEqual(outlier[5]["reason"], "local_rr_outlier")

    def test_discontinuity_and_rate_mismatch_suppress_hrv(self):
        beats = self.beat_series([1000]*70)
        result = metrics(beats, 0, 70001, kind="block", discontinuities=[30000])
        self.assertEqual(result["rmssd_ms"], "")
        self.assertEqual(result["hrv_reason"], "recording_discontinuity")
        beats[0]["timing_verified"] = False
        screen_rr(beats)
        self.assertEqual(beats[0]["reason"], "sampling_rate_mismatch")

    def test_channel_identification_never_guesses_analog_number(self):
        self.assertEqual(select_channel(STREAM), (1, "ecg_label"))
        unknown = {"channel_count": 2, "channels": [{"label": "A1"}, {"label": "A2"}]}
        self.assertEqual(select_channel(unknown)[0], None)
        self.assertEqual(select_channel(unknown, 2)[0], 1)
        self.assertEqual(select_channel(unknown, 3)[0], None)
        self.assertEqual(select_channel(STREAM, -1)[1], "disabled")
        ambiguous = {"channel_count": 2, "channels": [{"label": "ECG1"}, {"label": "ECG2"}]}
        self.assertEqual(select_channel(ambiguous)[1], "ambiguous_ecg_channels")

    def test_missing_ecg_is_recorded_without_fabricating_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "physiology").mkdir()
            report = analyze_recording(Path(directory), {"channel_count": 1, "nominal_srate": 1000})
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reason"], "ecg_label_not_found")
            self.assertTrue((Path(directory) / "physiology/ecg/report.json").exists())

    def test_derived_analysis_failure_does_not_abort_saved_session(self):
        from storage import Recorder
        from engine import Config
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), physiology=True, physiology_uid="ecg")
            info = types.SimpleNamespace(uid=lambda: "ecg")
            with patch("physiology.PhysiologyCapture") as capture_type:
                capture = capture_type.return_value
                capture.stream = STREAM
                capture.finish.return_value = {"error": "", "finished": True}
                rec = Recorder(Path(directory), config, info)
                with patch("ecg_analysis.analyze_recording", side_effect=ImportError("WFDB unavailable")):
                    rec.finish("completed")
                self.assertEqual(rec.metadata["status"], "completed")
                self.assertEqual(rec.metadata["ecg"]["status"], "failed")
                self.assertTrue((rec.path / "trials.csv").exists())
                self.assertEqual(json.loads((rec.path / "physiology/ecg/report.json").read_text())["status"], "failed")


@unittest.skipUnless(importlib.util.find_spec("wfdb"), "Optional WFDB dependency not installed")
class DetectorTests(unittest.TestCase):
    def rows(self, folder, name):
        with (folder / "physiology/ecg" / name).open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_positive_and_inverted_adc_ecg_recover_known_60_bpm(self):
        for polarity in (1, -1):
            with self.subTest(polarity=polarity), tempfile.TemporaryDirectory() as directory:
                folder = fixture(directory, polarity=polarity)
                raw = (folder / "physiology/continuous.csv").read_bytes()
                report = analyze_recording(folder, STREAM)
                self.assertEqual(report["status"], "computed")
                self.assertTrue(68 <= report["beats"] <= 70, report)
                self.assertAlmostEqual(report["session_metrics"]["mean_hr_bpm"], 60, delta=.2)
                self.assertLess(report["session_metrics"]["rmssd_ms"], 3)
                segments = self.rows(folder, "segments_hr_hrv.csv")
                self.assertNotEqual(segments[0]["sdnn_ms"], "")
                self.assertEqual(segments[1]["rmssd_ms"], "")
                self.assertEqual((folder / "physiology/continuous.csv").read_bytes(), raw)
                self.assertEqual(len(self.rows(folder, "rolling_hrv.csv")), 3)

    def test_burst_lsl_stamps_do_not_corrupt_rr_hrv(self):
        with tempfile.TemporaryDirectory() as directory:
            report = analyze_recording(fixture(directory, burst=True), STREAM)
            self.assertAlmostEqual(report["session_metrics"]["mean_hr_bpm"], 60, delta=.2)
            self.assertLess(report["session_metrics"]["rmssd_ms"], 3)

    def test_nan_splits_runs_without_bridging_missing_ecg(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = fixture(directory, corrupt=True)
            report = analyze_recording(folder, STREAM)
            self.assertGreater(report["discontinuities"], 0)
            self.assertEqual(report["session_metrics"]["hrv_reason"], "recording_discontinuity")
            self.assertTrue(all(row["rr_ms"] == "" for row in self.rows(folder, "beats.csv")
                                if row["reason"] == "first_beat_of_run"))

    def test_variable_rr_recovers_nonzero_hrv(self):
        with tempfile.TemporaryDirectory() as directory:
            report = analyze_recording(fixture(directory, variable=True), STREAM)
            self.assertAlmostEqual(report["session_metrics"]["mean_hr_bpm"], 60, delta=.3)
            self.assertAlmostEqual(report["session_metrics"]["rmssd_ms"], 200, delta=6)

    def test_300_second_windows_are_separate_from_60_second_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = fixture(directory, seconds=305)
            analyze_recording(folder, STREAM)
            rolling = self.rows(folder, "rolling_hrv.csv")
            five = [r for r in rolling if r["kind"] == "rolling_300s"]
            self.assertEqual(len(five), 2)  # [0,300) and [5,305)
            self.assertEqual(five[0]["duration_s"], "300.0")
            self.assertNotEqual(five[0]["rmssd_ms"], "")
