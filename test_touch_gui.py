"""Real Tk event-path/layout checks; synthetic clicks are not physical touch tests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import App
from engine import Config
from storage import Recorder


class TkTouchTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.app = App(self.root, demo=True)
        self.assertEqual(self.app.input_selection.get(), "タッチ")
        # Emulate exactly the supported viewport; no physical monitor setting is changed.
        self.root.geometry("1024x768+0+0")
        self.root.update()
        self.enterContext(patch.object(self.root, "attributes", return_value=None))
        self.enterContext(patch.object(self.app, "schedule_tick", return_value=None))
        self.app.config = Config(participant="GUI_SYNTHETIC", input_mode="touch", mode="demo",
                                 interval_ms=600, stimulus_ms=200, baseline_s=0, rest_s=0)
        self.app.rec = Recorder(Path(folder), self.app.config)
        self.addCleanup(self.cleanup_session)
        self.app.enter_session()
        self.root.focus_force()
        self.root.update()

    def cleanup_session(self):
        if self.app.active:
            self.app.abort("test_cleanup")

    def click(self, action, release=True):
        l, t, r, b = self.app.touch_regions[action]
        self.app.canvas.event_generate("<ButtonPress-1>", x=int((l + r) / 2), y=int((t + b) / 2))
        if release and self.app.active:
            self.app.canvas.event_generate("<ButtonRelease-1>", x=0, y=0)

    def test_minimum_viewport_text_and_controls_fit_without_overlap(self):
        c = self.app.canvas
        self.assertEqual((c.winfo_width(), c.winfo_height()), (1024, 768))
        for item in c.find_all():
            x0, y0, x1, y1 = c.bbox(item)
            self.assertGreaterEqual(x0, 0)
            self.assertGreaterEqual(y0, 0)
            self.assertLessEqual(x1, 1024)
            self.assertLessEqual(y1, 768)
        start_top = self.app.touch_regions["start"][1]
        for item in c.find_all():
            if c.type(item) == "text" and "touch-control" not in c.gettags(item):
                self.assertLess(c.bbox(item)[3], start_top)
        self.click("start")
        self.assertEqual(self.app.phase, "prepare")
        self.assertEqual(set(self.app.touch_regions), {"abort"})

    def test_instruction_text_fits_at_multiple_tk_scalings(self):
        old = self.root.tk.call("tk", "scaling")
        self.app.config = replace(self.app.config, baseline_s=30)
        try:
            for scaling in (1.333333, 2, 2.666667):
                self.root.tk.call("tk", "scaling", scaling)
                for block in (0, 1):
                    self.app.block_index = block
                    self.app.show_instruction()
                    for item in self.app.canvas.find_all():
                        bbox = self.app.canvas.bbox(item)
                        self.assertTrue(0 <= bbox[0] < bbox[2] <= 1024)
                        self.assertTrue(0 <= bbox[1] < bbox[3] <= 768)
                        if self.app.canvas.type(item) == "text" and "touch-control" not in self.app.canvas.gettags(item):
                            self.assertLess(bbox[3], self.app.touch_regions["start"][1])
        finally:
            self.root.tk.call("tk", "scaling", old)
            self.app.session_display = self.app.display_signature()

    def test_generated_events_complete_both_demo_blocks_and_save_touch_responses(self):
        app = self.app
        for block in range(2):
            self.assertEqual(app.phase, "ready")
            self.click("start")
            with patch("app.time.perf_counter_ns", return_value=app.phase_end):
                app.advance(app.phase_end)
            self.assertEqual(app.phase, "task")
            for trial in range(20):
                at = app.block_start + trial * 600_000_000
                with patch("app.time.perf_counter_ns", return_value=at):
                    app.advance(at)
                with patch("app.time.perf_counter_ns", return_value=at + 100_000_000):
                    self.click("response")
                with patch("app.time.perf_counter_ns", return_value=at + 200_000_000):
                    app.advance(at + 200_000_000)
            end = app.block_end
            with patch("app.time.perf_counter_ns", return_value=end):
                app.advance(end)
            if block == 0:
                self.assertEqual(app.phase, "rest")
                self.assertNotIn("response", app.touch_regions)
                with patch("app.time.perf_counter_ns", return_value=app.phase_end):
                    app.advance(app.phase_end)
        self.assertFalse(app.active)
        self.assertEqual(app.rec.metadata["status"], "completed")
        self.assertEqual(len(app.rec.rows), 40)
        self.assertTrue(all(r["rt_ms"] == 100 for r in app.rec.rows if r["responded"]))
        events = [json.loads(s) for s in (app.rec.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        accepted = [e for e in events if e["event"] == "response"]
        self.assertEqual(len(accepted), 38)
        self.assertTrue(all(json.loads(e["detail_json"])["input_mode"] == "touch" for e in accepted))

    def test_start_contact_must_release_and_offscreen_release_clears_grab(self):
        app = self.app
        self.click("start", release=False)
        self.assertEqual(app.phase, "prepare")
        with patch("app.time.perf_counter_ns", return_value=app.phase_end):
            app.advance(app.phase_end)
        with patch("app.time.perf_counter_ns", return_value=app.block_start + 100_000_000):
            self.click("response", release=False)
        self.assertIsNone(app.current["response"])
        app.canvas.event_generate("<ButtonRelease-1>", x=-50, y=-50)
        self.assertFalse(app.pointer_down)
        self.assertIsNone(app.canvas.grab_current())
        with patch("app.time.perf_counter_ns", return_value=app.block_start + 150_000_000):
            self.click("response")
        self.assertIsNotNone(app.current["response"])

    def test_scaling_change_saves_session_and_cleans_pending_hold(self):
        self.click("abort", release=False)
        self.assertIsNotNone(self.app.abort_timer)
        old = self.root.tk.call("tk", "scaling")
        try:
            self.root.tk.call("tk", "scaling", float(old) * 1.25)
            self.app.tick()
            self.assertFalse(self.app.active)
            self.assertEqual(self.app.rec.metadata["reason"], "display_changed")
            self.assertIsNone(self.app.abort_timer)
        finally:
            self.root.tk.call("tk", "scaling", old)

    def test_keyboard_mode_starts_with_enter_and_keeps_canvas_without_controls(self):
        self.app.config = replace(self.app.config, input_mode="keyboard")
        self.app.show_instruction()
        self.assertFalse(self.app.canvas.find_withtag("touch-control"))
        self.app.canvas.event_generate("<Return>")
        self.assertEqual(self.app.phase, "prepare")
        with patch("app.time.perf_counter_ns", return_value=self.app.phase_end):
            self.app.advance(self.app.phase_end)
        with patch("app.time.perf_counter_ns", return_value=self.app.block_start + 100_000_000):
            self.app.canvas.event_generate("<KeyPress-space>")
        self.assertIsNotNone(self.app.current["response"])
        self.app.canvas.event_generate("<KeyRelease-space>")


if __name__ == "__main__":
    unittest.main()
