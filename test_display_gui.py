"""Real Tk regression checks for the Surface's DPI and operator screens."""
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import call, patch

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
            if not widget.winfo_ismapped() or widget.winfo_width() <= 1:
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
                        if not widget.winfo_ismapped() or widget.winfo_height() <= 1:
                            continue
                        self.assertGreaterEqual(widget.winfo_rooty(), self.root.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                             self.root.winfo_rooty() + self.root.winfo_height())

    def test_information_uses_lower_space_and_collapses_without_overflow(self):
        for width, height, expanded in ((2196, 1800, True), (1464, 876, False), (2196, 1800, True)):
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
                                 - output_entry.master.winfo_rooty() - output_entry.master.winfo_height(), 20)

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

    def test_maximized_information_uses_horizontal_space_without_clipping(self):
        self.root.geometry("2196x1800+0+0")
        self.root.update()
        self.assertTrue(self.app.setup_information_expanded)
        cards = self.app.setup_information_cards
        for left, right in zip(cards, cards[1:]):
            self.assertEqual(left.winfo_rooty(), right.winfo_rooty())
            self.assertLess(left.winfo_rootx() + left.winfo_width(), right.winfo_rootx())
            self.assertLessEqual(abs(left.winfo_width() - right.winfo_width()), 2)
        area = self.app.setup_information
        self.assertLessEqual(abs(cards[-1].winfo_rootx() + cards[-1].winfo_width()
                                 - area.winfo_rootx() - area.winfo_width()), 8)
        self.assertLess(self.app.setup_preview.winfo_rooty() + self.app.setup_preview.winfo_height(),
                        cards[0].winfo_rooty())
        self.app.baseline.set("300")
        self.app.rest.set("600")
        self.root.update()
        for card in cards:
            for label in card.winfo_children():
                if isinstance(label, ttk.Label):
                    self.assertLessEqual(label.winfo_reqheight(), label.winfo_height())
        for label in self.app.setup_information_lower_labels:
            self.assertTrue(label.winfo_ismapped())
            self.assertLessEqual(label.winfo_reqheight(), label.winfo_height())
            card = label.master.master
            self.assertLessEqual(card.winfo_rooty() + card.winfo_height()
                                 - label.winfo_rooty() - label.winfo_height(), 18)
        self.assert_no_horizontal_clipping()
        self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))

    def test_develop_maximized_section_order_preserves_controls_and_start_actions(self):
        self.root.geometry("2196x1314+0+0")
        self.root.update()
        self.assertTrue(self.app.setup_reference_layout)
        widgets = list(self.descendants(self.app.frame))
        def control(variable):
            return next(w for w in widgets if isinstance(w, (ttk.Entry, ttk.Combobox))
                        and str(w.cget("textvariable")) == str(variable))
        participant = control(self.app.participant)
        baseline = control(self.app.baseline)
        interval = control(self.app.interval)
        target = control(self.app.target_position)
        preview = self.app.setup_preview
        self.assertGreater(preview.winfo_rootx(), participant.winfo_rootx() + participant.winfo_width())
        self.assertGreater(interval.winfo_rooty(), baseline.winfo_rooty() + baseline.winfo_height())
        self.assertGreater(interval.winfo_rooty(), preview.winfo_rooty() + preview.winfo_height())
        self.assertEqual(interval.winfo_rooty(), target.winfo_rooty())
        self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))
        self.assert_no_horizontal_clipping()
        for variable in (self.app.participant, self.app.order, self.app.input_selection, self.app.baseline, self.app.rest,
                         self.app.interval, self.app.stimulus, self.app.target_percent, self.app.target_position,
                         self.app.seed, self.app.output):
            self.assertTrue(control(variable).winfo_ismapped())
        with patch.object(self.app, "start") as start:
            for button in self.app.start_buttons:
                button.invoke()
            self.assertEqual(start.call_args_list, [call(False), call(True)])
        self.root.geometry("1464x876+0+0")
        self.root.update()
        self.assertFalse(self.app.setup_reference_layout)
        self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))

    def test_stimulus_controls_remain_unobscured_after_layout_switches(self):
        # ismapped() stays true even when a later-created sibling covers a frame.
        for width, height in ((2196, 1314), (1464, 876), (2196, 1314)):
            with self.subTest(viewport=(width, height)):
                self.root.geometry(f"{width}x{height}+0+0")
                self.root.lift()
                self.root.update()
                widgets = list(self.descendants(self.app.frame))
                controls = [next(w for w in widgets if isinstance(w, (ttk.Entry, ttk.Combobox))
                                 and str(w.cget("textvariable")) == str(variable))
                            for variable in (self.app.interval, self.app.stimulus, self.app.target_percent,
                                             self.app.target_position, self.app.seed)]
                settings = controls[0].master
                # Include field labels, the section heading, and the seed button.
                for widget in self.descendants(settings):
                    if not isinstance(widget, (ttk.Label, ttk.Entry, ttk.Combobox, ttk.Button)):
                        continue
                    with self.subTest(widget=str(widget)):
                        self.assertTrue(widget.winfo_ismapped())
                        x = widget.winfo_rootx() + widget.winfo_width() // 2
                        y = widget.winfo_rooty() + widget.winfo_height() // 2
                        hit = self.root.winfo_containing(x, y)
                        if hit is not None:
                            self.assertIs(hit, widget)
                        else:
                            # Native hit testing can miss synthetic oversized Windows
                            # viewports. Check Tk's actual bounds and stacking order.
                            current = widget
                            while current is not self.root:
                                parent = current.master
                                self.assertTrue(parent.winfo_rootx() <= x < parent.winfo_rootx() + parent.winfo_width())
                                self.assertTrue(parent.winfo_rooty() <= y < parent.winfo_rooty() + parent.winfo_height())
                                siblings = parent.winfo_children()
                                for sibling in siblings[siblings.index(current) + 1:]:
                                    if sibling.winfo_ismapped():
                                        self.assertFalse(sibling.winfo_rootx() <= x < sibling.winfo_rootx() + sibling.winfo_width()
                                                         and sibling.winfo_rooty() <= y < sibling.winfo_rooty() + sibling.winfo_height(),
                                                         f"{sibling} covers {widget}")
                                current = parent
                self.app.interval.set("1500")
                self.app.stimulus.set("400")
                self.app.target_percent.set("40")
                self.app.target_position.set("9 右下")
                seed_button = next(w for w in self.descendants(settings) if isinstance(w, ttk.Button))
                with patch("app.secrets.randbelow", return_value=12345):
                    seed_button.invoke()
                config = self.app.read_config(False)
                self.assertEqual((config.interval_ms, config.stimulus_ms, config.target_percent,
                                  config.zero_target, config.seed), (1500, 400, 40, 8, 12345))

    def test_reference_layout_has_contiguous_sections_without_duplicate_note_space(self):
        for width, height in ((2196, 1314), (2196, 1428), (1464, 876), (2196, 1314)):
            with self.subTest(viewport=(width, height)):
                self.root.geometry(f"{width}x{height}+0+0")
                self.root.update()
                if width < 1800:
                    self.assertTrue(self.app.setup_information.winfo_ismapped())
                    continue
                self.assertFalse(self.app.setup_information.winfo_ismapped())
                widgets = list(self.descendants(self.app.frame))
                note = next(w for w in widgets if isinstance(w, ttk.Label)
                            and str(w.cget("text")).startswith("1試行＝"))
                marker = next(w for w in widgets if isinstance(w, ttk.Checkbutton)
                              and str(w.cget("text")).startswith("追加のLSL"))
                self.assertLessEqual(marker.winfo_rooty() - note.winfo_rooty() - note.winfo_height(), 10)
                settings = next(w for w in widgets if isinstance(w, ttk.Entry)
                                and str(w.cget("textvariable")) == str(self.app.participant)).master
                advanced = note.master
                gap = advanced.winfo_rooty() - settings.winfo_rooty() - settings.winfo_height()
                self.assertTrue(20 <= gap <= 40, gap)
                self.assertEqual(self.app.setup_scroll.yview(), (0.0, 1.0))
                self.assert_no_horizontal_clipping()

    def test_trial_counts_input_guide_and_recording_guide_follow_settings(self):
        self.assertIn("各200試行", self.app.setup_trial_summary.get())
        self.assertIn("2-back 198", self.app.setup_trial_summary.get())
        self.assertIn("2-back 59", self.app.setup_trial_summary.get())
        self.app.interval.set("1500")
        self.assertIn("各400試行", self.app.setup_trial_summary.get())
        self.assertIn("2-back 398", self.app.setup_trial_summary.get())
        self.assertIn("2-back 119", self.app.setup_trial_summary.get())
        self.app.target_percent.set("50")
        self.assertIn("2-back 199", self.app.setup_trial_summary.get())
        self.app.stimulus.set("1500")
        self.assertIn("数値を確認", self.app.setup_trial_summary.get())
        self.app.stimulus.set("500")
        self.app.input_selection.set("キーボード")
        self.assertIn("Space", self.app.setup_input_guide.get())
        self.app.input_selection.set("タッチ")
        self.assertIn("1.5秒", self.app.setup_input_guide.get())
        self.app.physiology_enabled.set(True)
        self.assertIn("physiology/", self.app.setup_saved_guide.get())
        self.app.physiology_enabled.set(False)
        self.assertIn("保存はOFF", self.app.setup_saved_guide.get())
        self.app.demo = True
        self.app.interval.set("3000")
        self.assertIn("動作確認：各20試行", self.app.setup_trial_summary.get())
        self.app.clear()
        self.assertIsNone(self.app.setup_layout_timer)
        self.assertEqual(self.app.interval.trace_info(), [])

    def test_results_can_scroll_to_return_button_with_physiology_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app.config = Config()
            self.app.rec = Recorder(Path(folder), self.app.config)
            self.app.rec.finish("aborted", "escape")
            # Exercise the longest result screen without connecting hardware.
            self.app.rec.physiology = object()
            self.app.rec.metadata["physiology"] = {"error": "信号の受信に失敗しました。" * 12}
            self.root.geometry("1020x750+0+0")
            self.app.show_results("aborted", "physiology_stream_error")
            self.root.update()
            messages = [str(w.cget("text")) for w in self.descendants(self.app.frame)
                        if isinstance(w, ttk.Label)]
            self.assertIn("中止理由：" + self.app.rec.metadata["physiology"]["error"], messages)
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
