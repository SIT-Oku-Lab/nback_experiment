"""Local append-only event journal and CSV exports. No network transmission."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import time
import uuid

from engine import Config, VERSION, summarize


def utc_iso(epoch_ns: int) -> str:
    return datetime.fromtimestamp(epoch_ns / 1_000_000_000, timezone.utc).isoformat(timespec="microseconds")


class Clock:
    def __init__(self):
        before = time.perf_counter_ns()
        self.epoch_ns = time.time_ns()
        after = time.perf_counter_ns()
        self.perf_ns = (before + after) // 2
        self.anchor_uncertainty_ns = (after - before) // 2

    def epoch(self, perf_ns: int) -> int:
        return self.epoch_ns + perf_ns - self.perf_ns


class LSLMarkers:
    """Optional pylsl adapter; the GUI refuses to silently fall back when selected."""
    def __init__(self, session_id: str):
        from pylsl import StreamInfo, StreamOutlet, local_clock
        info = StreamInfo("NBackMarkers", "Markers", 1, 0, "string", session_id)
        info.desc().append_child_value("application", "NBackExperiment")
        info.desc().append_child_value("version", VERSION)
        self.outlet = StreamOutlet(info)
        self.local_clock = local_clock

    def push(self, payload: str, event_perf_ns: int) -> float:
        # Map the application's monotonic event time to LSL's local clock.
        before = time.perf_counter_ns()
        lsl_now = self.local_clock()
        after = time.perf_counter_ns()
        stamp = lsl_now + (event_perf_ns - (before + after) // 2) / 1e9
        self.outlet.push_sample([payload], stamp)
        return stamp


TRIAL_FIELDS = [
    "session_id", "mode", "block", "n", "trial", "position", "row", "column",
    "eligible", "target", "status", "planned_onset_session_ms", "onset_session_ms",
    "onset_unix_ns", "onset_utc", "onset_perf_ns", "onset_lateness_ms",
    "offset_session_ms", "visible_ms", "response_deadline_session_ms", "responded",
    "response_session_ms", "response_unix_ns", "response_utc", "rt_ms", "outcome",
    "closed_session_ms",
]
EVENT_FIELDS = ["sequence", "event", "session_id", "mode", "session_ms", "perf_ns",
                "unix_ns", "utc", "observed_wall_unix_ns", "wall_clock_delta_ms",
                "lsl_timestamp", "detail_json"]


class Recorder:
    def __init__(self, parent: Path, config: Config, stream_info=None):
        self.config = config
        self.clock = Clock()
        self.session_id = uuid.uuid4().hex
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", config.participant).strip("_")[:40] or "participant"
        self.path = parent / f"{stamp}_{safe_id}_{config.mode}_{self.session_id[:8]}"
        self.path.mkdir(parents=True, exist_ok=False)
        self.lsl = None
        self.lsl_failed = False
        self.physiology = None
        self.rows: list[dict] = []
        self.event_count = 0
        self.closed = False
        self.jsonl = (self.path / "events.jsonl").open("x", encoding="utf-8", buffering=1)
        self.events_file = (self.path / "events.csv").open("x", encoding="utf-8-sig", newline="")
        self.trials_file = (self.path / "trials.csv").open("x", encoding="utf-8-sig", newline="")
        self.events = csv.DictWriter(self.events_file, EVENT_FIELDS)
        self.trials = csv.DictWriter(self.trials_file, TRIAL_FIELDS)
        self.events.writeheader()
        self.trials.writeheader()
        self.metadata = {"session_id": self.session_id, "status": "running",
                         "config": config.metadata(), "created_utc": utc_iso(self.clock.epoch_ns),
                         "clock_anchor_unix_ns": self.clock.epoch_ns,
                         "clock_anchor_perf_ns": self.clock.perf_ns,
                         "clock_anchor_uncertainty_ns": self.clock.anchor_uncertainty_ns,
                         "python": platform.python_version(), "platform": platform.platform(),
                         "timing_definition": "software redraw completion; not photon onset",
                         "positions": "1..9 row-major; 1=top-left, 5=center",
                         "reference": "https://n-back.studio-age.com/jp/"}
        self.save_metadata()
        try:
            if config.physiology:
                if stream_info is None or stream_info.uid() != config.physiology_uid:
                    raise ValueError("選択したOpenSignalsの信号が見つかりません。再検索してください。")
                if config.ecg_channel > 0 and config.ecg_channel > stream_info.channel_count():
                    raise ValueError("指定したECG列が信号のチャンネル数を超えています。LSL列番号を確認してください。")
                from physiology import PhysiologyCapture
                self.physiology = PhysiologyCapture(self.path, self.clock, stream_info)
                self.physiology.wait_ready()
                self.metadata["physiology"] = {"stream": self.physiology.stream, "folder": "physiology"}
                self.save_metadata()
            if config.lsl:
                self.lsl = LSLMarkers(self.session_id)
        except Exception as exc:
            if self.physiology is not None:
                self.physiology.finish(time.perf_counter_ns())
            self.metadata["status"] = "initialization_failed"
            self.metadata["reason"] = str(exc)
            self.save_metadata()
            self.close_files()
            raise
        self.event("session_start", config=config.metadata())

    def ms(self, perf_ns: int | None) -> float | str:
        return "" if perf_ns is None else round((perf_ns - self.clock.perf_ns) / 1e6, 6)

    def save_metadata(self):
        temporary = self.path / "session.json.tmp"
        temporary.write_text(json.dumps(self.metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path / "session.json")

    def event(self, name: str, at: int | None = None, **detail):
        if self.closed:
            return
        at = time.perf_counter_ns() if at is None else at
        if self.physiology is not None:
            if name == "block_start":
                self.physiology.block_start(detail["block"], detail["n"], at,
                                            at + detail["planned_duration_ms"] * 1_000_000)
            elif name == "block_end":
                planned_end = self.clock.perf_ns + round(detail["planned_end_session_ms"] * 1e6)
                self.physiology.block_end(detail["block"], planned_end, "completed")
            elif name == "abort" and detail.get("phase") == "task":
                self.physiology.block_end(detail["block"], at, "aborted")
        epoch = self.clock.epoch(at)
        wall = time.time_ns()
        observed_at = time.perf_counter_ns()
        self.event_count += 1
        row = {"sequence": self.event_count, "event": name, "session_id": self.session_id,
               "mode": self.config.mode, "session_ms": self.ms(at), "perf_ns": at,
               "unix_ns": epoch, "utc": utc_iso(epoch), "observed_wall_unix_ns": wall,
               "wall_clock_delta_ms": round((wall - self.clock.epoch(observed_at)) / 1e6, 3),
               "lsl_timestamp": "", "detail_json": json.dumps(detail, ensure_ascii=False)}
        if self.lsl is not None and not self.lsl_failed:
            try:
                payload = json.dumps({"event": name, "sequence": self.event_count,
                                      "session_id": self.session_id, **detail}, ensure_ascii=False)
                row["lsl_timestamp"] = self.lsl.push(payload, at)
            except Exception as exc:
                self.lsl_failed = True
                row["detail_json"] = json.dumps({**detail, "lsl_error": str(exc)}, ensure_ascii=False)
        self.jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.events.writerow(row)
        self.events_file.flush()

    def trial(self, row: dict):
        row = {"session_id": self.session_id, "mode": self.config.mode, **row}
        self.trials.writerow({field: row.get(field, "") for field in TRIAL_FIELDS})
        self.trials_file.flush()
        self.rows.append(row)
        if self.physiology is not None:
            self.physiology.trial(row)

    def physiology_error(self):
        return self.physiology.check_health() if self.physiology is not None else ""

    def checkpoint(self):
        for handle in (self.jsonl, self.events_file, self.trials_file):
            handle.flush()
            os.fsync(handle.fileno())

    def finish(self, status: str, reason: str = ""):
        if self.closed:
            return
        if self.physiology is not None:
            self.metadata["physiology"] = self.physiology.finish(time.perf_counter_ns())
            if self.metadata["physiology"]["error"] and status == "completed":
                status, reason = "error", self.metadata["physiology"]["error"]
            self.event("physiology_saved", **self.metadata["physiology"])
            from ecg_analysis import analyze_recording, write_json
            try:
                if not self.metadata["physiology"]["finished"]:
                    raise RuntimeError("生理信号のファイルが確定していないためECG解析を実行しません。")
                self.metadata["ecg"] = analyze_recording(self.path, self.physiology.stream, self.config.ecg_channel)
            except Exception as exc:
                # Derived metrics must never destroy or invalidate the saved raw signal.
                self.metadata["ecg"] = {"status": "failed", "reason": str(exc), "folder": "physiology/ecg"}
                try:
                    folder = self.path / "physiology" / "ecg"
                    folder.mkdir(parents=True, exist_ok=True)
                    write_json(folder / "report.json", self.metadata["ecg"])
                except OSError:
                    pass
            self.event("ecg_analysis_saved", **self.metadata["ecg"])
        self.event("session_end", status=status, reason=reason)
        self.metadata.update(status=status, reason=reason, ended_utc=utc_iso(time.time_ns()),
                             event_count=self.event_count, lsl_failed=self.lsl_failed)
        self.save_metadata()
        blocks = sorted({row["block"] for row in self.rows})
        summary = [{"block": b, "n": next(r["n"] for r in self.rows if r["block"] == b),
                    **summarize([r for r in self.rows if r["block"] == b])} for b in blocks]
        (self.path / "summary.json").write_text(
            json.dumps({"session_id": self.session_id, "status": status,
                        "blocks": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.checkpoint()
        self.close_files()

    def close_files(self):
        self.closed = True
        for handle in (self.jsonl, self.events_file, self.trials_file):
            handle.close()
