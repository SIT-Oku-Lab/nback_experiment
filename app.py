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
import traceback

from engine import (BACKGROUND, FIXATION, GRID, STIMULUS, INTERVALS, VERSION,
                    Config, accepts_response, classify, make_sequence, scheduled_index, summarize)
from storage import Recorder, utc_iso

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ORDERS = {"0-back → 2-back": (0, 2), "2-back → 0-back": (2, 0),
          "0-back のみ": (0,), "2-back のみ": (2,)}
POSITIONS = ("1 左上", "2 上", "3 右上", "4 左", "5 中央", "6 右", "7 左下", "8 下", "9 右下")


class App:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.demo = demo
        self.root.title("N-back 実験 | 0-back / 2-back")
        self.root.geometry("1020x850")
        self.root.minsize(920, 760)
        self.root.configure(bg="#eeefed")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Yu Gothic UI", 10))
        style.configure("TFrame", background="#eeefed")
        style.configure("TLabel", background="#eeefed", foreground="#262b2a", font=("Yu Gothic UI", 11))
        style.configure("Muted.TLabel", foreground="#606763", font=("Yu Gothic UI", 10))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 25, "bold"))
        style.configure("Heading.TLabel", font=("Yu Gothic UI", 13, "bold"))
        style.configure("TButton", font=("Yu Gothic UI", 11), padding=(16, 10))
        style.configure("Primary.TButton", foreground="#ffffff", background="#424a45", padding=(20, 13))
        style.map("Primary.TButton", background=[("active", "#303a33")])
        style.configure("TCombobox", padding=5)
        style.configure("TEntry", padding=5)
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
        self.block_index = 0
        self.current_index = -1
        self.current = None
        self.output = tk.StringVar(value=str(APP_DIR / "results"))
        self.participant = tk.StringVar(value="P001")
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
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.on_error
        self.show_setup()

    def clear(self):
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None
        if self.canvas is not None:
            self.canvas.destroy()
            self.canvas = None

    def label(self, parent, text, style="TLabel", **kwargs):
        widget = ttk.Label(parent, text=text, style=style, **kwargs)
        return widget

    def show_setup(self):
        self.clear()
        self.phase = "setup"
        self.root.attributes("-fullscreen", False)
        self.root.configure(cursor="", bg="#eeefed")
        self.frame = ttk.Frame(self.root)
        self.frame.pack(fill="both", expand=True)
        footer = ttk.Frame(self.frame, padding=(36, 12, 36, 18))
        footer.pack(side="bottom", fill="x")
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")
        self.start_buttons = []
        for text, practice in (("本実験を開始" if not self.demo else "動作確認を開始", False), ("練習する（各60秒）", True)):
            button = ttk.Button(buttons, text=text, style="TButton" if practice else "Primary.TButton", command=lambda p=practice: self.start(p))
            button.pack(side="left", padx=(0, 10))
            self.start_buttons.append(button)
        ttk.Button(buttons, text="手順書を開く", command=self.open_manual).pack(side="right")
        self.label(footer, "Enter：説明から進む    Space：一致に応答    Esc：中止して保存    M：手動時刻マーカー", "Muted.TLabel").pack(anchor="w", pady=(10, 0))
        scroll_area = ttk.Frame(self.frame)
        scroll_area.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(scroll_area, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.setup_scroll = tk.Canvas(scroll_area, bg="#eeefed", highlightthickness=0, yscrollcommand=scrollbar.set)
        self.setup_scroll.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.setup_scroll.yview)
        f = ttk.Frame(self.setup_scroll, padding=(36, 25, 25, 8))
        content = self.setup_scroll.create_window(0, 0, window=f, anchor="nw")
        f.bind("<Configure>", lambda e: self.setup_scroll.configure(scrollregion=self.setup_scroll.bbox("all")))
        self.setup_scroll.bind("<Configure>", lambda e: self.setup_scroll.itemconfigure(content, width=e.width))
        self.label(f, "N-BACK  /  EXPERIMENT", "Muted.TLabel").pack(anchor="w")
        self.label(f, "空間 n-back 課題", "Title.TLabel").pack(anchor="w", pady=(3, 6))
        self.label(f, "0-back と 2-back を、同じ刺激画面・同じ応答方法で実施します。", "Muted.TLabel").pack(anchor="w")
        self.label(f, f"v{VERSION}  ·  OpenSignals 生理信号の同期受信・試行別保存", "Muted.TLabel").pack(anchor="w", pady=(3, 0))
        lsl_available = importlib.util.find_spec("pylsl") is not None
        sensor_frame = ttk.Frame(f)
        sensor_frame.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(sensor_frame, text="OpenSignalsの生理信号を同期受信して自動保存する",
                        variable=self.physiology_enabled, state="normal" if lsl_available else "disabled").pack(anchor="w")
        stream_row = ttk.Frame(sensor_frame)
        stream_row.pack(fill="x", pady=(7, 5))
        self.stream_combo = ttk.Combobox(stream_row, textvariable=self.stream_selection,
                                        values=tuple(self.streams), width=65, state="readonly")
        self.stream_combo.pack(side="left", fill="x", expand=True)
        self.discover_button = ttk.Button(stream_row, text="信号を検索", command=self.discover)
        self.discover_button.pack(side="left", padx=(8, 0))
        if not lsl_available:
            self.discover_button.configure(state="disabled")
            self.sensor_status.set("ソース版で生理信号を受信するにはpylslが必要です。配布exeには同梱されています。")
        ttk.Label(sensor_frame, textvariable=self.sensor_status, style="Muted.TLabel", wraplength=880).pack(anchor="w")
        self.label(sensor_frame, "各刺激試行の終了後と各10分課題の終了後に自動保存します。LabRecorderは不要です。", "Muted.TLabel").pack(anchor="w", pady=(3, 0))
        if self.demo:
            self.label(f, "動作確認モード：各12秒。本実験データとは別に保存されます。", "Heading.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Separator(f).pack(fill="x", pady=18)
        columns = ttk.Frame(f)
        columns.pack(fill="x")
        left, right = ttk.Frame(columns), ttk.Frame(columns, padding=(35, 0, 0, 0))
        left.grid(row=0, column=0, sticky="nw")
        right.grid(row=0, column=1, sticky="nw")
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)
        self.label(left, "実施の設定", "Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.form_row(left, 1, "参加者ID", self.participant)
        self.form_row(left, 2, "課題の順序", self.order, tuple(ORDERS), width=20)
        self.label(left, "各課題の時間").grid(row=3, column=0, sticky="w", pady=7, padx=(0, 15))
        self.label(left, "各12秒（動作確認）" if self.demo else "各10分", "Heading.TLabel").grid(row=3, column=1, sticky="w")
        self.form_row(left, 4, "開始前の安静（秒）", self.baseline, width=11)
        self.form_row(left, 5, "課題間の休憩（秒）", self.rest, width=11)
        self.label(left, "単独課題を選んだ場合は10分です。\n練習は選択した各課題を60秒ずつ行います。", "Muted.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.label(right, "参加者の画面", "Heading.TLabel").pack(anchor="w")
        preview = tk.Canvas(right, width=210, height=160, bg=BACKGROUND, highlightthickness=0)
        preview.pack(anchor="w", pady=(12, 10))
        self.draw_grid(preview, 105, 80, 126, sample=1)
        self.label(right, "グレー背景・固定グリッド・位置刺激のみ。\n得点、残り時間、正誤表示、効果音はありません。\n一致したときだけ Space キーを押します。", "Muted.TLabel", wraplength=350).pack(anchor="w")
        ttk.Separator(f).pack(fill="x", pady=18)
        self.label(f, "刺激・記録の設定", "Heading.TLabel").pack(anchor="w", pady=(0, 8))
        advanced = ttk.Frame(f)
        advanced.pack(fill="x")
        self.form_row(advanced, 0, "提示間隔（ms）", self.interval, tuple(map(str, INTERVALS)), width=10)
        self.form_row(advanced, 1, "刺激提示（ms）", self.stimulus, width=10)
        self.form_row(advanced, 2, "ターゲット率（%）", self.target_percent, width=10)
        self.form_row(advanced, 0, "0-back の指定位置", self.target_position, POSITIONS, width=12, col=2)
        self.form_row(advanced, 1, "乱数シード", self.seed, width=12, col=2)
        ttk.Button(advanced, text="新しいシード", command=lambda: self.seed.set(str(secrets.randbelow(2**32)))).grid(row=2, column=3, sticky="w", pady=3)
        self.label(f, "1試行 = 提示間隔。刺激が消えた後も、次の刺激が出る直前まで応答できます。", "Muted.TLabel").pack(anchor="w", pady=(7, 8))
        ttk.Checkbutton(f, text="追加でLSLマーカーも送出する（任意：LabRecorder等で別途記録する場合）",
                        variable=self.lsl_enabled, state="normal" if lsl_available else "disabled").pack(anchor="w")
        self.label(f, "BITalino / OpenSignals 対応手順は README_ja.md を参照。" +
                   ("" if lsl_available else " LSL利用時は pylsl の追加が必要です。"), "Muted.TLabel").pack(anchor="w", pady=(3, 8))
        output_row = ttk.Frame(f)
        output_row.pack(fill="x", pady=(0, 12))
        self.label(output_row, "保存先").pack(side="left", padx=(0, 12))
        ttk.Entry(output_row, textvariable=self.output).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="変更", command=self.choose_output).pack(side="left", padx=(8, 0))

    def on_wheel(self, event):
        if self.phase == "setup":
            self.setup_scroll.yview_scroll(-int(event.delta / 120), "units")

    def form_row(self, parent, row, title, variable, values=None, width=18, col=0):
        self.label(parent, title).grid(row=row, column=col, sticky="w", padx=(0 if col == 0 else 40, 12), pady=5)
        if values:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, width=width, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=variable, width=width)
        widget.grid(row=row, column=col + 1, sticky="w", pady=5)

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
        self.block_index = 0
        self.current = None
        self.presented = set()
        self.rec.metadata.update(display={"width_px": self.canvas.winfo_width(), "height_px": self.canvas.winfo_height(),
                                          "tk_scaling": self.root.tk.call("tk", "scaling"),
                                          "grid_size_px": 360, "gap_px": 8})
        self.rec.save_metadata()
        self.show_instruction()
        self.schedule_tick()

    def show_instruction(self):
        self.phase = "ready"
        self.rec.event("instruction_start", block=self.block_index + 1, n=self.config.conditions[self.block_index])
        c = self.canvas
        c.delete("all")
        x, y = c.winfo_width() / 2, c.winfo_height() / 2
        n = self.config.conditions[self.block_index]
        c.create_text(x, y - 230, text=f"{n}-back", font=("Yu Gothic UI", 32, "bold"), fill="#ededed")
        if n == 0:
            text = f"指定された位置（{POSITIONS[self.config.zero_target]}）に出たときだけ\nSpace キーを1回押してください。"
            self.draw_grid(c, x, y - 20, 156, sample=self.config.zero_target)
        else:
            text = "現在の位置が「2つ前」と同じときだけ\nSpace キーを1回押してください。最初の2試行は見るだけです。"
            self.draw_grid(c, x, y - 20, 156)
        c.create_text(x, y - 155, text=text, justify="center", font=("Yu Gothic UI", 16), fill="#ededed")
        duration = self.config.duration_ms // 1000
        duration_text = "10分間" if duration == 600 else f"{duration}秒間"
        c.create_text(x, y + 108, text=f"{duration_text}、位置を見て判断してください。", font=("Yu Gothic UI", 14), fill="#ededed")
        c.create_text(x, y + 168, text="準備ができたら Enter", font=("Yu Gothic UI", 18, "bold"), fill="#ededed")
        c.create_text(x, y + 212, text=f"{self.config.prepare_s}秒の準備後に開始します。中止は Esc。", font=("Yu Gothic UI", 11), fill="#e0e0e0")
        if self.block_index == 0 and self.config.baseline_s:
            c.create_text(x, y + 245, text=f"最初に{self.config.baseline_s}秒間、中央を見て安静にしてください。", font=("Yu Gothic UI", 11), fill="#e0e0e0")
        self.root.update_idletasks()

    @staticmethod
    def draw_grid(canvas, x, y, size, sample=None):
        gap = 8 if size >= 300 else 4
        cell = (size - gap * 2) / 3
        items = []
        for position in range(9):
            col, row = position % 3, position // 3
            left = x - size / 2 + col * (cell + gap)
            top = y - size / 2 + row * (cell + gap)
            item = canvas.create_rectangle(left, top, left + cell, top + cell, outline=GRID,
                                           width=1, fill=STIMULUS if position == sample else BACKGROUND)
            items.append(item)
        canvas.create_line(x - 6, y, x + 6, y, fill=FIXATION, width=2)
        canvas.create_line(x, y - 6, x, y + 6, fill=FIXATION, width=2)
        return items

    def neutral_screen(self):
        self.canvas.delete("all")
        self.cells = self.draw_grid(self.canvas, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, 360)
        self.root.update_idletasks()

    def on_enter(self, event=None):
        if self.active and self.phase == "ready":
            self.rec.event("instruction_end", block=self.block_index + 1)
            self.neutral_screen()
            self.begin_phase("prepare", self.config.prepare_s)
        return "break" if self.active else None

    def begin_phase(self, phase: str, seconds: float, at=None):
        self.phase = phase
        now = time.perf_counter_ns() if at is None else at
        self.phase_end = now + round(seconds * 1e9)
        self.rec.event(phase + "_start", now, block=self.block_index + 1, duration_s=seconds)
        self.rec.checkpoint()

    def schedule_tick(self):
        if self.active:
            self.timer = self.root.after(5, self.tick)

    def tick(self):
        self.timer = None
        if not self.active:
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

    def begin_block(self):
        self.phase = "task"
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
        if not self.active:
            return
        at = time.perf_counter_ns()
        repeated = self.key_down
        self.key_down = True
        self.advance(at)
        if not self.active:
            return "break"
        accepted = False
        reason = "outside_task"
        trial_number = None
        if repeated:
            reason = "key_held"
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
        self.rec.event("response" if accepted else "response_ignored", at, block=self.block_index + 1,
                       trial=trial_number, key="space", reason=reason, tk_event_time_ms=getattr(event, "time", None))
        return "break"

    def on_release(self, event=None):
        self.key_down = False
        return "break" if self.active else None

    def manual_marker(self, event=None):
        if self.active:
            self.rec.event("manual_marker", phase=self.phase, block=self.block_index + 1)
            return "break"

    def on_focus_out(self, event=None):
        if self.active:
            self.root.after(100, self.check_focus)

    def check_focus(self):
        if self.active and self.phase in ("prepare", "baseline", "task", "rest") and self.root.focus_displayof() is None:
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
        self.frame = ttk.Frame(self.root, padding=(45, 40))
        self.frame.pack(fill="both", expand=True)
        f = self.frame
        self.label(f, "SESSION / SAVED", "Muted.TLabel").pack(anchor="w")
        self.label(f, "実施を終了しました" if status == "completed" else "中止して保存しました", "Title.TLabel").pack(anchor="w", pady=(8, 18))
        mode = {"experiment": "本実験", "practice": "練習", "demo": "動作確認"}[self.config.mode]
        self.label(f, f"参加者：{self.config.participant}    モード：{mode}").pack(anchor="w")
        if reason:
            reasons = {"escape": "Escキー", "focus_lost": "課題画面からフォーカスが外れたため",
                       "window_closed": "ウィンドウを閉じたため", "lsl_send_failed": "LSL送出エラー", "error": "アプリケーションエラー",
                       "physiology_stream_error": "OpenSignalsの生理信号の切断・時計同期・保存エラー"}
            self.label(f, "中止理由：" + reasons.get(reason, reason)).pack(anchor="w", pady=(8, 0))
        self.label(f, "成績は実験者向けです。次の課題まで参加者に見せない運用もできます。", "Muted.TLabel").pack(anchor="w", pady=(10, 15))
        tree = ttk.Treeview(f, columns=("n", "count", "hit", "miss", "fa", "cr", "accuracy", "rt"), show="headings", height=4)
        for name, text, width in zip(tree["columns"], ("課題", "採点試行", "Hit", "Miss", "誤反応", "正しい棄却", "正答率", "平均RT (ms)"), (85, 95, 65, 65, 75, 100, 90, 125)):
            tree.heading(name, text=text)
            tree.column(name, width=width, anchor="center", stretch=True)
        for block in range(1, len(self.config.conditions) + 1):
            rows = [r for r in self.rec.rows if r["block"] == block]
            summary = summarize(rows)
            tree.insert("", "end", values=(f"{self.config.conditions[block-1]}-back", summary["scored_trials"],
                                            summary["hit"], summary["miss"], summary["false_alarm"], summary["correct_rejection"],
                                            f"{summary['accuracy']:.1%}" if summary["accuracy"] is not None else "—",
                                            f"{summary['mean_hit_rt_ms']:.1f}" if summary["mean_hit_rt_ms"] is not None else "—"))
        tree.pack(fill="x", pady=(0, 16))
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
