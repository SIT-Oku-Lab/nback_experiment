"""Input regression tests using the real recorder and deterministic scheduler."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from engine import Config, make_sequence
from app import App
import test_experiment


class ControlCanvas(test_experiment.FakeCanvas):
    def __init__(self):
        self.grabbed = False
        self.width, self.height = 1024, 768
        self.drawn = []

    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def winfo_rootx(self): return 100
    def winfo_rooty(self): return 50
    def grab_set(self): self.grabbed = True
    def grab_current(self): return self if self.grabbed else None
    def grab_release(self): self.grabbed = False
    def delete(self, *args): self.drawn.clear()
    def create_rectangle(self, *args, **kwargs): self.drawn.append((args, kwargs)); return len(self.drawn)
    def create_text(self, *args, **kwargs): self.drawn.append((args, kwargs)); return len(self.drawn)
    def create_line(self, *args, **kwargs): return None


class ControlRoot:
    def __init__(self):
        self.callbacks = {}
        self.next_id = 0
        self.focused = True
        self.scaling = 1.33
        self.tk = types.SimpleNamespace(call=lambda *args: self.scaling)

    def after(self, ms, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = (ms, callback)
        return self.next_id

    def after_cancel(self, timer): self.callbacks.pop(timer, None)
    def update_idletasks(self): pass
    def winfo_screenwidth(self): return 1024
    def winfo_screenheight(self): return 768
    def focus_displayof(self): return self if self.focused else None
    def attributes(self, *args): pass
    def configure(self, *args, **kwargs): pass


class InputTests(unittest.TestCase):
    def make_app(self, folder, n=0):
        app = test_experiment.SchedulerTests().make_app(folder, n)
        app.config = replace(app.config, input_mode="touch")
        app.canvas = ControlCanvas()
        app.root = ControlRoot()
        app.pointer_down = False
        app.pointer_action = None
        app.abort_timer = None
        app.response_feedback_timer = None
        app.response_feedback_until = None
        app.focus_timer = None
        app.abort_hold_start = None
        app.timer = None
        app.session_display = app.display_signature()
        app.render_touch_controls()
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

    def press(self, app, ms, x=512, y=700):
        with patch("app.time.perf_counter_ns", return_value=app.block_start + ms * 1_000_000):
            app.on_pointer_press(types.SimpleNamespace(x=x, y=y, time=ms))

    def test_pointer_press_records_before_release_and_requires_fresh_contact(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.press(app, 100)
            self.assertTrue(app.canvas.grabbed)
            self.assertEqual(app.current["response"], app.block_start + 100_000_000)
            self.press(app, 3100)
            self.assertIsNone(app.current["response"])
            # A release delivered outside the application still clears the latch.
            app.on_pointer_release(types.SimpleNamespace(x=-100, y=-100))
            self.assertFalse(app.canvas.grabbed)
            self.press(app, 3200)
            self.assertEqual(app.current["response"], app.block_start + 3_200_000_000)
            app.rec.finish("aborted")

    def test_slide_into_response_and_repeat_after_outside_press_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.press(app, 100, x=100, y=100)
            app.on_pointer_motion(types.SimpleNamespace(x=512, y=700))
            self.press(app, 150)
            self.assertIsNone(app.current["response"])
            app.on_pointer_release()
            self.press(app, 200)
            self.assertIsNotNone(app.current["response"])
            app.rec.finish("aborted")

    def test_instruction_touch_does_not_leak_into_task_or_accept_enter(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            app.phase = "ready"
            app.render_touch_controls()
            app.on_enter()
            self.assertEqual(app.phase, "ready")
            self.press(app, 100)
            self.assertEqual(app.phase, "prepare")
            self.assertNotIn("response", app.touch_regions)
            # Keep the contact through preparation and the first task stimulus.
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 6_000_000_000):
                app.begin_block()
            self.press(app, 100)
            self.assertIsNone(app.current["response"])
            app.on_pointer_release()
            self.press(app, 200)
            self.assertIsNotNone(app.current["response"])
            app.rec.finish("aborted")

    def test_touch_layout_phases_minimum_screen_and_nonoverlap(self):
        self.assertIsNone(App.touch_display_error(1024, 768))
        for size in ((768, 1024), (1024, 600), (1000, 768), (1080, 1080)):
            self.assertIsNotNone(App.touch_display_error(*size))
        for width, height in ((1024, 768), (1280, 800), (1920, 1200)):
            for phase in ("ready", "prepare", "baseline", "task", "rest"):
                regions = App.touch_layout(width, height, phase)
                self.assertIn("abort", regions)
                self.assertEqual("response" in regions, phase == "task")
                self.assertEqual("start" in regions, phase == "ready")
                if phase == "task":
                    l, t, r, b = regions["response"]
                    self.assertEqual((r - l, b - t, height - b), (320, 100, 24))
                    self.assertGreater(t, height / 2 + 180)

    def use_real_finish(self, app):
        app.finish = types.MethodType(App.finish, app)
        app.show_results = lambda *args: None

    def test_abort_hold_requires_1500ms_and_saves_partial_trial(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.use_real_finish(app)
            self.press(app, 100, x=900, y=50)
            self.advance(app, 1000)
            self.assertTrue(app.active)
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 1_599_000_000):
                app.complete_abort_hold()
            self.assertTrue(app.active)
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 1_600_000_000):
                app.complete_abort_hold()
            self.assertFalse(app.active)
            self.assertEqual(app.rec.metadata["reason"], "touch_hold")
            self.assertEqual(app.rec.rows[0]["status"], "aborted")
            self.assertFalse(app.canvas.grabbed)
            self.assertIsNone(app.abort_timer)

    def test_abort_hold_cancels_on_release_or_leaving_area(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            for cancel in (lambda: app.on_pointer_release(),
                           lambda: app.on_pointer_motion(types.SimpleNamespace(x=512, y=700))):
                self.press(app, 100, x=900, y=50)
                timer = app.abort_timer
                cancel()
                self.assertNotIn(timer, app.root.callbacks)
                self.assertIsNone(app.abort_hold_start)
                with patch("app.time.perf_counter_ns", return_value=app.block_start + 2_000_000_000):
                    app.complete_abort_hold()
                self.assertTrue(app.active)
                app.on_pointer_release()
            app.rec.finish("aborted")

    def test_focus_loss_and_display_change_abort_and_preserve_recording(self):
        for change in ("focus", "size", "scaling"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                app = self.make_app(folder)
                self.use_real_finish(app)
                if change == "focus":
                    app.root.focused = False
                    app.check_focus()
                else:
                    if change == "size":
                        app.canvas.width = 768
                    else:
                        app.root.scaling = 2
                    app.on_display_configure(types.SimpleNamespace())
                    self.assertTrue(app.active)  # Saving is deferred out of redraw callbacks.
                    app.check_display_changed()
                self.assertFalse(app.active)
                self.assertEqual(app.rec.metadata["reason"], "focus_lost" if change == "focus" else "display_changed")
                self.assertEqual(app.rec.rows[0]["status"], "aborted")

    def test_root_coordinates_and_keyboard_mode_ignore_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            with patch("app.time.perf_counter_ns", return_value=app.block_start + 100_000_000):
                app.on_pointer_press(types.SimpleNamespace(x_root=612, y_root=750, time=100))
            detail = json.loads(self.events(app)[-1]["detail_json"])
            self.assertEqual((detail["x"], detail["y"]), (512, 700))
            app.on_pointer_release()
            self.advance(app, 3000)
            app.config = replace(app.config, input_mode="keyboard")
            self.press(app, 3100)
            self.assertIsNone(app.current["response"])
            app.rec.finish("aborted")

    def test_non_task_phases_have_no_response_and_ignore_contacts(self):
        for phase in ("prepare", "baseline", "rest"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                app = self.make_app(folder)
                app.phase = phase
                app.phase_end = app.block_start + 10_000_000_000
                app.render_touch_controls()
                self.press(app, 100)
                self.assertIsNone(app.current["response"])
                app.on_pointer_release()
                app.rec.finish("aborted")

    def test_touch_repeats_release_and_all_task_durations(self):
        for mode in ("experiment", "practice", "demo"):
            for n in (0, 2):
                with self.subTest(mode=mode, n=n), tempfile.TemporaryDirectory() as folder:
                    app = self.make_app(folder, n)
                    app.config = replace(app.config, mode=mode)
                    app.sequences = [make_sequence(app.config, n, 0)]
                    # Start a fresh deterministic block with the selected duration.
                    app.current = None
                    app.current_index = -1
                    app.presented = set()
                    app.block_end = app.block_start + app.config.duration_ms * 1_000_000
                    app.render_touch_controls()
                    for ms in range(0, app.config.duration_ms, app.config.interval_ms):
                        self.advance(app, ms)
                        self.press(app, ms + 100)
                        self.press(app, ms + 150)  # Duplicate press without release.
                        app.on_pointer_release()
                    self.advance(app, app.config.duration_ms)
                    self.assertEqual(app.rec.metadata["status"], "completed")
                    self.assertEqual(len(app.rec.rows), app.config.duration_ms // app.config.interval_ms)
                    self.assertEqual(sum(r["responded"] for r in app.rec.rows), len(app.rec.rows) - n)
                    self.assertTrue(all(r["rt_ms"] == 100 for r in app.rec.rows if r["responded"]))


if __name__ == "__main__":
    unittest.main()
