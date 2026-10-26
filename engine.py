"""Deterministic task logic; no GUI, hardware, or wall-clock dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import random

VERSION = "1.2.13"
BACKGROUND = "#808080"
GRID = "#737373"
STIMULUS = "#bfbfbf"
FIXATION = "#5e5e5e"
INTERVALS = (1000, 1500, 2000, 2500, 3000, 4000, 5000)


@dataclass(frozen=True)
class Config:
    participant: str = "P001"
    conditions: tuple[int, ...] = (0, 2)
    interval_ms: int = 3000
    stimulus_ms: int = 500
    target_percent: int = 30
    zero_target: int = 4  # zero-based, row-major
    baseline_s: int = 30
    rest_s: int = 60
    seed: int = 1
    mode: str = "experiment"
    lsl: bool = False
    physiology: bool = False
    physiology_uid: str = ""
    physiology_name: str = ""
    input_mode: str = "keyboard"

    @property
    def duration_ms(self) -> int:
        return {"experiment": 600_000, "practice": 60_000, "demo": 12_000}[self.mode]

    @property
    def prepare_s(self) -> int:
        return 1 if self.mode == "demo" else 5

    def validate(self) -> None:
        if self.input_mode not in ("touch", "keyboard"):
            raise ValueError("入力方式はタッチまたはキーボードを選択してください。")
        if not self.participant or len(self.participant) > 64:
            raise ValueError("参加者IDは1〜64文字で入力してください。")
        if not self.conditions or any(n not in (0, 2) for n in self.conditions):
            raise ValueError("課題は0-backまたは2-backを選んでください。")
        if self.mode not in ("experiment", "practice", "demo"):
            raise ValueError("実施モードが不正です。")
        if self.interval_ms <= 0 or self.duration_ms % self.interval_ms:
            raise ValueError("提示間隔は課題時間を割り切る値にしてください。")
        if self.mode != "demo" and self.interval_ms not in INTERVALS:
            raise ValueError("提示間隔は指定の選択肢から選んでください。")
        if not 50 <= self.stimulus_ms < self.interval_ms:
            raise ValueError("刺激提示時間は50 ms以上、提示間隔未満にしてください。")
        if not 10 <= self.target_percent <= 50:
            raise ValueError("ターゲット率は10〜50%にしてください。")
        if not 0 <= self.zero_target < 9:
            raise ValueError("0-backのターゲット位置が不正です。")
        if not 0 <= self.baseline_s <= 300 or not 0 <= self.rest_s <= 600:
            raise ValueError("安静は0〜300秒、休憩は0〜600秒にしてください。")
        if not 0 <= self.seed <= 2**32 - 1:
            raise ValueError("乱数シードは0〜4294967295にしてください。")
        if self.duration_ms // self.interval_ms <= max(self.conditions):
            raise ValueError("試行数が不足しています。")
        if self.physiology and not self.physiology_uid:
            raise ValueError("OpenSignalsで計測とLSL送信を開始し、「信号を検索」で記録するデバイスを選んでください。")

    def metadata(self) -> dict:
        return {**asdict(self), "duration_ms": self.duration_ms,
                "prepare_s": self.prepare_s, "version": VERSION,
                "colors": {"background": BACKGROUND, "grid": GRID,
                           "stimulus": STIMULUS, "fixation": FIXATION}}


@dataclass(frozen=True)
class Trial:
    index: int
    position: int
    eligible: bool
    target: bool


def make_sequence(config: Config, n: int, block_index: int) -> list[Trial]:
    """Exact planned target count; non-targets cannot accidentally match."""
    config.validate()
    rng = random.Random(config.seed + block_index * 1_000_003 + n * 10_007)
    count = config.duration_ms // config.interval_ms
    eligible_indices = list(range(n, count))
    target_count = math.floor(len(eligible_indices) * config.target_percent / 100 + 0.5)
    targets = set(rng.sample(eligible_indices, target_count))
    trials: list[Trial] = []
    for index in range(count):
        eligible = index >= n
        compare = config.zero_target if n == 0 else (trials[index - n].position if eligible else None)
        target = index in targets
        if target:
            position = compare
        else:
            position = rng.choice([p for p in range(9) if p != compare])
        trials.append(Trial(index, position, eligible, target))
    return trials


def classify(trial: Trial, responded: bool) -> str:
    if not trial.eligible:
        return "warmup"
    if trial.target:
        return "hit" if responded else "miss"
    return "false_alarm" if responded else "correct_rejection"


def scheduled_index(elapsed_ns: int, interval_ms: int) -> int:
    return max(0, elapsed_ns // (interval_ms * 1_000_000))


def accepts_response(trial: Trial, onset_ns: int | None, deadline_ns: int,
                     now_ns: int, responded: bool) -> bool:
    return (trial.eligible and onset_ns is not None and not responded
            and onset_ns <= now_ns < deadline_ns)


def summarize(rows: list[dict]) -> dict:
    completed = [r for r in rows if r["status"] == "completed" and r["eligible"]]
    counts = {name: sum(r["outcome"] == name for r in completed)
              for name in ("hit", "miss", "false_alarm", "correct_rejection")}
    hit_rts = [float(r["rt_ms"]) for r in completed
               if r["outcome"] == "hit" and r["rt_ms"] != ""]
    target_n = counts["hit"] + counts["miss"]
    non_target_n = counts["false_alarm"] + counts["correct_rejection"]
    return {**counts, "scored_trials": len(completed),
            "accuracy": ((counts["hit"] + counts["correct_rejection"]) / len(completed)
                         if completed else None),
            "hit_rate": counts["hit"] / target_n if target_n else None,
            "false_alarm_rate": counts["false_alarm"] / non_target_n if non_target_n else None,
            "mean_hit_rt_ms": sum(hit_rts) / len(hit_rts) if hit_rts else None,
            "skipped_trials": sum(r["status"] == "skipped" for r in rows),
            "aborted_trials": sum(r["status"] == "aborted" for r in rows),
            "invalid_history_trials": sum(r["status"] == "invalid_history" for r in rows),
            "late_trials_over_50ms": sum(float(r.get("onset_lateness_ms") or 0) > 50 for r in rows)}
