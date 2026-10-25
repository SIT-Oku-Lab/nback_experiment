"""Offline, fixed-duration spatial 0/2-back experiment for Windows/Tk."""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
import traceback

from engine import (BACKGROUND, FIXATION, GRID, STIMULUS, INTERVALS, VERSION,
                    Config, accepts_response, classify, make_sequence, scheduled_index, summarize)
from storage import Recorder, utc_iso
from windows_touch import WindowsTouchPolicy
from native_touch import WindowsTouchInput

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ORDERS = {"0-back → 2-back": (0, 2), "2-back → 0-back": (2, 0),
          "0-back のみ": (0,), "2-back のみ": (2,)}
POSITIONS = ("1 左上", "2 上", "3 右上", "4 左", "5 中央", "6 右", "7 左下", "8 下", "9 右下")
INPUT_MODES = {"タッチ": "touch", "キーボード": "keyboard"}
ABORT_HOLD_NS = 1_500_000_000
ABORT_MOVE_MARGIN = 32
ABORT_OUTSIDE_GRACE_NS = 250_000_000


class App:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.demo = demo
        self.root.title("N-back 実験 | 0-back / 2-back")
        setup_width = min(1020, self.root.winfo_screenwidth() - 40)
        setup_height = min(850, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{setup_width}x{setup_height}")
        self.root.minsize(min(920, setup_width), min(600, setup_height))
        self.root.configure(bg="#eeefed")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Yu Gothic UI", 10))
        style.configure("TFrame", background="#eeefed")
        style.configure("TLabel", background="#eeefed", foreground="#262b2a", font=("Yu Gothic UI", 11))
        style.configure("Muted.TLabel", foreground="#606763", font=("Yu Gothic UI", 10))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 25, "bold"))
        style.configure("SetupTitle.TLabel", font=("Yu Gothic UI", 16, "bold"))
        style.configure("Heading.TLabel", font=("Yu Gothic UI", 13, "bold"))
        style.configure("TButton", font=("Yu Gothic UI", 11), padding=(16, 10))
        style.configure("Primary.TButton", foreground="#ffffff", background="#424a45", padding=(20, 13))
        style.map("Primary.TButton", background=[("active", "#303a33")])
        style.configure("Setup.TButton", padding=(16, 7))
        style.configure("SetupPrimary.TButton", foreground="#ffffff", background="#424a45", padding=(20, 8))
        style.map("SetupPrimary.TButton", background=[("active", "#303a33")])
        style.configure("TCombobox", padding=5)
        style.configure("TEntry", padding=5)
        style.configure("Treeview", rowheight=tkfont.Font(family="Yu Gothic UI", size=10).metrics("linespace") + 12)
        self.active = False
        self.starting = False
        self.closing = False
        self.discovery_running = False
        self.streams = {}
        self.phase = "setup"
        self.rec: Recorder | None = None
        self.canvas: tk.Canvas | None = None
        self.frame: ttk.Frame | None = None
        self.timer = None
        self.key_down = False
        self.pointer_down = False
        self.pointer_action = None
        self.pointer_source = None
        self.touch_regions = {}
        self.abort_timer = None
        self.response_feedback_timer = None
        self.response_feedback_until = None
        self.focus_timer = None
        self.abort_hold_start = None
        self.abort_outside_since = None
        self.session_display = None
        self.display_changed = False
        self.touch_policy = None
        self.native_touch = None
        self.block_index = 0
        self.current_index = -1
        self.current = None
        self.output = tk.StringVar(value=str(APP_DIR / "results"))
        self.participant = tk.StringVar(value="P001")
        self.input_selection = tk.StringVar(value="タッチ")
        self.order = tk.StringVar(value=next(iter(ORDERS)))
        self.interval = tk.StringVar(value="3000")
        self.stimulus = tk.StringVar(value="500")
        self.target_percent = tk.StringVar(value="30")
        self.target_position = tk.StringVar(value=POSITIONS[4])
        self.baseline = tk.StringVar(value="30")
        self.rest = tk.StringVar(value="60")
        self.seed = tk.StringVar(value=str(secrets.randbelow(2**32)))
        self.lsl_enabled = tk.BooleanVar(value=False)
        self.physiology_enabled = tk.BooleanVar(value=importlib.util.find_spec("pylsl") is not None)
        self.stream_selection = tk.StringVar()
        self.sensor_status = tk.StringVar(value="OpenSignalsの計測を開始してから、信号を検索してください。")
        self.root.bind("<KeyPress-space>", self.on_space)
        self.root.bind("<KeyRelease-space>", self.on_release)
        self.root.bind("<Return>", self.on_enter)
        self.root.bind("<Escape>", lambda e: self.abort("escape") if self.active else None)
        self.root.bind("<KeyPress-m>", self.manual_marker)
        self.root.bind("<KeyPress-M>", self.manual_marker)
        self.root.bind("<MouseWheel>", self.on_wheel)
        self.root.bind("<FocusOut>", self.on_focus_out)
        self.root.bind("<ButtonPress-1>", self.on_pointer_press)
        self.root.bind("<ButtonRelease-1>", self.on_pointer_release)
        self.root.bind("<B1-Motion>", self.on_pointer_motion)
        self.root.bind("<Configure>", self.on_display_configure)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.on_error
        self.show_setup()

    def clear(self):
        if self.native_touch is not None:
            self.native_touch.stop()
            self.native_touch = None
        if self.touch_policy is not None:
            self.touch_policy.restore()
            self.touch_policy = None
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None
        if self.canvas is not None:
            self.canvas.destroy()
            self.canvas = None

    def label(self, parent, text, style="TLabel", **kwargs):
        widget = ttk.Label(parent, text=text, style=style, **kwargs)
        if "wraplength" not in kwargs:
            parent.bind("<Configure>", lambda e: widget.configure(wraplength=max(100, e.width - 16)), add="+")
        return widget

    def scroll_content(self, parent):
        """Keep long operator screens accessible at any DPI/window height."""
        area = ttk.Frame(parent)
        area.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(area, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(area, bg="#eeefed", highlightthickness=0, yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=canvas.yview)
        content = ttk.Frame(canvas, padding=(24, 20, 24, 16))
        window = canvas.create_window(0, 0, window=content, anchor="nw")
        content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.operator_scroll = canvas
        return content

    def show_setup(self):
        self.clear()
        self.phase = "setup"
        self.root.attributes("-fullscreen", False)
        self.maximize_window()
        self.root.configure(cursor="", bg="#eeefed")
        self.frame = ttk.Frame(self.root)
        self.frame.pack(fill="both", expand=True)
        footer = ttk.Frame(self.frame, padding=(24, 6, 24, 10))
        footer.pack(side="bottom", fill="x")
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")
        self.start_buttons = []
        for text, practice in (("動作確認を開始" if self.demo else "本実験を開始", False), ("練習（各60秒）", True)):
            button = ttk.Button(buttons, text=text, style="Setup.TButton" if practice else "SetupPrimary.TButton",
                                command=lambda p=practice: self.start(p))
            self.start_buttons.append(button)
        manual_button = ttk.Button(buttons, text="手順書", style="Setup.TButton", command=self.open_manual)
        def arrange_buttons(event):
            controls = self.start_buttons + [manual_button]
            wide = event.width >= sum(button.winfo_reqwidth() + 10 for button in controls)
            for index, button in enumerate(controls):
                button.grid(row=0 if wide else index, column=index if wide else 0,
                            sticky="ew", padx=(0, 10), pady=2)
        buttons.bind("<Configure>", arrange_buttons)
        self.label(footer, "タッチ：開始・一致／右上を長押しで中止　｜　キー：Enter・Space・Esc　｜　M：時刻マーカー",
                   "Muted.TLabel").pack(anchor="w", pady=(4, 0))
        f = self.scroll_content(self.frame)
        f.configure(padding=(24, 8, 24, 8))
        self.setup_scroll = self.operator_scroll
        header = ttk.Frame(f)
        header.pack(fill="x")
        self.label(header, "空間 n-back 課題", "SetupTitle.TLabel").pack(side="left")
        duration = "動作確認：各12秒" if self.demo else "本実験：各10分／練習：各60秒"
        self.label(header, f"v{VERSION}　｜　{duration}", "Muted.TLabel").pack(side="right")

        lsl_available = importlib.util.find_spec("pylsl") is not None
        sensor = ttk.Frame(f)
        sensor.pack(fill="x", pady=(6, 8))
        ttk.Checkbutton(sensor, text="OpenSignalsの生理信号を受信・自動保存", variable=self.physiology_enabled,
                        state="normal" if lsl_available else "disabled").pack(anchor="w")
        stream_row = ttk.Frame(sensor)
        stream_row.pack(fill="x", pady=(3, 2))
        self.stream_combo = ttk.Combobox(stream_row, textvariable=self.stream_selection,
                                        values=tuple(self.streams), width=30, state="readonly")
        self.stream_combo.pack(side="left", fill="x", expand=True)
        self.discover_button = ttk.Button(stream_row, text="信号を検索", style="Setup.TButton", command=self.discover)
        self.discover_button.pack(side="left", padx=(8, 0))
        if not lsl_available:
            self.discover_button.configure(state="disabled")
            self.sensor_status.set("信号受信にはpylslが必要です（配布exeには同梱）。")
        sensor_label = ttk.Label(sensor, textvariable=self.sensor_status, style="Muted.TLabel")
        sensor_label.pack(anchor="w")
        sensor.bind("<Configure>", lambda e: sensor_label.configure(wraplength=max(100, e.width - 16)))

        columns = ttk.Frame(f)
        columns.pack(fill="x")
        left, right = ttk.Frame(columns), ttk.Frame(columns)
        left.grid(row=0, column=0, sticky="nw")
        right.grid(row=0, column=1, sticky="nw", padx=(28, 0))
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)
        self.label(left, "実施設定", "Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.form_row(left, 1, "参加者ID", self.participant, width=18)
        self.form_row(left, 2, "課題順序", self.order, tuple(ORDERS), width=18)
        self.form_row(left, 3, "入力方式", self.input_selection, tuple(INPUT_MODES), width=18)
        self.form_row(left, 4, "開始前の安静（秒）", self.baseline, width=10)
        self.form_row(left, 5, "課題間の休憩（秒）", self.rest, width=10)
        self.label(left, "練習：安静なし・課題間休憩5秒。\n入力方式は開始後に変更できません。", "Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 0))

        self.label(right, "刺激・記録設定", "Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.form_row(right, 1, "提示間隔（ms）", self.interval, tuple(map(str, INTERVALS)), width=12)
        self.form_row(right, 2, "刺激提示（ms）", self.stimulus, width=12)
        self.form_row(right, 3, "ターゲット率（%）", self.target_percent, width=12)
        self.form_row(right, 4, "0-back指定位置", self.target_position, POSITIONS, width=12)
        seed_row = ttk.Frame(right)
        self.label(right, "乱数シード").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=3)
        seed_row.grid(row=5, column=1, sticky="w", pady=3)
        ttk.Entry(seed_row, textvariable=self.seed, width=12).pack(side="left")
        ttk.Button(seed_row, text="更新", style="Setup.TButton", command=lambda: self.seed.set(str(secrets.randbelow(2**32)))).pack(side="left", padx=(6, 0))
        self.label(right, "1試行＝提示間隔。次の刺激直前まで応答可。", "Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 0))
        def arrange_columns(event):
            wide = event.width >= left.winfo_reqwidth() + right.winfo_reqwidth() + 28
            right.grid(row=0 if wide else 1, column=1 if wide else 0, sticky="nw",
                       padx=(28 if wide else 0, 0), pady=(0 if wide else 12, 0))
        columns.bind("<Configure>", arrange_columns)

        preview_row = ttk.Frame(f)
        preview_row.pack(fill="x", pady=(8, 5))
        preview = tk.Canvas(preview_row, width=100, height=84, bg=BACKGROUND, highlightthickness=0)
        preview.pack(side="left", padx=(0, 14))
        self.draw_grid(preview, 50, 42, 72, sample=1)
        self.label(preview_row, "グレー背景・位置刺激。得点・残り時間・正誤・音なし。\n"
                   "一致時のみ1回応答。タッチは横向き・1024×768以上・回転ロック。\n"
                   "生理信号は試行・課題終了ごとに保存。LabRecorder不要。", "Muted.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(f, text="追加のLSLマーカー送出（任意・LabRecorder等で記録）", variable=self.lsl_enabled,
                        state="normal" if lsl_available else "disabled").pack(anchor="w")
        output_row = ttk.Frame(f)
        output_row.pack(fill="x")
        self.label(output_row, "保存先").pack(side="left", padx=(0, 12))
        ttk.Entry(output_row, textvariable=self.output).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="変更", style="Setup.TButton", command=self.choose_output).pack(side="left", padx=(8, 0))

    def maximize_window(self):
        try:
            self.root.state("zoomed")
        except tk.TclError:
            # X11 uses an attribute for maximization instead of a window state.
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                pass

    @staticmethod
    def component_scale(width, height):
        """Preserve the reference layout's proportions within the actual viewport."""
        return min(width / 1024, height / 768)

    def canvas_scale(self):
        return self.component_scale(self.canvas.winfo_width(), self.canvas.winfo_height())

    def on_wheel(self, event):
        if self.phase in ("setup", "results"):
            self.operator_scroll.yview_scroll(-int(event.delta / 120), "units")

    def form_row(self, parent, row, title, variable, values=None, width=18, col=0):
        self.label(parent, title).grid(row=row, column=col, sticky="w", padx=(0 if col == 0 else 40, 12), pady=3)
        if values:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, width=width, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=variable, width=width)
        widget.grid(row=row, column=col + 1, sticky="w", pady=3)

    def choose_output(self):
        selected = filedialog.askdirectory(title="実験データの保存先", initialdir=APP_DIR)
        if selected:
            self.output.set(selected)

    def open_manual(self):
        path = APP_DIR / "README_ja.md"
        if sys.platform == "win32":
            os.startfile(path)

    def read_config(self, practice: bool):
        try:
            cfg = Config(participant=self.participant.get().strip(), conditions=ORDERS[self.order.get()],
                         interval_ms=int(self.interval.get()), stimulus_ms=int(self.stimulus.get()),
                         target_percent=int(self.target_percent.get()), zero_target=POSITIONS.index(self.target_position.get()),
                         baseline_s=int(self.baseline.get()), rest_s=int(self.rest.get()), seed=int(self.seed.get()),
                         mode="practice" if practice else "experiment", lsl=self.lsl_enabled.get(),
                         input_mode=INPUT_MODES[self.input_selection.get()],
                         physiology=self.physiology_enabled.get(),
                         physiology_uid=self.streams[self.stream_selection.get()].uid() if self.stream_selection.get() in self.streams else "",
                         physiology_name=self.streams[self.stream_selection.get()].name() if self.stream_selection.get() in self.streams else "")
        except (ValueError, KeyError):
            raise ValueError("設定の数値と課題の選択を確認してください。") from None
        if practice:
            cfg = replace(cfg, baseline_s=0, rest_s=5)
        elif self.demo:
            cfg = replace(cfg, interval_ms=600, stimulus_ms=200, baseline_s=1, rest_s=1, mode="demo")
        cfg.validate()
        return cfg

    def start(self, practice: bool):
        if self.starting or self.discovery_running:
            return
        try:
            cfg = self.read_config(practice)
            if cfg.input_mode == "touch":
                error = self.touch_display_error(self.root.winfo_screenwidth(), self.root.winfo_screenheight())
                if error:
                    raise ValueError(error)
            if not self.output.get().strip():
                raise ValueError("保存先を指定してください。")
            parent = Path(self.output.get()).expanduser().resolve()
        except Exception as exc:
            messagebox.showerror("開始できません", str(exc))
            return
        info = self.streams.get(self.stream_selection.get()) if cfg.physiology else None
        self.starting = True
        self.sensor_status.set("生理信号の受信・時計同期・保存先を確認しています…" if cfg.physiology else "保存先を準備しています…")
        for button in self.start_buttons:
            button.configure(state="disabled")
        self.discover_button.configure(state="disabled")
        results = queue.Queue()

        def prepare():
            try:
                rec = Recorder(parent, cfg, info)
                if self.closing:
                    rec.finish("aborted", "window_closed_during_initialization")
                else:
                    results.put((rec, None))
            except Exception as exc:
                results.put((None, exc))

        threading.Thread(target=prepare, name="Experiment-prepare", daemon=False).start()

        def poll():
            if self.closing:
                return
            try:
                rec, error = results.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            self.starting = False
            for button in self.start_buttons:
                button.configure(state="normal")
            self.discover_button.configure(state="normal" if importlib.util.find_spec("pylsl") is not None else "disabled")
            if error:
                self.sensor_status.set("開始できませんでした：" + str(error))
                messagebox.showerror("開始できません", str(error))
                return
            self.rec, self.config = rec, cfg
            self.enter_session()

        self.root.after(100, poll)

    def discover(self):
        if self.discovery_running or self.starting:
            return
        self.discovery_running = True
        self.discover_button.configure(state="disabled")
        self.sensor_status.set("信号を検索しています…（OpenSignalsでLSLと計測を有効にしてください）")
        results = queue.Queue()

        def search():
            try:
                from physiology import discover_streams
                results.put((discover_streams(), None))
            except Exception as exc:
                results.put(([], exc))

        threading.Thread(target=search, name="LSL-discovery", daemon=True).start()

        def poll():
            if self.closing:
                return
            try:
                infos, error = results.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            self.discovery_running = False
            self.discover_button.configure(state="normal")
            self.streams = {}
            for info in infos:
                label = f"{info.name()} | {info.type()} | {info.hostname()} | {info.channel_count()}ch {info.nominal_srate():g}Hz | {info.uid()[:8]}"
                self.streams[label] = info
            self.stream_combo.configure(values=tuple(self.streams))
            preferred = [label for label, info in self.streams.items() if info.name().lower() == "opensignals"]
            # Never silently pick one device when several are advertised.
            selected = preferred[0] if len(preferred) == 1 else (next(iter(self.streams)) if len(self.streams) == 1 else "")
            self.stream_selection.set(selected)
            if error:
                self.sensor_status.set("検索に失敗しました：" + str(error))
            elif not infos:
                self.sensor_status.set("生理信号が見つかりません。OpenSignalsのINTEGRATIONS → LSLと、計測開始を確認してください。")
            else:
                self.sensor_status.set(f"{len(infos)}件の信号が見つかりました。記録するデバイス（MAC/type）を確認して選択してください。")

        self.root.after(100, poll)

    def enter_session(self):
        self.sequences = [make_sequence(self.config, n, b) for b, n in enumerate(self.config.conditions)]
        import csv
        with (self.rec.path / "planned_trials.csv").open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["block", "n", "trial", "position", "eligible", "target", "block_onset_ms"])
            for b, sequence in enumerate(self.sequences):
                for trial in sequence:
                    writer.writerow([b + 1, self.config.conditions[b], trial.index + 1, trial.position + 1,
                                     int(trial.eligible), int(trial.target), trial.index * self.config.interval_ms])
        self.clear()
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=BACKGROUND, cursor="none")
        self.canvas = tk.Canvas(self.root, bg=BACKGROUND, highlightthickness=0, cursor="none", takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.canvas.focus_set()
        self.active = True
        self.key_down = False
        self.pointer_down = False
        self.pointer_action = None
        self.touch_regions = {}
        self.display_changed = False
        self.session_display = self.display_signature()
        self.block_index = 0
        self.current = None
        self.presented = set()
        self.rec.metadata.update(display={"width_px": self.canvas.winfo_width(), "height_px": self.canvas.winfo_height(),
                                          "tk_scaling": self.root.tk.call("tk", "scaling"),
                                          "layout_reference_px": [1024, 768],
                                          "layout_scale": self.canvas_scale(),
                                          "grid_size_px": 360 * self.canvas_scale(),
                                          "gap_px": 8 * self.canvas_scale(),
                                          "fixation_size_px": 12 * self.canvas_scale()})
        self.rec.metadata["input"] = {
            "mode": self.config.input_mode,
            "response_time_definition": "monotonic native-contact callback or Tk-handler entry; not physical contact time",
            "pointer_event": "WM_POINTERDOWN primary contacts; Tk mouse/pen fallback" if self.config.input_mode == "touch" else None,
        }
        if self.config.input_mode == "touch":
            self.touch_policy = WindowsTouchPolicy()
            windows_touch = self.touch_policy.install(self.root, self.canvas)
            self.native_touch = WindowsTouchInput(self.root, self.canvas, self.on_native_contact)
            native_touch = self.native_touch.install()
            self.rec.metadata["input"].update(
                task_regions=self.touch_layout(*self.session_display[:2], "task"), abort_hold_ms=1500,
                abort_move_margin_px=ABORT_MOVE_MARGIN * self.canvas_scale(), abort_outside_grace_ms=250,
                windows_touch=windows_touch,
                native_touch=native_touch,
                response_feedback={"kind": "grayscale_press", "minimum_ms": 150, "depends_on_scoring": False})
        self.rec.save_metadata()
        if self.config.input_mode == "touch":
            error = self.touch_display_error(*self.session_display[:2])
            if error:
                self.abort("display_unsupported")
                return
        self.show_instruction()
        self.schedule_tick()

    def show_instruction(self):
        self.phase = "ready"
        self.rec.event("instruction_start", block=self.block_index + 1, n=self.config.conditions[self.block_index])
        c = self.canvas
        c.delete("all")
        x, y = c.winfo_width() / 2, c.winfo_height() / 2
        n = self.config.conditions[self.block_index]
        touch = self.config.input_mode == "touch"
        scale = self.canvas_scale()
        font_size = lambda points: -max(1, round(points * 4 / 3 * scale))
        c.create_text(x, y - 230 * scale, text=f"{n}-back", font=("Yu Gothic UI", font_size(32), "bold"), fill="#ededed")
        action = "一致ボタンに1回触れてください。" if touch else "Space キーを1回押してください。"
        if n == 0:
            text = f"指定された位置（{POSITIONS[self.config.zero_target]}）に出たときだけ\n{action}"
            self.draw_grid(c, x, y - 20 * scale, 156 * scale, sample=self.config.zero_target, scale=scale)
        else:
            text = f"現在の位置が「2つ前」と同じときだけ\n{action}最初の2試行は見るだけです。"
            self.draw_grid(c, x, y - 20 * scale, 156 * scale, scale=scale)
        c.create_text(x, y - 155 * scale, text=text, justify="center", width=c.winfo_width() - 80 * scale,
                      font=("Yu Gothic UI", font_size(16)), fill="#ededed")
        duration = self.config.duration_ms // 1000
        duration_text = "10分間" if duration == 600 else f"{duration}秒間"
        c.create_text(x, y + 108 * scale, text=f"{duration_text}、位置を見て判断してください。", font=("Yu Gothic UI", font_size(14)), fill="#ededed")
        c.create_text(x, y + 168 * scale, text="準備ができたら下の開始に触れてください" if touch else "準備ができたら Enter", font=("Yu Gothic UI", font_size(18), "bold"), fill="#ededed")
        stop = "中止は右上を1.5秒長押し。" if touch else "中止は Esc。"
        c.create_text(x, y + 212 * scale, text=f"開始前に残り秒数を表示します。{stop}", font=("Yu Gothic UI", font_size(11)), fill="#e0e0e0")
        if self.block_index == 0 and self.config.baseline_s:
            c.create_text(x, y + 245 * scale, text=f"最初に{self.config.baseline_s}秒間、中央を見て安静にしてください。", font=("Yu Gothic UI", font_size(11)), fill="#e0e0e0")
        self.render_touch_controls()
        self.root.update_idletasks()

    @staticmethod
    def draw_grid(canvas, x, y, size, sample=None, scale=1):
        gap = (8 if size / scale >= 300 else 4) * scale
        cell = (size - gap * 2) / 3
        items = []
        for position in range(9):
            col, row = position % 3, position // 3
            left = x - size / 2 + col * (cell + gap)
            top = y - size / 2 + row * (cell + gap)
            item = canvas.create_rectangle(left, top, left + cell, top + cell, outline=GRID,
                                           width=scale, fill=STIMULUS if position == sample else BACKGROUND)
            items.append(item)
        canvas.create_line(x - 6 * scale, y, x + 6 * scale, y, fill=FIXATION, width=2 * scale)
        canvas.create_line(x, y - 6 * scale, x, y + 6 * scale, fill=FIXATION, width=2 * scale)
        return items

    def neutral_screen(self):
        self.canvas.delete("all")
        self.start_cue_text = None
        scale = self.canvas_scale()
        self.cells = self.draw_grid(self.canvas, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                                    360 * scale, scale=scale)
        self.render_touch_controls()
        self.root.update_idletasks()

    def on_enter(self, event=None):
        if self.active and self.config.input_mode == "keyboard":
            self.start_from_instruction()
        return "break" if self.active else None

    def start_from_instruction(self):
        if self.active and self.phase == "ready":
            if self.display_changed:
                self.abort("display_changed")
                return
            self.rec.event("instruction_end", block=self.block_index + 1)
            self.neutral_screen()
            self.begin_phase("prepare", self.config.prepare_s)

    def begin_phase(self, phase: str, seconds: float, at=None):
        self.phase = phase
        self.render_touch_controls()
        now = time.perf_counter_ns() if at is None else at
        self.phase_end = now + round(seconds * 1e9)
        self.update_start_cue(now)
        self.rec.event(phase + "_start", now, block=self.block_index + 1, duration_s=seconds)
        self.rec.checkpoint()

    def update_start_cue(self, now: int):
        if self.phase not in ("prepare", "baseline"):
            return
        remaining_ns = max(0, self.phase_end - now)
        if self.phase == "prepare" and self.block_index == 0:
            remaining_ns += self.config.baseline_s * 1_000_000_000
        # Round up so zero is never displayed while waiting for the first stimulus.
        remaining = max(1, (remaining_ns + 999_999_999) // 1_000_000_000)
        if remaining <= 3:
            status = "まもなく開始します"
        elif self.phase == "baseline":
            status = "安静中：中央を見てお待ちください"
        else:
            status = "準備中：まだ回答しないでください"
        n = self.config.conditions[self.block_index]
        text = f"{status}\n{n}-back 開始まで {remaining} 秒"
        if text == getattr(self, "start_cue_text", None):
            return
        self.canvas.delete("start-cue")
        scale = self.canvas_scale()
        self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2 - 235 * scale,
                                text=text, justify="center", width=self.canvas.winfo_width() - 80 * scale,
                                font=("Yu Gothic UI", -max(1, round(22 * scale)), "bold"), fill="#ededed", tags="start-cue")
        self.start_cue_text = text

    def schedule_tick(self):
        if self.active:
            self.timer = self.root.after(5, self.tick)

    def tick(self):
        self.timer = None
        if not self.active:
            return
        if self.config.input_mode == "touch" and self.check_display_changed():
            return
        sensor_error = self.rec.physiology_error()
        if sensor_error:
            self.rec.event("physiology_error", message=sensor_error)
            self.abort("physiology_stream_error")
            return
        self.advance(time.perf_counter_ns())
        if self.active and self.rec.lsl_failed:
            self.abort("lsl_send_failed")
        self.schedule_tick()

    def advance(self, now: int):
        if not self.active:
            return
        if self.phase == "task":
            self.advance_task(now)
        elif self.phase in ("prepare", "baseline", "rest") and now >= self.phase_end:
            previous = self.phase
            self.rec.event(previous + "_end", now, block=self.block_index + 1,
                           planned_end_session_ms=self.rec.ms(self.phase_end), lateness_ms=(now - self.phase_end) / 1e6)
            if previous == "prepare" and self.block_index == 0 and self.config.baseline_s:
                self.begin_phase("baseline", self.config.baseline_s)
            elif previous == "rest":
                self.block_index += 1
                self.show_instruction()
            else:
                self.begin_block()
        elif self.phase in ("prepare", "baseline"):
            self.update_start_cue(now)

    def begin_block(self):
        self.phase = "task"
        self.canvas.delete("start-cue")
        self.start_cue_text = None
        self.render_touch_controls()
        self.root.update_idletasks()
        self.current_index = -1
        self.current = None
        self.presented = set()
        self.block_start = time.perf_counter_ns()
        self.block_end = self.block_start + self.config.duration_ms * 1_000_000
        self.rec.event("block_start", self.block_start, block=self.block_index + 1,
                       n=self.config.conditions[self.block_index], planned_duration_ms=self.config.duration_ms)
        self.advance_task(time.perf_counter_ns())

    def advance_task(self, now: int):
        interval_ns = self.config.interval_ms * 1_000_000
        index = scheduled_index(now - self.block_start, self.config.interval_ms)
        sequence = self.sequences[self.block_index]
        # Responses always use absolute deadlines, even when a GUI callback is delayed.
        if self.current is not None:
            if now >= self.current["deadline"]:
                self.close_trial("completed", now)
            elif self.current["offset"] is None and now >= self.current["planned"] + self.config.stimulus_ms * 1_000_000:
                self.hide_stimulus()
        if now >= self.block_end:
            for missed in range(self.current_index + 1, len(sequence)):
                self.skip_trial(missed, now)
            self.rec.event("block_end", now, block=self.block_index + 1, n=self.config.conditions[self.block_index],
                           planned_end_session_ms=self.rec.ms(self.block_end),
                           actual_duration_ms=(now - self.block_start) / 1e6,
                           end_lateness_ms=(now - self.block_end) / 1e6)
            self.rec.checkpoint()
            if self.block_index + 1 < len(self.config.conditions):
                self.begin_phase("rest", self.config.rest_s)
            else:
                self.finish("completed")
            return
        if index <= self.current_index:
            return
        for missed in range(self.current_index + 1, min(index, len(sequence))):
            self.skip_trial(missed, now)
        planned = self.block_start + index * interval_ns
        if now >= planned + self.config.stimulus_ms * 1_000_000:
            self.skip_trial(index, now)
            return
        trial = sequence[index]
        self.canvas.itemconfigure(self.cells[trial.position], fill=STIMULUS)
        self.root.update_idletasks()
        onset = time.perf_counter_ns()
        self.current_index = index
        self.current = {"trial": trial, "planned": planned, "onset": onset,
                        "deadline": planned + interval_ns, "offset": None, "response": None,
                        "history_valid": all(i in self.presented for i in range(max(0, index - self.config.conditions[self.block_index]), index))}
        self.presented.add(index)
        self.rec.event("stimulus_onset", onset, block=self.block_index + 1, n=self.config.conditions[self.block_index],
                       trial=index + 1, position=trial.position + 1, eligible=trial.eligible, target=trial.target,
                       planned_session_ms=self.rec.ms(planned), lateness_ms=(onset - planned) / 1e6)

    def hide_stimulus(self):
        if self.current and self.current["offset"] is None:
            self.canvas.itemconfigure(self.cells[self.current["trial"].position], fill=BACKGROUND)
            self.root.update_idletasks()
            self.current["offset"] = time.perf_counter_ns()
            self.rec.event("stimulus_offset", self.current["offset"], block=self.block_index + 1,
                           trial=self.current["trial"].index + 1,
                           visible_ms=(self.current["offset"] - self.current["onset"]) / 1e6)

    def trial_row(self, trial, status, now, planned, onset=None, offset=None, response=None):
        return {"block": self.block_index + 1, "n": self.config.conditions[self.block_index],
                "trial": trial.index + 1, "position": trial.position + 1,
                "row": trial.position // 3 + 1, "column": trial.position % 3 + 1,
                "eligible": int(trial.eligible), "target": int(trial.target), "status": status,
                "planned_onset_session_ms": self.rec.ms(planned), "onset_session_ms": self.rec.ms(onset),
                "onset_unix_ns": self.rec.clock.epoch(onset) if onset is not None else "",
                "onset_utc": utc_iso(self.rec.clock.epoch(onset)) if onset is not None else "",
                "onset_perf_ns": onset if onset is not None else "",
                "onset_lateness_ms": (onset - planned) / 1e6 if onset is not None else "",
                "offset_session_ms": self.rec.ms(offset),
                "visible_ms": (offset - onset) / 1e6 if offset is not None and onset is not None else "",
                "response_deadline_session_ms": self.rec.ms(planned + self.config.interval_ms * 1_000_000),
                "responded": int(response is not None), "response_session_ms": self.rec.ms(response),
                "response_unix_ns": self.rec.clock.epoch(response) if response is not None else "",
                "response_utc": utc_iso(self.rec.clock.epoch(response)) if response is not None else "",
                "rt_ms": (response - onset) / 1e6 if response is not None and onset is not None else "",
                "outcome": classify(trial, response is not None) if status == "completed" else status,
                "closed_session_ms": self.rec.ms(now)}

    def close_trial(self, status, now):
        self.hide_stimulus()
        data = self.current
        if status == "completed" and not data["history_valid"]:
            status = "invalid_history"
        now = max(now, data["offset"])
        row = self.trial_row(data["trial"], status, now, data["planned"], data["onset"], data["offset"], data["response"])
        self.rec.trial(row)
        self.rec.event("trial_end", now, block=row["block"], trial=row["trial"], outcome=row["outcome"], status=status)
        self.current = None

    def skip_trial(self, index, now):
        trial = self.sequences[self.block_index][index]
        planned = self.block_start + index * self.config.interval_ms * 1_000_000
        self.rec.trial(self.trial_row(trial, "skipped", now, planned))
        self.rec.event("trial_skipped", now, block=self.block_index + 1, trial=index + 1,
                       reason="presentation_window_elapsed", lateness_ms=(now - planned) / 1e6)
        self.current_index = index

    def on_space(self, event):
        at = time.perf_counter_ns()
        if not self.active:
            return
        if self.config.input_mode != "keyboard":
            return "break"
        repeated = self.key_down
        self.key_down = True
        return self.respond("keyboard", event, at, repeated)

    def respond(self, source, event, at, repeated=False):
        """Use the input handler's entry timestamp, before any scheduler/redraw work."""
        if not self.active or source != self.config.input_mode:
            return "break" if self.active else None
        if self.display_changed:
            self.abort("display_changed")
            return "break"
        self.advance(at)
        if not self.active:
            return "break"
        accepted = False
        reason = "outside_task"
        trial_number = None
        if repeated:
            reason = "key_held" if source == "keyboard" else "touch_held"
        elif self.phase == "task" and self.current:
            data = self.current
            trial_number = data["trial"].index + 1
            accepted = accepts_response(data["trial"], data["onset"], data["deadline"], at, data["response"] is not None)
            if accepted:
                data["response"] = at
                reason = "accepted"
            elif not data["trial"].eligible:
                reason = "warmup"
            elif data["response"] is not None:
                reason = "already_responded"
            else:
                reason = "outside_response_window"
        details = {"input_mode": source, "tk_event_time_ms": getattr(event, "time", None)}
        if getattr(event, "input_source", None) == "wm_pointer":
            details.update(input_source="wm_pointer", contact_id=event.contact_id,
                           native_event_time_ms=event.time, tk_event_time_ms=None)
        if source == "keyboard":
            details["key"] = "space"
        else:
            x, y = self.pointer_position(event)
            details.update(x=x, y=y, input_source=getattr(event, "input_source", "tk_pointer"))
        self.rec.event("response" if accepted else "response_ignored", at, block=self.block_index + 1,
                       trial=trial_number, reason=reason, **details)
        return "break"

    def on_release(self, event=None):
        self.key_down = False
        return "break" if self.active else None

    @staticmethod
    def touch_display_error(width, height):
        if width < 1024 or height < 768 or width <= height:
            return "タッチ操作には横向き・有効表示領域1024×768以上が必要です。画面の向きと解像度を確認してください。"
        return None

    @staticmethod
    def touch_layout(width, height, phase):
        scale = App.component_scale(width, height)
        regions = {"abort": (width - 284 * scale, 24 * scale, width - 24 * scale, 112 * scale)}
        # Keep the response target visible from Start through the first stimulus.
        # respond() still accepts/scores responses only during the task phase.
        if phase in ("ready", "prepare", "baseline", "task"):
            regions["start" if phase == "ready" else "response"] = (width / 2 - 160 * scale, height - 124 * scale,
                                                                    width / 2 + 160 * scale, height - 24 * scale)
        return regions

    def render_touch_controls(self):
        self.reset_response_feedback()
        if self.config.input_mode != "touch":
            self.touch_regions = {}
            return
        self.canvas.delete("touch-control")
        scale = self.canvas_scale()
        self.touch_regions = self.touch_layout(self.canvas.winfo_width(), self.canvas.winfo_height(), self.phase)
        for action, (left, top, right, bottom) in self.touch_regions.items():
            self.canvas.create_rectangle(left, top, right, bottom, fill="#707070", outline="#dedede", width=2 * scale,
                                         tags=("touch-control", action + "-face"))
            self.canvas.create_text((left + right) / 2, (top + bottom) / 2,
                                    text={"abort": "長押しで中止", "start": "開始", "response": "一致"}[action],
                                    fill="#ededed", font=("Yu Gothic UI", -max(1, round((18 if action == "abort" else 26) * scale)), "bold"),
                                    tags=("touch-control", action + "-label"))
        if self.abort_hold_start is not None:
            self.paint_abort_hold(max(0, (ABORT_HOLD_NS - (time.perf_counter_ns() - self.abort_hold_start)) / 1e9))

    def paint_abort_hold(self, remaining=None):
        if self.canvas is not None:
            # Do not show 0.0 while the 1.5-second threshold has not been reached.
            shown = math.ceil(max(0, remaining) * 10) / 10 if remaining is not None else None
            self.canvas.itemconfigure("abort-face", fill="#bfbfbf" if remaining is not None else "#707070")
            self.canvas.itemconfigure("abort-label", fill="#262b2a" if remaining is not None else "#ededed",
                                      text=("指をボタンへ戻す" if self.abort_outside_since is not None else f"中止まで {shown:.1f} 秒")
                                      if remaining is not None else "長押しで中止")

    def paint_response_feedback(self, pressed):
        if self.canvas is not None:
            self.canvas.itemconfigure("response-face", fill="#bfbfbf" if pressed else "#707070")
            self.canvas.itemconfigure("response-label", fill="#262b2a" if pressed else "#ededed")

    def reset_response_feedback(self):
        if self.response_feedback_timer is not None:
            self.root.after_cancel(self.response_feedback_timer)
            self.response_feedback_timer = None
        self.response_feedback_until = None
        self.paint_response_feedback(False)

    def show_response_feedback(self, at):
        self.reset_response_feedback()
        self.response_feedback_until = at + 150_000_000
        self.paint_response_feedback(True)

    def release_response_feedback(self):
        if self.response_feedback_timer is not None:
            self.root.after_cancel(self.response_feedback_timer)
            self.response_feedback_timer = None
        if self.response_feedback_until is None:
            return
        remaining = self.response_feedback_until - time.perf_counter_ns()
        if self.active and self.phase == "task" and remaining > 0:
            self.response_feedback_timer = self.root.after(math.ceil(remaining / 1e6), self.release_response_feedback)
        else:
            self.reset_response_feedback()

    def pointer_position(self, event):
        if hasattr(event, "x_root"):
            return event.x_root - self.canvas.winfo_rootx(), event.y_root - self.canvas.winfo_rooty()
        return event.x, event.y

    def touch_action_at(self, x, y):
        return next((action for action, (l, t, r, b) in self.touch_regions.items() if l <= x < r and t <= y < b), None)

    @staticmethod
    def contact_time(event):
        return event.perf_ns if event is not None and hasattr(event, "perf_ns") else time.perf_counter_ns()

    def on_native_contact(self, kind, event):
        if not self.active:
            return
        if kind == "down":
            self.on_pointer_press(event)
        elif kind == "move":
            self.on_pointer_motion(event)
        elif kind == "up":
            self.on_pointer_release(event)
        elif kind == "cancel":
            self.cancel_abort_hold("contact_cancelled", event.perf_ns)
            self.on_pointer_release()
        elif kind == "error":
            self.rec.event("touch_input_error", event.perf_ns, message=event.message)
            self.abort("touch_input_error")

    def duplicate_mouse_contact(self, event):
        if event is None or getattr(event, "input_source", None) == "wm_pointer":
            return False
        native = getattr(self, "native_touch", None)
        return ((self.pointer_down and self.pointer_source == "wm_pointer")
                or (native is not None and native.primary_id is not None))

    def on_pointer_press(self, event):
        at = self.contact_time(event)
        if not self.active or self.config.input_mode != "touch":
            return
        if self.duplicate_mouse_contact(event):
            return "break"
        x, y = self.pointer_position(event)
        action = self.touch_action_at(x, y)
        if self.pointer_down:
            if action == "response":
                self.respond("touch", event, at, repeated=True)
            return "break"
        self.pointer_down = True
        self.pointer_action = action
        self.pointer_source = getattr(event, "input_source", "tk_pointer")
        # WM_POINTER already retains the contact. A Tk mouse grab can synthesize an
        # immediate mouse release because Tk never saw a matching mouse press.
        if self.pointer_source != "wm_pointer":
            self.canvas.grab_set()
        if action == "start":
            self.start_from_instruction()
        elif action == "response":
            # Acknowledge the contact regardless of warmup, correctness or scoring acceptance.
            self.show_response_feedback(at)
            self.respond("touch", event, at)
        elif action == "abort":
            self.abort_hold_start = at
            self.abort_outside_since = None
            self.rec.event("abort_hold_start", at, phase=self.phase, required_ms=1500, x=x, y=y,
                           input_source=getattr(event, "input_source", "tk_pointer"),
                           contact_id=getattr(event, "contact_id", None),
                           move_margin_px=ABORT_MOVE_MARGIN, outside_grace_ms=250)
            self.paint_abort_hold(max(0, (ABORT_HOLD_NS - (time.perf_counter_ns() - at)) / 1e9))
            self.abort_timer = self.root.after(100, self.complete_abort_hold)
        return "break"

    def on_pointer_release(self, event=None):
        if self.active and self.duplicate_mouse_contact(event):
            return "break"
        at = self.contact_time(event)
        if self.active and self.pointer_action == "abort" and self.abort_hold_start is not None:
            # A released contact may arrive before an overdue after() callback.
            inside = self.abort_outside_since is None
            if event is not None and (hasattr(event, "x") or hasattr(event, "x_root")):
                inside = self.abort_retains_contact(*self.pointer_position(event))
            expired_outside = (self.abort_outside_since is not None
                               and at - self.abort_outside_since >= ABORT_OUTSIDE_GRACE_NS)
            if inside and not expired_outside and at - self.abort_hold_start >= ABORT_HOLD_NS:
                self.finish_abort_hold(at, "release")
                return "break"
            self.cancel_abort_hold("released_early" if inside and not expired_outside else "moved_outside", at)
        self.pointer_down = False
        self.pointer_action = None
        self.pointer_source = None
        self.release_response_feedback()
        self.cancel_abort_hold()
        if self.canvas is not None and self.canvas.grab_current() == self.canvas:
            self.canvas.grab_release()
        return "break" if self.active and self.config.input_mode == "touch" else None

    def on_pointer_motion(self, event):
        if self.active and self.duplicate_mouse_contact(event):
            return "break"
        at = self.contact_time(event)
        if self.active and self.pointer_action == "abort" and self.abort_hold_start is not None:
            inside = self.abort_retains_contact(*self.pointer_position(event))
            if self.abort_outside_since is not None and at - self.abort_outside_since >= ABORT_OUTSIDE_GRACE_NS:
                self.cancel_abort_hold("moved_outside", at)
            elif inside:
                self.abort_outside_since = None
            elif self.abort_outside_since is None:
                self.abort_outside_since = at
                self.rec.event("abort_hold_outside", at, phase=self.phase,
                               x=self.pointer_position(event)[0], y=self.pointer_position(event)[1])
        return "break" if self.active and self.config.input_mode == "touch" else None

    def abort_retains_contact(self, x, y):
        bounds = self.touch_regions.get("abort")
        if bounds is None:
            return False
        l, t, r, b = bounds
        margin = ABORT_MOVE_MARGIN * self.canvas_scale()
        return (max(0, l - margin) <= x < min(self.canvas.winfo_width(), r + margin)
                and max(0, t - margin) <= y < min(self.canvas.winfo_height(), b + margin))

    def cancel_abort_hold(self, reason=None, at=None):
        if reason is not None and self.abort_hold_start is not None:
            at = time.perf_counter_ns() if at is None else at
            self.rec.event("abort_hold_cancelled", at, phase=self.phase, reason=reason,
                           elapsed_ms=(at - self.abort_hold_start) / 1e6)
        if self.abort_timer is not None:
            self.root.after_cancel(self.abort_timer)
            self.abort_timer = None
        self.abort_hold_start = None
        self.abort_outside_since = None
        self.paint_abort_hold()

    def finish_abort_hold(self, at, trigger):
        self.rec.event("abort_hold_completed", at, phase=self.phase, trigger=trigger,
                       elapsed_ms=(at - self.abort_hold_start) / 1e6)
        self.cancel_abort_hold()
        self.abort("touch_hold")

    def complete_abort_hold(self):
        self.abort_timer = None
        if not self.active or not self.pointer_down or self.pointer_action != "abort" or self.abort_hold_start is None:
            return
        at = time.perf_counter_ns()
        if self.abort_outside_since is not None and at - self.abort_outside_since >= ABORT_OUTSIDE_GRACE_NS:
            self.cancel_abort_hold("moved_outside", at)
            return
        remaining = ABORT_HOLD_NS - (at - self.abort_hold_start)
        if remaining > 0 or self.abort_outside_since is not None:
            self.paint_abort_hold(max(0, remaining / 1e9))
            delay = min(100, math.ceil(remaining / 1e6)) if remaining > 0 else 100
            self.abort_timer = self.root.after(delay, self.complete_abort_hold)
        else:
            self.finish_abort_hold(at, "timer")

    def display_signature(self):
        return (self.canvas.winfo_width(), self.canvas.winfo_height(), self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(), float(self.root.tk.call("tk", "scaling")))

    def on_display_configure(self, event):
        if (self.active and self.config.input_mode == "touch" and self.session_display is not None
                and self.display_signature() != self.session_display):
            # Defer saving/destruction out of update_idletasks inside stimulus redraws.
            self.display_changed = True

    def check_display_changed(self):
        if self.active and self.config.input_mode == "touch" and (self.display_changed or self.display_signature() != self.session_display):
            self.rec.event("display_change", original=self.session_display, observed=self.display_signature())
            self.abort("display_changed")
            return True
        return False

    def manual_marker(self, event=None):
        if self.active:
            self.rec.event("manual_marker", phase=self.phase, block=self.block_index + 1)
            return "break"

    def on_focus_out(self, event=None):
        if self.active:
            if self.focus_timer is not None:
                self.root.after_cancel(self.focus_timer)
            self.focus_timer = self.root.after(100, self.check_focus)

    def check_focus(self):
        self.focus_timer = None
        if self.active and (self.phase in ("prepare", "baseline", "task", "rest") or self.config.input_mode == "touch") and self.root.focus_displayof() is None:
            self.abort("focus_lost")

    def abort(self, reason):
        if not self.active:
            return
        now = time.perf_counter_ns()
        self.rec.event("abort", now, reason=reason, phase=self.phase, block=self.block_index + 1)
        if self.phase == "task" and self.current:
            self.close_trial("aborted", now)
        self.finish("aborted", reason)

    def finish(self, status, reason=""):
        self.active = False
        self.key_down = False
        self.on_pointer_release()
        if self.focus_timer is not None:
            self.root.after_cancel(self.focus_timer)
            self.focus_timer = None
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.rec.finish(status, reason)
        self.root.attributes("-fullscreen", False)
        self.root.configure(cursor="", bg="#eeefed")
        self.show_results(self.rec.metadata["status"], self.rec.metadata.get("reason", reason))

    def show_results(self, status, reason):
        self.clear()
        self.phase = "results"
        self.frame = ttk.Frame(self.root)
        self.frame.pack(fill="both", expand=True)
        f = self.scroll_content(self.frame)
        self.label(f, "SESSION / SAVED", "Muted.TLabel").pack(anchor="w")
        self.label(f, "実施を終了しました" if status == "completed" else "中止して保存しました", "Title.TLabel").pack(anchor="w", pady=(8, 18))
        mode = {"experiment": "本実験", "practice": "練習", "demo": "動作確認"}[self.config.mode]
        self.label(f, f"参加者：{self.config.participant}    モード：{mode}").pack(anchor="w")
        if reason:
            reasons = {"escape": "Escキー", "focus_lost": "課題画面からフォーカスが外れたため",
                       "touch_input_error": "Windowsタッチ入力の受信エラー",
                       "touch_hold": "中止ボタンの長押し", "display_changed": "画面の向き・表示領域・倍率が変更されたため",
                       "display_unsupported": "タッチ操作の画面条件を満たしていないため",
                       "window_closed": "ウィンドウを閉じたため", "lsl_send_failed": "LSL送出エラー", "error": "アプリケーションエラー",
                       "physiology_stream_error": "OpenSignalsの生理信号の切断・時計同期・保存エラー"}
            self.label(f, "中止理由：" + reasons.get(reason, reason)).pack(anchor="w", pady=(8, 0))
        self.label(f, "成績は実験者向けです。次の課題まで参加者に見せない運用もできます。", "Muted.TLabel").pack(anchor="w", pady=(10, 15))
        table = ttk.Frame(f)
        table.pack(fill="x", pady=(0, 16))
        tree = ttk.Treeview(table, columns=("n", "count", "hit", "miss", "fa", "cr", "accuracy", "rt"), show="headings", height=4)
        for name, text, width in zip(tree["columns"], ("課題", "採点試行", "Hit", "Miss", "誤反応", "正しい棄却", "正答率", "平均RT (ms)"), (85, 95, 65, 65, 75, 100, 90, 125)):
            tree.heading(name, text=text)
            heading_width = tkfont.nametofont("TkHeadingFont").measure(text) + 24
            tree.column(name, width=max(width, heading_width), minwidth=max(width, heading_width), anchor="center", stretch=True)
        for block in range(1, len(self.config.conditions) + 1):
            rows = [r for r in self.rec.rows if r["block"] == block]
            summary = summarize(rows)
            tree.insert("", "end", values=(f"{self.config.conditions[block-1]}-back", summary["scored_trials"],
                                            summary["hit"], summary["miss"], summary["false_alarm"], summary["correct_rejection"],
                                            f"{summary['accuracy']:.1%}" if summary["accuracy"] is not None else "—",
                                            f"{summary['mean_hit_rt_ms']:.1f}" if summary["mean_hit_rt_ms"] is not None else "—"))
        tree.pack(fill="x")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
        horizontal.pack(fill="x")
        tree.configure(xscrollcommand=horizontal.set)
        skipped = sum(r["status"] == "skipped" for r in self.rec.rows)
        late = sum(float(r["onset_lateness_ms"] or 0) > 50 for r in self.rec.rows)
        self.label(f, f"提示されなかった試行：{skipped}    提示遅延が50 msを超えた試行：{late}").pack(anchor="w")
        self.label(f, "保存先", "Heading.TLabel").pack(anchor="w", pady=(25, 8))
        entry = ttk.Entry(f)
        entry.insert(0, str(self.rec.path))
        entry.configure(state="readonly")
        entry.pack(fill="x")
        self.label(f, "trials.csv：各試行・反応時間    events.csv：イベント時刻\nsession.json：実験条件    planned_trials.csv：全刺激系列    summary.json：集計\nevents.jsonl：逐次イベント記録", "Muted.TLabel").pack(anchor="w", pady=(12, 25))
        if self.rec.physiology is not None:
            self.label(f, "生理信号：physiology/continuous.csv（全区間）\nphysiology/trials/（各試行）・blocks/（各課題）・segments.csv（件数・欠測確認）", "Muted.TLabel").pack(anchor="w", pady=(0, 15))
            report = self.rec.metadata.get("physiology", {})
            summary = report.get("summary", {})
            self.label(f, f"生理信号サンプル：{summary.get('samples', '—')}    保存区間：{summary.get('segments', '—')}    不完全な区間：{summary.get('incomplete_segments', '—')}", "Muted.TLabel").pack(anchor="w", pady=(0, 8))
            if report.get("error"):
                self.label(f, "生理計測エラー：" + report["error"], "Muted.TLabel", wraplength=880).pack(anchor="w", pady=(0, 8))
        ttk.Button(f, text="保存フォルダを開く", command=lambda: os.startfile(self.rec.path)).pack(anchor="w", pady=(0, 10))
        ttk.Button(f, text="設定画面へ", style="Primary.TButton", command=self.show_setup).pack(anchor="w")

    def on_error(self, exc_type, exc, tb):
        self.active = False
        self.on_pointer_release()
        for timer in (self.timer, self.focus_timer):
            if timer is not None:
                self.root.after_cancel(timer)
        self.timer = self.focus_timer = None
        self.root.attributes("-fullscreen", False)
        self.root.configure(cursor="")
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        path = self.rec.path if self.rec is not None else APP_DIR
        try:
            (path / "error.txt").write_text(detail, encoding="utf-8")
            if self.rec is not None and not self.rec.closed:
                self.rec.finish("error", str(exc))
        except Exception:
            pass
        messagebox.showerror("実施を停止しました", f"{exc}\n\n記録済みデータ：{path}\n詳細：error.txt")
        self.show_setup()

    def on_close(self):
        self.closing = True
        if self.active:
            self.abort("window_closed")
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="空間0-back / 2-back実験")
    parser.add_argument("--demo", action="store_true", help="各12秒のGUI動作確認。mode=demoとして保存")
    args = parser.parse_args()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root, demo=args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
