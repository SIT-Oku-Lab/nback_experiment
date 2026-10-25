"""Real Tk regression checks for the Surface's DPI and operator screens."""
from dataclasses import replace
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
        self.addCleanup(self.root.destroy)
        # 150% Windows scaling = 144 DPI / 72 points per inch.
        self.root.tk.call("tk", "scaling", 2.0)
        self.app = App(self.root)

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


if __name__ == "__main__":
    unittest.main()
