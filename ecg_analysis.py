"""Post-acquisition ECG-derived research metrics. Raw recordings are never edited.

WFDB XQRS detects QRS complexes; automated RR screening does NOT establish
normal sinus beats. Review beats.csv and the ECG before interpreting HRV.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import re
import statistics

METHOD = {
    "detector": "WFDB XQRS + correct_peaks (50 ms search, 150 ms baseline)",
    "rr_timebase": "sample index / nominal sampling rate within uninterrupted runs",
    "beat_timestamps": "recorded clock-corrected session_ms; no sample interpolation",
    "rr_range_ms": [300, 2000], "local_rr_tolerance_fraction": .20,
    "local_rr_neighbours_each_side": 5,
    "local_rr_reference": "mean of bounded RR in centred 11-beat neighbourhood",
    "hrv_windows_s": [60, 300], "rolling_step_s": 5,
    "hrv_min_duration_s": 60, "hrv_min_accepted_intervals": 30,
    "hrv_min_accepted_fraction": .95, "hrv_min_coverage_fraction": .90,
    "max_rate_error_fraction": .02,
    "warning": "Automatically screened RR, not clinically verified NN. No ectopic classification. "
               "HRV requires ECG/beat review; different window durations are not interchangeable.",
}
BEAT_FIELDS = ["beat", "run", "sample_index", "session_ms", "unix_ns", "previous_session_ms",
               "rr_ms", "hr_bpm", "accepted", "reason", "timing_verified"]
METRIC_FIELDS = ["kind", "block", "n", "trial", "start_session_ms", "end_session_ms", "duration_s",
                 "rr_count", "accepted_rr", "rejected_rr", "accepted_fraction", "rr_coverage_fraction",
                 "adjacent_pairs", "mean_rr_ms", "mean_hr_bpm", "sdnn_ms", "rmssd_ms", "pnn50_percent",
                 "quality", "hrv_reason"]


def write_json(path, value):
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def write_csv(path, fields, rows):
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def select_channel(stream, override=0):
    channels = stream.get("channels", [])
    count = int(stream["channel_count"])
    if override == -1:
        return None, "disabled"
    if override > 0:
        if override > count:
            return None, "channel_out_of_range"
        return override - 1, "explicit_lsl_column"
    matches = [i for i, channel in enumerate(channels[:count])
               if re.search(r"ecg|ekg|electrocard", channel.get("label", ""), re.I)]
    if len(matches) == 1:
        return matches[0], "ecg_label"
    return None, "ambiguous_ecg_channels" if matches else "ecg_label_not_found"


def screen_rr(beats):
    """Mark outliers, retaining every detected beat and every original RR."""
    for i, beat in enumerate(beats):
        rr = beat["rr_ms"]
        if rr == "":
            reason = "first_beat_of_run"
        elif not 300 <= rr <= 2000:
            reason = "rr_out_of_range"
        elif not beat["timing_verified"]:
            reason = "sampling_rate_mismatch"
        else:
            nearby = [b["rr_ms"] for b in beats[max(0, i-5):i+6]
                      if b["run"] == beat["run"] and b["rr_ms"] != "" and 300 <= b["rr_ms"] <= 2000]
            reference = statistics.mean(nearby)
            reason = "local_rr_outlier" if abs(rr - reference) > .20 * reference else "accepted"
        beat["reason"], beat["accepted"] = reason, reason == "accepted"
        beat["hr_bpm"] = 60000 / rr if beat["accepted"] else ""
    return beats


def metrics(beats, start, end, *, kind, block="", n="", trial="", discontinuities=()):
    selected = [b for b in beats if b["rr_ms"] != "" and start <= b["previous_session_ms"]
                and b["session_ms"] < end]
    accepted = [b for b in selected if b["accepted"]]
    rr = [b["rr_ms"] for b in accepted]
    # Never concatenate across rejected intervals or recording discontinuities.
    diffs = [b["rr_ms"] - a["rr_ms"] for a, b in zip(selected, selected[1:])
             if a["accepted"] and b["accepted"] and b["beat"] == a["beat"] + 1 and b["run"] == a["run"]]
    duration = max(0, (end - start) / 1000)
    fraction = len(rr) / len(selected) if selected else 0
    coverage = sum(rr) / (end - start) if end > start else 0
    reason = ("duration_below_60s" if duration < 60 else
              "recording_discontinuity" if any(start <= t < end for t in discontinuities) else
              "insufficient_accepted_rr" if len(rr) < 30 else
              "too_many_rejected_rr" if fraction < .95 else
              "insufficient_rr_coverage" if not .90 <= coverage <= 1.05 else
              "no_adjacent_accepted_rr" if not diffs else "")
    mean = statistics.mean(rr) if rr and fraction >= .95 else None
    return {"kind": kind, "block": block, "n": n, "trial": trial,
            "start_session_ms": start, "end_session_ms": end, "duration_s": duration,
            "rr_count": len(selected), "accepted_rr": len(rr), "rejected_rr": len(selected)-len(rr),
            "accepted_fraction": fraction, "rr_coverage_fraction": coverage, "adjacent_pairs": len(diffs),
            "mean_rr_ms": mean if mean is not None else "", "mean_hr_bpm": 60000 / mean if mean else "",
            "sdnn_ms": statistics.stdev(rr) if not reason else "",
            "rmssd_ms": math.sqrt(statistics.mean(d*d for d in diffs)) if not reason else "",
            "pnn50_percent": 100 * sum(abs(d) > 50 for d in diffs) / len(diffs) if not reason else "",
            "quality": "screened_rr_requires_review" if not reason else "hrv_unavailable",
            "hrv_reason": reason}


def analyze_recording(parent: Path, stream: dict, channel_override=0):
    """Run only after acquisition has closed continuous.csv. No network I/O."""
    parent = Path(parent)
    folder = parent / "physiology" / "ecg"
    folder.mkdir(exist_ok=True)
    channel, selection = select_channel(stream, channel_override)
    report = {"status": "unavailable", "reason": selection, "method": METHOD,
              "source": "../continuous.csv", "channel_selection": selection,
              "folder": "physiology/ecg", "computed_after_acquisition": True}
    if channel is None:
        write_json(folder / "report.json", report)
        return report
    rate = float(stream["nominal_srate"])
    report.update(channel_column=f"ch_{channel+1}",
                  channel_label=next((c.get("label", "") for c in stream.get("channels", [])
                                      if c.get("column") == f"ch_{channel+1}"), ""), sampling_rate_hz=rate)
    if not math.isfinite(rate) or not 100 <= rate <= 4000:
        report["reason"] = "unsupported_sampling_rate_100_to_4000_hz_required"
        write_json(folder / "report.json", report)
        return report
    # Optional for source installs; frozen releases bundle these dependencies.
    import numpy as np
    import scipy
    import wfdb
    from wfdb import processing
    seq_column = next((c["column"] for c in stream.get("channels", [])
                       if c.get("label", "").lower() == "nseq"), None)
    values, times, indices, epochs = [], [], [], []
    runs, discontinuities = [], []
    run_start, previous_seq = None, None
    with (parent / "physiology" / "continuous.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            value, stamp = float(row[f"ch_{channel+1}"]), float(row["session_ms"])
            sequence = float(row[seq_column]) if seq_column else None
            sequence_ok = (sequence is None or previous_seq is None or
                           (math.isfinite(sequence) and math.isfinite(previous_seq)
                            and sequence.is_integer() and previous_seq.is_integer()
                            and (sequence == (previous_seq+1) % 16 if 0 <= sequence < 16 and 0 <= previous_seq < 16
                                 else sequence == previous_seq+1)))
            gap = bool(times) and (stamp - times[-1] > max(250, 10000/rate)
                                   or int(row["sample_index"]) != indices[-1]+1)
            finite = math.isfinite(value)
            if gap or not sequence_ok or not finite:
                if run_start is not None:
                    runs.append((run_start, len(values)))
                run_start = None
                discontinuities.append(stamp)
            if finite and run_start is None:
                run_start = len(values)
            values.append(value)
            times.append(stamp)
            indices.append(int(row["sample_index"]))
            epochs.append(int(row["unix_ns"]))
            previous_seq = sequence
    if run_start is not None:
        runs.append((run_start, len(values)))
    signal = np.asarray(values, dtype=float)
    beats, errors = [], []
    for run_id, (lo, hi) in enumerate(runs, 1):
        if hi-lo < 3*rate or np.ptp(signal[lo:hi]) <= np.finfo(float).eps:
            continue
        nominal_duration = (hi-lo-1)/rate
        actual_duration = (times[hi-1]-times[lo])/1000
        timing_ok = actual_duration > 0 and abs(nominal_duration/actual_duration-1) <= .02
        try:
            peaks = processing.xqrs_detect(signal[lo:hi], fs=rate, verbose=False)
            if len(peaks):
                peaks = processing.correct_peaks(signal[lo:hi], peaks, max(1, round(.05*rate)),
                                                 max(1, round(.15*rate)), peak_dir="compare")
            peaks = sorted(set(int(p)+lo for p in peaks if 0 <= p < hi-lo))
        except ValueError as exc:
            errors.append({"run": run_id, "error": str(exc)})
            discontinuities.append(times[lo])
            continue
        last = None
        for peak in peaks:
            rr = (peak-last)/rate*1000 if last is not None else ""
            beats.append({"beat": len(beats)+1, "run": run_id, "sample_index": indices[peak],
                          "session_ms": times[peak], "unix_ns": epochs[peak],
                          "previous_session_ms": times[last] if last is not None else "",
                          "rr_ms": rr, "timing_verified": timing_ok})
            last = peak
    screen_rr(beats)
    write_csv(folder / "beats.csv", BEAT_FIELDS, beats)
    end = times[-1] + 1000/rate if times else 0
    segment_rows = []
    with (parent / "physiology" / "segments.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            segment_rows.append(metrics(beats, float(row["start_session_ms"]), float(row["end_session_ms"]),
                                        kind=row["kind"], block=row["block"], n=row["n"], trial=row["trial"],
                                        discontinuities=discontinuities))
    write_csv(folder / "segments_hr_hrv.csv", METRIC_FIELDS, segment_rows)
    rolling = []
    for window in (60000, 300000):
        for stop in range(window, math.floor(end)+1, 5000):
            rolling.append(metrics(beats, stop-window, stop, kind=f"rolling_{window//1000}s",
                                   discontinuities=discontinuities))
    write_csv(folder / "rolling_hrv.csv", METRIC_FIELDS, rolling)
    report.update(status="computed" if beats else "no_beats_detected", reason="", beats=len(beats),
                  accepted_rr=sum(b["accepted"] for b in beats),
                  rejected_rr=sum(not b["accepted"] for b in beats if b["rr_ms"] != ""),
                  discontinuities=len(discontinuities), sequence_checked=seq_column is not None,
                  detection_errors=errors, wfdb_version=wfdb.__version__, numpy_version=np.__version__,
                  scipy_version=scipy.__version__,
                  session_metrics=metrics(beats, max(0, times[0]) if times else 0, end, kind="session",
                                          discontinuities=discontinuities),
                  files=["beats.csv", "segments_hr_hrv.csv", "rolling_hrv.csv"])
    write_json(folder / "report.json", report)
    return report
