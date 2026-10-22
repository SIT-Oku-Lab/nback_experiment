"""Run: python -m unittest -v. Simulated clocks do not require a 20-minute wait."""
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from engine import Config, INTERVALS, Trial, accepts_response, classify, make_sequence, summarize
from storage import Clock, Recorder
from app import App


class SequenceTests(unittest.TestCase):
    def test_all_intervals_both_conditions_last_exactly_600_seconds(self):
        for interval in INTERVALS:
            for n in (0, 2):
                cfg = Config(interval_ms=interval)
                seq = make_sequence(cfg, n, 0)
                self.assertEqual(len(seq) * interval, 600_000)
                self.assertEqual(sum(t.eligible for t in seq), len(seq) - n)

    def test_exact_targets_and_no_accidental_matches_many_seeds(self):
        for seed in range(100):
            for n in (0, 2):
                cfg = Config(seed=seed, zero_target=seed % 9)
                seq = make_sequence(cfg, n, 0)
                expected = math.floor((len(seq) - n) * .30 + .5)
                self.assertEqual(sum(t.target for t in seq), expected)
                for trial in seq:
                    self.assertIn(trial.position, range(9))
                    if trial.eligible:
                        comparison = cfg.zero_target if n == 0 else seq[trial.index - n].position
                        self.assertEqual(trial.target, trial.position == comparison)

    def test_seed_reproduces_and_blocks_have_independent_randomness(self):
        cfg = Config(seed=123)
        self.assertEqual(make_sequence(cfg, 2, 0), make_sequence(cfg, 2, 0))
        self.assertNotEqual(make_sequence(cfg, 2, 0), make_sequence(cfg, 2, 1))

    def test_first_two_trials_never_scored(self):
        seq = make_sequence(Config(), 2, 0)
        for trial in seq[:2]:
            self.assertFalse(trial.eligible)
            self.assertFalse(trial.target)
            self.assertEqual(classify(trial, True), "warmup")

    def test_outcomes(self):
        target = Trial(3, 4, True, True)
        other = Trial(4, 1, True, False)
        self.assertEqual(classify(target, True), "hit")
        self.assertEqual(classify(target, False), "miss")
        self.assertEqual(classify(other, True), "false_alarm")
        self.assertEqual(classify(other, False), "correct_rejection")

    def test_response_window_and_single_response(self):
        trial = Trial(3, 4, True, True)
        self.assertTrue(accepts_response(trial, 100, 300, 100, False))
        self.assertTrue(accepts_response(trial, 100, 300, 299, False))
        self.assertFalse(accepts_response(trial, 100, 300, 300, False))
        self.assertFalse(accepts_response(trial, 100, 300, 99, False))
        self.assertFalse(accepts_response(trial, 100, 300, 150, True))
        self.assertFalse(accepts_response(Trial(0, 4, False, False), 100, 300, 150, False))

    def test_invalid_configurations(self):
        cfg = Config()
        for changes in ({"interval_ms": 0}, {"interval_ms": 3333}, {"stimulus_ms": 3000},
                        {"stimulus_ms": 49}, {"baseline_s": -1}, {"rest_s": 601},
                        {"participant": ""}, {"seed": -1}, {"zero_target": 9}, {"target_percent": 9}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(cfg, **changes).validate()

    def test_summary_excludes_incomplete_missing_history_and_warmup(self):
        rows = [{"status": "completed", "eligible": 1, "outcome": "hit", "rt_ms": 350},
                {"status": "completed", "eligible": 1, "outcome": "correct_rejection", "rt_ms": ""},
                {"status": "completed", "eligible": 0, "outcome": "warmup", "rt_ms": ""},
                {"status": "aborted", "eligible": 1, "outcome": "aborted", "rt_ms": ""},
                {"status": "skipped", "eligible": 1, "outcome": "skipped", "rt_ms": ""},
                {"status": "invalid_history", "eligible": 1, "outcome": "invalid_history", "rt_ms": ""}]
        result = summarize(rows)
        self.assertEqual(result["scored_trials"], 2)
        self.assertEqual(result["accuracy"], 1)
        self.assertEqual(result["mean_hit_rt_ms"], 350)
        self.assertEqual(result["invalid_history_trials"], 1)


class RecorderTests(unittest.TestCase):
    def test_journal_is_readable_before_normal_shutdown_and_finish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            rec = Recorder(Path(folder), Config(participant="実験/../P001"))
            self.assertEqual(rec.path.parent, Path(folder))
            rec.event("manual_marker", note="日本語")
            entries = [json.loads(line) for line in (rec.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(entries[-1]["event"], "manual_marker")
            rec.finish("aborted", "test")
            rec.finish("aborted", "second_call")
            metadata = json.loads((rec.path / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["reason"], "test")
            with (rec.path / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 3)

    def test_epoch_mapping_is_independent_of_later_wall_clock_changes(self):
        clock = Clock()
        with patch("storage.time.time_ns", return_value=0):
            self.assertEqual(clock.epoch(clock.perf_ns + 2_000_000_000), clock.epoch_ns + 2_000_000_000)


class FakeCanvas:
    def itemconfigure(self, *args, **kwargs):
        pass


class SchedulerTests(unittest.TestCase):
    def make_app(self, folder, n=0):
        app = App.__new__(App)
        app.config = Config(conditions=(n,), baseline_s=0, rest_s=0)
        app.rec = Recorder(Path(folder), app.config)
        app.sequences = [make_sequence(app.config, n, 0)]
        app.active, app.phase, app.block_index = True, "task", 0
        app.current_index, app.current, app.presented = -1, None, set()
        app.block_start = app.rec.clock.perf_ns
        app.block_end = app.block_start + 600_000_000_000
        app.canvas = FakeCanvas()
        app.cells = list(range(9))
        app.root = types.SimpleNamespace(update_idletasks=lambda: None)
        app.finish = lambda status: (setattr(app, "active", False), app.rec.finish(status))
        app.key_down = False
        return app

    def advance(self, app, elapsed_ms):
        now = app.block_start + round(elapsed_ms * 1_000_000)
        with patch("app.time.perf_counter_ns", return_value=now):
            app.advance(now)

    def test_full_600_second_scheduler_both_conditions(self):
        for n in (0, 2):
            with tempfile.TemporaryDirectory() as folder:
                app = self.make_app(folder, n)
                for index in range(200):
                    self.advance(app, index * 3000)
                    self.advance(app, index * 3000 + 500)
                self.assertTrue(app.active)
                self.advance(app, 599999)
                self.assertTrue(app.active)
                self.advance(app, 600000)
                self.assertFalse(app.active)
                self.assertEqual(len(app.rec.rows), 200)
                self.assertTrue(all(r["status"] == "completed" for r in app.rec.rows))
                events = [json.loads(s) for s in (app.rec.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
                end = next(e for e in events if e["event"] == "block_end")
                self.assertEqual(json.loads(end["detail_json"])["actual_duration_ms"], 600000)

    def test_delayed_callbacks_skip_stale_stimuli_and_invalidate_missing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder, 2)
            self.advance(app, 0)
            self.advance(app, 9500)  # trial 1 closes; 2, 3, and 4 cannot be shown
            self.advance(app, 12000)
            self.advance(app, 15000)
            self.assertEqual([r["status"] for r in app.rec.rows],
                             ["completed", "skipped", "skipped", "skipped", "invalid_history"])
            app.rec.finish("aborted")

    def test_held_space_does_not_count_again_after_next_trial(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.advance(app, 0)
            for ms in (100, 200, 3100):
                with patch("app.time.perf_counter_ns", return_value=app.block_start + ms * 1_000_000):
                    app.on_space(types.SimpleNamespace(time=ms))
            self.assertIsNone(app.current["response"])
            app.on_release()
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 3200 * 1_000_000):
                app.on_space(types.SimpleNamespace(time=3200))
            self.assertIsNotNone(app.current["response"])
            app.rec.finish("aborted")

    def test_abort_preserves_partial_trial_without_scoring_it(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.advance(app, 0)
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 100_000_000):
                app.close_trial("aborted", app.block_start + 100_000_000)
            self.assertEqual(app.rec.rows[0]["status"], "aborted")
            self.assertEqual(summarize(app.rec.rows)["scored_trials"], 0)
            app.rec.finish("aborted")


if __name__ == "__main__":
    unittest.main()
