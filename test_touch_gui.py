"""Real Tk event-path/layout checks; synthetic clicks are not physical touch tests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

from app import App
from engine import Config
from storage import Recorder
from windows_touch import FLAGS, PROPERTY


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
        self.fullscreen_attributes = self.root.attributes
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

    def test_real_fullscreen_transition_does_not_trigger_display_abort(self):
        self.app.abort("test_restart")
        self.root.attributes = self.fullscreen_attributes
        self.app.rec = Recorder(self.app.rec.path.parent, self.app.config)
        self.app.enter_session()
        self.root.focus_force()
        self.root.update()
        self.assertEqual((self.app.canvas.winfo_width(), self.app.canvas.winfo_height()),
                         (self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
        self.assertFalse(self.app.check_display_changed())
        self.assertFalse(self.app.display_changed)

    def test_real_tk_long_hold_timer_aborts_without_confirmation_dialog(self):
        self.click("abort", release=False)
        self.assertIn("中止まで", self.app.canvas.itemcget("abort-label", "text"))
        # Exercise Tk's real after callback, rather than calling the handler directly.
        stop = self.root.after(1700, self.root.quit)
        try:
            self.root.mainloop()
            self.assertFalse(self.app.active)
            self.assertEqual(self.app.rec.metadata["reason"], "touch_hold")
            self.assertIsNone(self.app.abort_timer)
            self.assertIsNone(self.app.canvas)
        finally:
            self.root.after_cancel(stop)

    @unittest.skipUnless(sys.platform == "win32", "Windows-specific window properties")
    def test_windows_long_press_policy_is_applied_and_restored_on_exit(self):
        policy = self.app.touch_policy
        saved = policy.previous.copy()
        self.assertTrue(self.app.rec.metadata["input"]["windows_touch"]["applied"])
        self.assertIn(self.app.canvas.winfo_id(), saved)
        for hwnd, old in saved.items():
            self.assertEqual(policy.api.GetPropW(hwnd, PROPERTY), old | FLAGS)
        self.app.abort("test_cleanup")
        self.assertIsNone(self.app.touch_policy)
        # The top-level wrapper still exists after the task canvas is destroyed.
        hwnd = policy.api.GetAncestor(self.root.winfo_id(), 2)
        self.assertEqual(policy.api.GetPropW(hwnd, PROPERTY) or 0, saved[hwnd])

    def test_abort_countdown_cancels_when_contact_moves_out(self):
        self.click("abort", release=False)
        self.assertEqual(self.app.canvas.itemcget("abort-face", "fill"), "#bfbfbf")
        self.app.canvas.event_generate("<B1-Motion>", x=0, y=0)
        self.assertIsNone(self.app.abort_timer)
        self.assertEqual(self.app.canvas.itemcget("abort-label", "text"), "長押しで中止")
        self.assertTrue(self.app.active)
        self.app.canvas.event_generate("<ButtonRelease-1>", x=0, y=0)

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

    def test_response_press_is_visible_after_short_tap_and_then_resets(self):
        self.click("start")
        self.app.begin_block()
        canvas = self.app.canvas
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#707070")
        self.click("response", release=False)
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#bfbfbf")
        self.assertEqual(canvas.itemcget("response-label", "fill"), "#262b2a")
        self.assertIsNotNone(self.app.current["response"])
        canvas.event_generate("<ButtonRelease-1>", x=-50, y=-50)
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#bfbfbf")
        self.root.after(200, self.root.quit)
        self.root.mainloop()
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#707070")
        self.assertIsNone(self.app.response_feedback_timer)

    def test_feedback_is_identical_for_warmup_and_rejected_repeated_tap(self):
        self.click("start")
        self.app.block_index = 1
        self.app.begin_block()
        canvas = self.app.canvas
        self.click("response")
        self.assertIsNone(self.app.current["response"])  # 2-back warmup is unscored.
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#bfbfbf")
        # Move to an eligible trial, then tap twice within that same trial.
        at = self.app.block_start + 1_200_000_000
        with patch("app.time.perf_counter_ns", return_value=at):
            self.app.advance(at)
        with patch("app.time.perf_counter_ns", return_value=at + 100_000_000):
            self.click("response")
        response = self.app.current["response"]
        self.assertIsNotNone(response)
        with patch("app.time.perf_counter_ns", return_value=at + 200_000_000):
            self.click("response")
        self.assertEqual(self.app.current["response"], response)
        self.assertEqual(canvas.itemcget("response-face", "fill"), "#bfbfbf")
        self.app.begin_phase("rest", 1)
        self.assertFalse(canvas.find_withtag("response-face"))
        self.assertIsNone(self.app.response_feedback_timer)


if __name__ == "__main__":
    unittest.main()
