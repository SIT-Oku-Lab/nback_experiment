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
