"""The wall-clock estimate of a run, printed up front by `qg verify`.

The stages run in lanes (static beside the tests, QA after them), so the run takes its setup
plus its slowest lane, not the sum of its stages.
"""

import statistics
from dataclasses import dataclass

from qg.config import QA_BOOT_MINUTES, Config
from qg.queue import RunState
from qg.util import elapsed

HISTORY_RUNS = 5


@dataclass(frozen=True)
class Estimate:
    """Minutes per lane, the run's expected wall clock and its hard cap."""

    lanes: dict[str, float]
    setup: float
    simplify: float
    total: float
    cap: int
    qa_minutes: float


def stage_history(runs: list[RunState], stage: str) -> float | None:
    """Returns the median minutes of the last runs' own (not reused) passes of `stage`."""
    seen = []
    for run in sorted(runs, key=lambda r: r.id, reverse=True):
        held = run.stages.get(stage)
        if run.mode == "verify" and held and held.status == "passed" and not held.reused_from:
            seconds = elapsed(held.started, held.ended)
            if seconds:
                seen.append(seconds / 60)
        if len(seen) == HISTORY_RUNS:
            break
    return statistics.median(seen) if seen else None


def estimate(cfg: Config, stage_set: list[str], qa_scale: float, runs: list[RunState]) -> Estimate:
    """Returns the estimate for `stage_set`: history medians, else the configured typical minutes.

    The QA lane is its stack boot plus the agent's time limit grown by the plan's scale: the agent
    uses its allowance, so history says nothing about a bigger plan.
    """

    def minutes(stage: str) -> float:
        found = stage_history(runs, stage)
        return max(found, 1.0) if found is not None else float(cfg.typical_minutes[stage])

    lanes: dict[str, float] = {}
    for lane, names in cfg.lane_stages(stage_set).items():
        lanes[lane] = sum(
            QA_BOOT_MINUTES + cfg.qa_scaled(qa_scale).time_limit_minutes
            if name == "qa"
            else minutes(name)
            for name in names
        )
    setup = minutes("setup")
    simplify = float(cfg.timeouts["simplify"]) / 2 if "simplify" in stage_set else 0.0
    chain = lanes.get("tests", 0.0) + lanes.get("qa", 0.0)
    others = [found for lane, found in lanes.items() if lane not in ("tests", "qa")]
    total = setup + simplify + max([chain, *others])
    return Estimate(
        lanes=lanes,
        setup=setup,
        simplify=simplify,
        total=total,
        cap=cfg.deadline_minutes(stage_set, qa_scale),
        qa_minutes=cfg.qa_scaled(qa_scale).time_limit_minutes if "qa" in stage_set else 0,
    )


def describe(found: Estimate, stage_set: list[str]) -> list[str]:
    """Returns the lines `qg verify` prints for the estimate."""
    lanes = ", ".join(f"{lane} {minutes:.0f} min" for lane, minutes in found.lanes.items())
    lines = [
        f"estimate  about {found.total:.0f} min wall clock "
        f"(setup {found.setup:.0f} + slowest lane; static runs beside tests then qa: {lanes})",
        f"cap       {found.cap} min including a margin; the run is stopped past it",
    ]
    if "qa" in stage_set:
        lines.append(f"qa        agent limit {found.qa_minutes:.0f} min for this plan's size")
    return lines
