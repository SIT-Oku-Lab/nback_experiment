"""Input regression tests using the real recorder and deterministic scheduler."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from engine import Config
import test_experiment


class InputTests(unittest.TestCase):
    def make_app(self, folder, n=0):
        app = test_experiment.SchedulerTests().make_app(folder, n)
        app.config = replace(app.config, input_mode="touch")
        # The real session receives this Config before creating its Recorder.
        app.rec.metadata["config"] = app.config.metadata()
        app.rec.save_metadata()
        self.addCleanup(lambda: app.rec.finish("aborted", "test_cleanup"))
        self.advance(app, 0)
        return app

    def advance(self, app, ms):
        test_experiment.SchedulerTests().advance(app, ms)

    def respond(self, app, ms, source="touch", repeated=False):
        at = app.block_start + ms * 1_000_000
        with patch("app.time.perf_counter_ns", return_value=at):
            app.respond(source, types.SimpleNamespace(x=512, y=700, time=ms), at, repeated)

    def events(self, app):
        return [json.loads(line) for line in (app.rec.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_input_config_default_validation_and_metadata(self):
        self.assertEqual(Config().input_mode, "keyboard")
        for mode in ("touch", "keyboard"):
            cfg = Config(input_mode=mode)
            cfg.validate()
            self.assertEqual(cfg.metadata()["input_mode"], mode)
        with self.assertRaises(ValueError):
            Config(input_mode="invalid").validate()

    def test_touch_timestamp_and_event_details_preserve_trial_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.respond(app, 150)
            self.assertEqual(app.current["response"], app.block_start + 150_000_000)
            entry = self.events(app)[-1]
            detail = json.loads(entry["detail_json"])
            self.assertEqual(detail["input_mode"], "touch")
            self.assertEqual((detail["x"], detail["y"], detail["tk_event_time_ms"]), (512, 700, 150))
            self.assertNotIn("key", detail)
            self.advance(app, 3000)
            self.assertEqual(app.rec.rows[0]["rt_ms"], 150)
            metadata = json.loads((app.rec.path / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["config"]["input_mode"], "touch")
            app.rec.finish("aborted")

    def test_wrong_input_mode_and_double_response_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.respond(app, 100, source="keyboard")
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 100_000_000):
                app.on_space(types.SimpleNamespace(time=100))
            self.assertIsNone(app.current["response"])
            self.respond(app, 150)
            self.respond(app, 200)
            self.assertEqual(app.current["response"], app.block_start + 150_000_000)
            self.assertEqual(json.loads(self.events(app)[-1]["detail_json"])["reason"], "already_responded")
            app.config = replace(app.config, input_mode="keyboard")
            self.advance(app, 3000)
            self.respond(app, 3100)
            self.assertIsNone(app.current["response"])
            app.rec.finish("aborted")

    def test_warmup_and_held_contact_crossing_trial_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder, n=2)
            for ms in (100, 3100):
                self.respond(app, ms)
                self.assertIsNone(app.current["response"])
                self.assertEqual(json.loads(self.events(app)[-1]["detail_json"])["reason"], "warmup")
            self.respond(app, 6100, repeated=True)
            self.assertIsNone(app.current["response"])
            self.respond(app, 6200)
            self.assertIsNotNone(app.current["response"])
            app.rec.finish("aborted")

    def test_exact_deadline_does_not_apply_response_to_previous_trial(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.respond(app, 3000)
            self.assertEqual(app.rec.rows[0]["responded"], 0)
            self.assertEqual(app.current["trial"].index, 1)
            self.assertEqual(app.current["response"], app.block_start + 3_000_000_000)
            app.rec.finish("aborted")


if __name__ == "__main__":
    unittest.main()
