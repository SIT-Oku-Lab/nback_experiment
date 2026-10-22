"""End-to-end LSL acquisition test with synthetic signals, never BITalino data."""
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import threading
import time
import uuid

root = Path(__file__).resolve().parent
if (root / ".build_deps").exists():
    sys.path.insert(0, str(root / ".build_deps"))
from pylsl import StreamInfo, StreamOutlet, local_clock, resolve_byprop
from engine import Config
from storage import Recorder


def run():
    source = "synthetic-check-" + uuid.uuid4().hex
    info = StreamInfo("OpenSignals-SYNTHETIC-CHECK", "TEST-NOT-BITALINO", 6, 1000, "float32", source)
    channels = info.desc().append_child("channels")
    for index in range(6):
        channel = channels.append_child("channel")
        channel.append_child_value("label", f"SYNTHETIC_{index+1}")
        channel.append_child_value("unit", "arbitrary")
    outlet = StreamOutlet(info, chunk_size=10)
    stop = threading.Event()

    def produce():
        sample_index = 0
        anchor = local_clock()
        while not stop.is_set():
            current = local_clock()
            while anchor + sample_index / 1000 <= current:
                stamp = anchor + sample_index / 1000
                outlet.push_sample([math.sin(sample_index / (10 + c)) for c in range(6)], stamp)
                sample_index += 1
            stop.wait(.002)

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    rec = None
    try:
        streams = resolve_byprop("source_id", source, timeout=5)
        assert len(streams) == 1, "Synthetic stream not found"
        cfg = replace(Config(participant="SYNTHETIC_CAPTURE", mode="demo"), physiology=True,
                      physiology_uid=streams[0].uid(), physiology_name=streams[0].name())
        rec = Recorder(root / "verification_results", cfg, streams[0])
        start = time.perf_counter_ns() + 50_000_000
        end = start + 2_000_000_000
        rec.event("block_start", start, block=1, n=0, planned_duration_ms=2000)
        for trial in (1, 2):
            boundary = start + trial * 1_000_000_000
            while time.perf_counter_ns() < boundary:
                time.sleep(.005)
            rec.trial({"block": 1, "n": 0, "trial": trial, "eligible": 1,
                       "status": "completed", "outcome": "correct_rejection", "rt_ms": "",
                       "planned_onset_session_ms": rec.ms(boundary - 1_000_000_000),
                       "onset_session_ms": rec.ms(boundary - 1_000_000_000 + 2_000_000),
                       "response_deadline_session_ms": rec.ms(boundary), "closed_session_ms": rec.ms(boundary)})
            limit = time.monotonic() + 2
            path = rec.path / "physiology" / "trials" / f"block_01_trial_{trial:04d}.json"
            while not path.exists() and time.monotonic() < limit:
                time.sleep(.01)
            assert path.exists(), "Trial was not saved while acquisition was active"
        rec.event("block_end", end, block=1, planned_end_session_ms=rec.ms(end))
        rec.finish("completed")
        physiological = rec.path / "physiology"

        def read_rows(path):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))

        trial1 = read_rows(physiological / "trials" / "block_01_trial_0001.csv")
        trial2 = read_rows(physiological / "trials" / "block_01_trial_0002.csv")
        block = read_rows(physiological / "blocks" / "block_01_0back.csv")
        assert 990 <= len(trial1) <= 1010 and 990 <= len(trial2) <= 1010
        assert [r["sample_index"] for r in trial1 + trial2] == [r["sample_index"] for r in block]
        segments = read_rows(physiological / "segments.csv")
        assert len(segments) == 3 and all(r["quality"] == "complete" for r in segments), segments
        assert all(float(r["relative_trial_ms"]) < 1000 for r in trial1 + trial2)
        assert json.loads((physiological / "stream.json").read_text(encoding="utf-8"))["channels"][0]["label"] == "SYNTHETIC_1"
        report = {"result": "passed", "sample_rate": 1000, "channels": 6,
                  "trial_samples": [len(trial1), len(trial2)], "block_samples": len(block),
                  "trial_files_saved_before_session_end": True, "no_overlap_or_loss_between_trials": True,
                  "all_segments_complete": True, "scope": "synthetic LSL, not BITalino hardware"}

        # Keep the outlet advertised but stop sending: this must be detected.
        rec = Recorder(root / "verification_results", cfg, streams[0])
        stop.set()
        producer.join(timeout=2)
        limit = time.monotonic() + 4
        while not rec.physiology_error() and time.monotonic() < limit:
            time.sleep(.05)
        assert rec.physiology_error(), "Stopped stream was not detected"
        rec.finish("aborted", "synthetic_dropout_test")
        report["stream_stall_detected"] = True
        (root / "verification_results" / "physiology_roundtrip.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        if rec is not None and not rec.closed:
            rec.finish("error", "synthetic validation failed")
        stop.set()
        producer.join(timeout=2)


if __name__ == "__main__":
    run()
