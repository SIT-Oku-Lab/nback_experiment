"""OpenSignals LSL reception and incremental, time-aligned physiological CSVs.

Only the acquisition thread owns these files. Tk sends small boundary messages;
signal reception, trial export and fsync never run in the stimulus callback.
"""
from __future__ import annotations

from collections import deque
import csv
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import threading
import time


def describe_stream(info) -> dict:
    return {"uid": info.uid(), "name": info.name(), "type": info.type(),
            "source_id": info.source_id(), "hostname": info.hostname(),
            "channel_count": info.channel_count(), "nominal_srate": info.nominal_srate()}


def discover_streams(wait_s=2.0):
    from pylsl import cf_string, resolve_streams
    # OpenSignals identifies the device MAC in type, not necessarily source_id.
    return sorted((info for info in resolve_streams(wait_s)
                   if info.channel_format() != cf_string and info.nominal_srate() > 0
                   and info.channel_count() > 0),
                  key=lambda info: (info.name().lower() != "opensignals", info.name(), info.type(), info.uid()))


@dataclass
class Sample:
    index: int
    perf_ns: int
    remote_s: float
    correction_s: float
    local_s: float
    values: list


INDEX_FIELDS = ["kind", "block", "n", "trial", "status", "start_session_ms", "end_session_ms",
                "samples", "expected_samples", "coverage_ratio", "gap_count", "max_gap_ms",
                "quality", "finalized_after_timeout", "file"]


class SegmentWriter:
    """Single-threaded disk writer, separately testable without LSL or Tk."""
    def __init__(self, parent: Path, clock, stream: dict, settle_s=1.0):
        self.path = parent / "physiology"
        self.path.mkdir(exist_ok=False)
        (self.path / "trials").mkdir()
        (self.path / "blocks").mkdir()
        self.clock = clock
        self.stream = stream
        self.rate = float(stream["nominal_srate"])
        self.settle_s = settle_s
        self.header = ["sample_index", "lsl_remote_s", "clock_correction_s", "lsl_local_s",
                       "session_ms", "unix_ns"] + [f"ch_{i+1}" for i in range(stream["channel_count"])]
        self.raw_handle = (self.path / "continuous.csv").open("x", encoding="utf-8-sig", newline="")
        self.raw = csv.writer(self.raw_handle)
        self.raw.writerow(self.header)
        self.index_handle = (self.path / "segments.csv").open("x", encoding="utf-8-sig", newline="")
        self.index = csv.DictWriter(self.index_handle, INDEX_FIELDS)
        self.index.writeheader()
        self.buffer: deque[Sample] = deque()
        self.blocks = {}
        self.pending = []
        self.count = 0
        self.watermark = None
        self.last_remote = None
        self.gaps = 0
        self.max_gap_ns = 0
        self.saved_segments = []
        self.closed = False
        self.write_json(self.path / "stream.json", stream)

    @staticmethod
    def write_json(path, value):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def ms(self, at):
        return round((at - self.clock.perf_ns) / 1e6, 6)

    def row(self, sample):
        return [sample.index, sample.remote_s, sample.correction_s, sample.local_s,
                self.ms(sample.perf_ns), self.clock.epoch(sample.perf_ns), *sample.values]

    def perf(self, session_ms):
        return self.clock.perf_ns + round(float(session_ms) * 1e6)

    @property
    def gap_limit_ns(self):
        return max(100_000_000, round(5e9 / self.rate))

    def add(self, sample: Sample):
        if len(sample.values) != self.stream["channel_count"]:
            raise ValueError("生理信号のチャンネル数が途中で変わりました。")
        if self.last_remote is not None and sample.remote_s <= self.last_remote:
            raise ValueError("生理信号の送信時刻が逆行・重複しました。")
        if self.watermark is not None:
            gap = sample.perf_ns - self.watermark
            if gap <= 0:
                raise ValueError("時計補正後の生理信号時刻が逆行・重複しました。")
            self.max_gap_ns = max(self.max_gap_ns, gap)
            self.gaps += gap > self.gap_limit_ns
        self.raw.writerow(self.row(sample))
        self.count += 1
        self.last_remote = sample.remote_s
        self.watermark = sample.perf_ns
        self.buffer.append(sample)
        while self.buffer and self.buffer[0].perf_ns < sample.perf_ns - 30_000_000_000:
            self.buffer.popleft()
        for block in self.blocks.values():
            self.append_block(block, sample)

    def append_block(self, block, sample):
        if block["start"] <= sample.perf_ns < block["end"] and sample.index > block["last_index"]:
            block["writer"].writerow(self.row(sample))
            block["last_index"] = sample.index
            block["sample_count"] += 1
            if block["first"] is None:
                block["first"] = sample.perf_ns
            if block["last"] is not None:
                gap = sample.perf_ns - block["last"]
                block["max_gap"] = max(block["max_gap"], gap)
                block["gaps"] += gap > self.gap_limit_ns
            block["last"] = sample.perf_ns

    def start_block(self, block, n, start, end):
        path = self.path / "blocks" / f"block_{block:02d}_{n}back.csv"
        handle = path.with_suffix(".csv.part").open("x", encoding="utf-8-sig", newline="")
        writer = csv.writer(handle)
        writer.writerow(self.header)
        window = {"kind": "block", "block": block, "n": n, "trial": "", "start": start,
                  "end": end, "status": "running", "handle": handle, "writer": writer,
                  "last_index": -1, "sample_count": 0, "first": None, "last": None,
                  "gaps": 0, "max_gap": 0, "file": str(path.relative_to(self.path)).replace("\\", "/")}
        self.blocks[block] = window
        for sample in self.buffer:
            self.append_block(window, sample)
        handle.flush()

    def end_block(self, block, end, status):
        window = self.blocks.get(block)
        if window is not None:
            window.update(end=min(end, window["end"]), status=status, submitted=time.monotonic())

    def trial(self, row):
        start = self.perf(row["planned_onset_session_ms"])
        end = min(self.perf(row["response_deadline_session_ms"]), self.perf(row["closed_session_ms"]))
        self.pending.append({"kind": "trial", "block": row["block"], "n": row["n"], "trial": row["trial"],
                             "start": start, "end": max(start, end), "status": row["status"],
                             "task": dict(row), "submitted": time.monotonic()})

    def quality(self, window, count, first, last, gaps, max_gap, timeout):
        expected = max(0, round((window["end"] - window["start"]) / 1e9 * self.rate))
        coverage = count / expected if expected else None
        edges_ok = (first is not None and last is not None
                    and first - window["start"] <= self.gap_limit_ns
                    and window["end"] - last <= self.gap_limit_ns)
        quality = ("complete" if count and coverage is not None and .95 <= coverage <= 1.05
                   and edges_ok and not gaps and not timeout else "incomplete")
        return {"kind": window["kind"], "block": window["block"], "n": window["n"],
                "trial": window["trial"], "status": window["status"],
                "start_session_ms": self.ms(window["start"]), "end_session_ms": self.ms(window["end"]),
                "samples": count, "expected_samples": expected, "coverage_ratio": coverage,
                "gap_count": gaps, "max_gap_ms": max_gap / 1e6,
                "quality": quality, "finalized_after_timeout": timeout, "file": window["file"]}

    def save_trial(self, window, timeout):
        path = self.path / "trials" / f"block_{window['block']:02d}_trial_{window['trial']:04d}.csv"
        window["file"] = str(path.relative_to(self.path)).replace("\\", "/")
        samples = [s for s in self.buffer if window["start"] <= s.perf_ns < window["end"]]
        onset = window["task"].get("onset_session_ms", "")
        with path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.header + ["relative_trial_ms", "relative_stimulus_ms"])
            for sample in samples:
                writer.writerow(self.row(sample) + [(sample.perf_ns - window["start"]) / 1e6,
                                self.ms(sample.perf_ns) - float(onset) if onset != "" else ""])
            handle.flush()
            os.fsync(handle.fileno())
        gaps = [b.perf_ns - a.perf_ns for a, b in zip(samples, samples[1:])]
        record = self.quality(window, len(samples), samples[0].perf_ns if samples else None,
                              samples[-1].perf_ns if samples else None,
                              sum(gap > self.gap_limit_ns for gap in gaps), max(gaps, default=0), timeout)
        self.write_json(path.with_suffix(".json"), {**record, "task": window["task"],
                        "window_reference": "planned onset inclusive, deadline exclusive; truncated on abort"})
        self.record_segment(record)

    def record_segment(self, record):
        self.index.writerow(record)
        self.index_handle.flush()
        self.raw_handle.flush()
        os.fsync(self.index_handle.fileno())
        os.fsync(self.raw_handle.fileno())
        self.saved_segments.append(record)

    def finalize_block_file(self, window):
        path = self.path / window["file"]
        partial = path.with_suffix(".csv.part")
        # An abort may arrive after samples beyond its boundary were appended.
        # Re-filter only this exceptional case; normal blocks simply rename.
        if window["last"] is not None and window["last"] >= window["end"]:
            temporary = path.with_suffix(".csv.tmp")
            count, first, last, gaps, max_gap = 0, None, None, 0, 0
            with partial.open(encoding="utf-8-sig", newline="") as source, temporary.open("x", encoding="utf-8-sig", newline="") as target:
                reader, output = csv.reader(source), csv.writer(target)
                output.writerow(next(reader))
                for row in reader:
                    perf = self.perf(row[4])
                    if window["start"] <= perf < window["end"]:
                        output.writerow(row)
                        count += 1
                        first = perf if first is None else first
                        if last is not None:
                            gap = perf - last
                            gaps += gap > self.gap_limit_ns
                            max_gap = max(max_gap, gap)
                        last = perf
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(path)
            partial.unlink()
            window.update(sample_count=count, first=first, last=last, gaps=gaps, max_gap=max_gap)
        else:
            partial.replace(path)

    def finalize_ready(self, force=False):
        now = time.monotonic()
        for window in self.pending[:]:
            covered = self.watermark is not None and self.watermark >= window["end"]
            if force or covered or now - window["submitted"] >= self.settle_s:
                self.save_trial(window, timeout=not covered)
                self.pending.remove(window)
        for block, window in list(self.blocks.items()):
            if window["status"] == "running":
                continue
            covered = self.watermark is not None and self.watermark >= window["end"]
            if force or covered or now - window["submitted"] >= self.settle_s:
                window["handle"].flush()
                os.fsync(window["handle"].fileno())
                window["handle"].close()
                self.finalize_block_file(window)
                record = self.quality(window, window["sample_count"], window["first"], window["last"],
                                      window["gaps"], window["max_gap"], timeout=not covered)
                self.write_json((self.path / window["file"]).with_suffix(".json"), record)
                self.record_segment(record)
                del self.blocks[block]

    def close(self, cut_perf_ns, error=""):
        if self.closed:
            return
        try:
            for block, window in list(self.blocks.items()):
                if window["status"] == "running":
                    self.end_block(block, cut_perf_ns, "aborted")
            self.finalize_ready(force=True)
            report = {"samples": self.count, "gap_count": self.gaps, "max_gap_ms": self.max_gap_ns / 1e6,
                      "error": error, "segments": len(self.saved_segments),
                      "incomplete_segments": sum(s["quality"] != "complete" for s in self.saved_segments),
                      "cut_session_ms": self.ms(cut_perf_ns)}
            self.write_json(self.path / "summary.json", report)
        finally:
            self.closed = True
            for block in self.blocks.values():
                if not block["handle"].closed:
                    block["handle"].close()
            self.raw_handle.close()
            self.index_handle.close()


class PhysiologyCapture:
    def __init__(self, parent, clock, info):
        self.parent, self.clock, self.info = parent, clock, info
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.error = ""
        self.last_arrival_ns = None
        self.stream = describe_stream(info)
        self.thread = threading.Thread(target=self.run, name="OpenSignals-acquisition", daemon=True)
        self.thread.start()

    def wait_ready(self, timeout=10):
        if not self.ready.wait(timeout):
            self.cancel.set()
            self.thread.join(timeout=2)
            raise RuntimeError("OpenSignalsから生理信号を受信できません。計測開始とLSL設定を確認してください。")
        if self.error:
            raise RuntimeError(self.error)

    def check_health(self):
        if self.error:
            return self.error
        if self.last_arrival_ns is not None and time.perf_counter_ns() - self.last_arrival_ns > 2_000_000_000:
            return "OpenSignalsの生理信号が2秒以上途絶えました。"
        return ""

    def block_start(self, block, n, start, end):
        self.commands.put(("block_start", (block, n, start, end)))

    def block_end(self, block, end, status):
        self.commands.put(("block_end", (block, end, status)))

    def trial(self, row):
        self.commands.put(("trial", (dict(row),)))

    def finish(self, cut_perf_ns):
        self.commands.put(("finish", (cut_perf_ns,)))
        if not self.done.wait(8):
            self.cancel.set()
            self.error = self.error or "生理計測データの保存処理が時間内に終了しませんでした。"
        report_path = self.parent / "physiology" / "summary.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        return {"stream": self.stream, "error": self.error,
                "finished": self.done.is_set(), "folder": "physiology", "summary": report}

    def run(self):
        inlet = None
        writer = None
        finish_cut = None
        finish_deadline = None
        startup_deadline = time.monotonic() + 8
        correction_at = 0
        try:
            from pylsl import StreamInlet, local_clock
            inlet = StreamInlet(self.info, max_buflen=60, recover=False, processing_flags=0)
            inlet.open_stream(timeout=3)
            full_info = inlet.info(timeout=3)
            channel = full_info.desc().child("channels").child("channel")
            channels = []
            for index in range(full_info.channel_count()):
                channels.append({"column": f"ch_{index+1}", "label": channel.child_value("label"),
                                 "unit": channel.child_value("unit")})
                channel = channel.next_sibling()
            before = time.perf_counter_ns()
            lsl_anchor = local_clock()
            perf_anchor = (before + time.perf_counter_ns()) // 2
            correction = inlet.time_correction(timeout=3)
            self.stream.update(channels=channels, xml=full_info.as_xml(), clock_correction="remote + correction = local LSL",
                               lsl_anchor=lsl_anchor, perf_anchor_ns=perf_anchor)
            writer = SegmentWriter(self.parent, self.clock, self.stream)
            while not self.cancel.is_set():
                # Drain boundary messages before and after receiving a chunk.
                for _ in range(2):
                    while True:
                        try:
                            command, args = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        if command == "finish":
                            finish_cut = args[0]
                            finish_deadline = time.monotonic() + writer.settle_s
                        elif command == "block_start":
                            writer.start_block(*args)
                        elif command == "block_end":
                            writer.end_block(*args)
                        elif command == "trial":
                            writer.trial(*args)
                    if _ == 0:
                        samples, timestamps = inlet.pull_chunk(timeout=.02, max_samples=1024)
                if time.monotonic() - correction_at > 1:
                    correction = inlet.time_correction(timeout=.2)
                    correction_at = time.monotonic()
                received_at = time.perf_counter_ns()
                for values, remote_s in zip(samples, timestamps):
                    if not math.isfinite(remote_s):
                        raise ValueError("生理信号の時刻が不正です。")
                    local_s = remote_s + correction
                    sample_perf = perf_anchor + round((local_s - lsl_anchor) * 1e9)
                    age_ns = received_at - sample_perf
                    if age_ns < -1_000_000_000 or age_ns > 2_000_000_000:
                        raise ValueError("生理信号の時刻がこのPCの時刻と対応しません（1秒超の未来／2秒超の遅延）。")
                    writer.add(Sample(writer.count + 1, sample_perf, remote_s, correction, local_s, values))
                if timestamps:
                    self.last_arrival_ns = received_at
                    self.ready.set()
                    writer.raw_handle.flush()
                    for block in writer.blocks.values():
                        block["handle"].flush()
                writer.finalize_ready()
                if finish_cut is not None:
                    if (writer.watermark is not None and writer.watermark >= finish_cut) or time.monotonic() >= finish_deadline:
                        break
                elif self.last_arrival_ns is not None:
                    if received_at - self.last_arrival_ns > 2_000_000_000:
                        raise RuntimeError("OpenSignalsの生理信号が2秒以上途絶えました。")
                elif time.monotonic() > startup_deadline:
                    raise RuntimeError("OpenSignalsのストリームにサンプルがありません。計測を開始してください。")
        except Exception as exc:
            self.error = str(exc)
        finally:
            # The GUI can enqueue the last partial trial after a stream error.
            # Stay available briefly so its abort boundaries can be saved too.
            if self.error and writer is not None and finish_cut is None:
                self.ready.set()
                while not self.cancel.is_set():
                    try:
                        command, args = self.commands.get(timeout=.05)
                    except queue.Empty:
                        continue
                    try:
                        if command == "finish":
                            finish_cut = args[0]
                            break
                        if command == "trial":
                            writer.trial(*args)
                        elif command == "block_end":
                            writer.end_block(*args)
                    except Exception as exc:
                        self.error += "; " + str(exc)
            try:
                if writer is not None:
                    writer.close(finish_cut or time.perf_counter_ns(), self.error)
            except Exception as exc:
                self.error = self.error or str(exc)
            if inlet is not None:
                inlet.close_stream()
            self.ready.set()
            self.done.set()
