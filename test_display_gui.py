"""Real Tk regression checks for the Surface's DPI and operator screens."""
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest

from app import App
from engine import Config
from storage import Recorder


class SurfaceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        # Permit viewports larger than the host monitor when emulating the Surface.
        self.root.maxsize(4000, 3000)
        self.addCleanup(self.root.destroy)
        # 150% Windows scaling = 144 DPI / 72 points per inch.
        self.root.tk.call("tk", "scaling", 2.0)
        self.app = App(self.root)
        self.initial_window_state = self.root.state()
        self.root.state("normal")

    def test_startup_and_return_to_setup_are_maximized(self):
        self.assertEqual(self.initial_window_state, "zoomed")
        self.app.show_setup()
        self.root.update()
        self.assertEqual(self.root.state(), "zoomed")
        self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def assert_no_horizontal_clipping(self):
        for widget in self.descendants(self.app.frame):
            if not widget.winfo_manager() or widget.winfo_width() <= 1:
                continue
            with self.subTest(widget=str(widget)):
                self.assertGreaterEqual(widget.winfo_x(), 0)
                self.assertLessEqual(widget.winfo_x() + widget.winfo_width(),
                                     widget.master.winfo_width() + 2)
                if isinstance(widget, (ttk.Label, ttk.Button, ttk.Checkbutton)):
                    self.assertLessEqual(widget.winfo_reqwidth(), widget.winfo_width() + 2)

    def test_setup_at_surface_dpi_and_effective_viewports(self):
        for width, height in ((1020, 850), (1464, 876), (1464, 976), (2196, 1364)):
            with self.subTest(viewport=(width, height)):
                self.root.geometry(f"{width}x{height}+0+0")
                self.root.update()
                self.assert_no_horizontal_clipping()
                for button in self.app.start_buttons:
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                         self.root.winfo_rooty() + self.root.winfo_height())
                canvas = self.app.operator_scroll
                canvas.yview_moveto(1)
                self.root.update()
                self.assertAlmostEqual(canvas.yview()[1], 1.0, places=2)

    def test_maximized_surface_setup_shows_every_control_without_scrolling(self):
        # Reserve space for the title bar and taskbar, including in effective coordinates.
        for demo in (False, True):
            self.app.demo = demo
            self.app.show_setup()
            self.root.state("normal")
            for width, height in ((1464, 876), (2196, 1314)):
                with self.subTest(demo=demo, viewport=(width, height)):
                    self.root.geometry(f"{width}x{height}+0+0")
                    self.root.update()
                    self.assert_no_horizontal_clipping()
                    canvas = self.app.setup_scroll
                    self.assertEqual(canvas.yview(), (0.0, 1.0))
                    for widget in self.descendants(self.app.frame):
                        if not widget.winfo_manager() or widget.winfo_height() <= 1:
                            continue
                        self.assertGreaterEqual(widget.winfo_rooty(), self.root.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                             self.root.winfo_rooty() + self.root.winfo_height())

    def test_information_uses_lower_space_and_collapses_without_overflow(self):
        for width, height, expanded in ((2196, 1314, True), (1464, 876, False), (2196, 1314, True)):
            self.root.geometry(f"{width}x{height}+0+0")
            self.root.update()
            self.assertEqual(self.app.setup_information_expanded, expanded)
            self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))
            self.assert_no_horizontal_clipping()
            canvas = self.app.setup_scroll
            content_bounds = canvas.bbox("all")
            self.assertEqual(content_bounds[3], canvas.winfo_height())
            # The output row follows the growing information area at the bottom.
            output_entry = next(w for w in self.descendants(self.app.frame)
                                if isinstance(w, ttk.Entry) and str(w.cget("textvariable")) == str(self.app.output))
            self.assertLessEqual(canvas.winfo_rooty() + canvas.winfo_height()
                                 - output_entry.master.winfo_rooty() - output_entry.master.winfo_height(), 10)

    def test_live_overview_and_target_preview_follow_settings_without_trace_leaks(self):
        self.root.geometry("2196x1314+0+0")
        self.root.update()
        self.assertIn("21分40秒", self.app.setup_summary.get())
        self.assertIn("練習：約2分15秒", self.app.setup_summary.get())
        self.app.order.set("2-back のみ")
        self.app.baseline.set("0")
        self.app.rest.set("120")
        self.app.input_selection.set("キーボード")
        self.assertIn("10分05秒", self.app.setup_summary.get())
        self.assertIn("課題間休憩なし", self.app.setup_summary.get())
        self.assertIn("キーボード", self.app.setup_summary.get())
        self.app.target_position.set("9 右下")
        from engine import STIMULUS
        preview = self.app.setup_preview
        highlighted = [item for item in preview.find_all()
                       if preview.type(item) == "rectangle" and preview.itemcget(item, "fill") == STIMULUS]
        self.assertEqual(len(highlighted), 1)
        self.assertGreater(preview.coords(highlighted[0])[0], preview.winfo_width() / 2)
        self.assertGreater(preview.coords(highlighted[0])[1], preview.winfo_height() / 2)
        self.app.baseline.set("")
        self.assertIn("数値", self.app.setup_summary.get())
        self.app.baseline.set("30")
        self.app.demo = True
        self.app.order.set("0-back → 2-back")
        self.assertIn("0分28秒", self.app.setup_summary.get())
        for _ in range(3):
            self.app.show_setup()
        self.assertEqual(len(self.app.order.trace_info()), 1)
        self.assertEqual(len(self.app.target_position.trace_info()), 1)
        self.app.clear()
        self.assertEqual(self.app.order.trace_info(), [])

    def test_results_can_scroll_to_return_button_with_physiology_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app.config = Config()
            self.app.rec = Recorder(Path(folder), self.app.config)
            self.app.rec.finish("aborted", "escape")
            # Exercise the longest result screen without connecting hardware.
            self.app.rec.physiology = object()
            self.app.rec.metadata["physiology"] = {"error": "信号の受信に失敗しました。" * 12}
            self.root.geometry("1020x750+0+0")
            self.app.show_results("aborted", "escape")
            self.root.update()
            self.assert_no_horizontal_clipping()
            canvas = self.app.operator_scroll
            canvas.yview_moveto(1)
            self.root.update()
            button = next(w for w in self.descendants(self.app.frame)
                          if isinstance(w, ttk.Button) and w.cget("text") == "設定画面へ")
            self.assertGreaterEqual(button.winfo_rooty(), canvas.winfo_rooty())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 canvas.winfo_rooty() + canvas.winfo_height())
            button.invoke()
            self.root.update()
            self.assertEqual(self.app.phase, "setup")

    def test_keyboard_instructions_fit_at_150_percent(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app.config = replace(Config(), input_mode="keyboard")
            self.app.rec = Recorder(Path(folder), self.app.config)
            self.app.clear()
            self.root.geometry("1464x976+0+0")
            self.app.canvas = tk.Canvas(self.root, highlightthickness=0)
            self.app.canvas.pack(fill="both", expand=True)
            self.root.update()
            for block in (0, 1):
                self.app.block_index = block
                self.app.show_instruction()
                for item in self.app.canvas.find_all():
                    x0, y0, x1, y1 = self.app.canvas.bbox(item)
                    self.assertTrue(0 <= x0 < x1 <= 1464)
                    self.assertTrue(0 <= y0 < y1 <= 976)
            self.app.rec.finish("aborted", "test_cleanup")

    def test_trial_components_scale_together_and_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as cleanup:
            app = self.app
            app.config = replace(Config(), input_mode="touch")
            app.rec = Recorder(Path(folder), app.config)
            cleanup.callback(app.rec.close_files)
            app.clear()
            app.canvas = tk.Canvas(self.root, highlightthickness=0)
            app.canvas.pack(fill="both", expand=True)
            for width, height in ((1024, 768), (1464, 976), (2196, 1464)):
                self.root.geometry(f"{width}x{height}+0+0")
                self.root.update()
                self.assertEqual((app.canvas.winfo_width(), app.canvas.winfo_height()), (width, height))
                scale = App.component_scale(width, height)
                for mode in ("touch", "keyboard"):
                    app.config = replace(app.config, input_mode=mode)
                    for block in (0, 1):
                        app.block_index = block
                        app.show_instruction()
                        for item in app.canvas.find_all():
                            x0, y0, x1, y1 = app.canvas.bbox(item)
                            self.assertTrue(0 <= x0 < x1 <= width)
                            self.assertTrue(0 <= y0 < y1 <= height)
                            if mode == "touch" and app.canvas.type(item) == "text" and "touch-control" not in app.canvas.gettags(item):
                                self.assertLess(y1, app.touch_regions["start"][1])
                    start_region = app.touch_regions.get("start")
                    app.neutral_screen()
                    left, top = app.canvas.coords(app.cells[0])[:2]
                    right, bottom = app.canvas.coords(app.cells[-1])[2:]
                    self.assertAlmostEqual(right - left, 360 * scale)
                    self.assertAlmostEqual(bottom - top, 360 * scale)
                    self.assertAlmostEqual((left + right) / 2, width / 2)
                    self.assertAlmostEqual((top + bottom) / 2, height / 2)
                    for phase in ("prepare", "baseline", "task", "rest"):
                        app.phase = phase
                        app.render_touch_controls()
                        if mode == "touch" and phase != "rest":
                            self.assertEqual(app.touch_regions["response"], start_region)
                            self.assertGreater(app.touch_regions["response"][1], bottom)
                        if mode == "touch":
                            abort = app.touch_regions["abort"]
                            self.assertLess(abort[3], top)
                            self.assertTrue(app.abort_retains_contact(abort[0] - 31 * scale, abort[1]))
                            self.assertFalse(app.abort_retains_contact(abort[0] - 33 * scale, abort[1]))
                        if phase in ("prepare", "baseline"):
                            app.phase_end = app.rec.clock.perf_ns + 30_000_000_000
                            app.start_cue_text = None
                            app.update_start_cue(app.rec.clock.perf_ns)
                            cue = app.canvas.bbox("start-cue")
                            self.assertLess(cue[3], top)
                            if mode == "touch":
                                self.assertGreater(cue[1], app.touch_regions["abort"][3])
                    app.canvas.delete("all")


if __name__ == "__main__":
    unittest.main()
