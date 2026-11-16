"""A release run: shared stages once, one QA run per phase, one verdict, failures with owners."""

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import replace

from qg import cycles, ownership, paths, queue
from qg.queue import Request, RunState, StageState
from qg.util import now_iso

PHASE_ID = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*:[A-Za-z0-9]+(?:\.[0-9]+)*")
FAIL_FAST = re.compile(r"release \S+: shared stage (\S+) failed")
SHARED = ("static", "backend", "frontend", "playwright")
REASON_CHARS = 140
NO_CHANGE_FLOW = "plan:no user-facing change"
PLAN_PREFIX = "plan:"
RETEST = re.compile(r"^retest (\S+)$")
WALK_RESULTS = ("walked", "failed")


def split_phase(text: str) -> tuple[str, str]:
    """Returns the PRD and phase of `F01:P1` or `ABC-11:P1.1`; raises ValueError for anything else."""
    if not PHASE_ID.fullmatch(text):
        raise ValueError(f"{text!r} is not PRD:PHASE, for example F01:P1 or ABC-11:P1")
    prd, _, phase = text.partition(":")
    return prd.upper(), phase.upper()


def phase_pairs(state: RunState) -> list[tuple[str, str]]:
    """Returns the (PRD, phase) pairs a release run was asked for."""
    return [split_phase(text) for text in state.phases]


def children(parent_id: str) -> list[RunState]:
    """Returns the per-phase QA runs of a release, oldest first."""
    found = [r for r in queue.list_runs() if r.release == parent_id and r.mode == "phase"]
    return sorted(found, key=lambda run: run.id)


def fail_fast(parent: RunState, stage: str) -> list[str]:
    """Cancels the still-queued phase runs of a release whose shared `stage` failed.

    The release can no longer be READY, so a phase run that has not started would only spend
    an hour of QA. Running phase runs are left alone. Returns the ids that were cancelled.
    """
    reason = f"release {parent.id}: shared stage {stage} failed"
    dropped = []

    def drop(state: RunState) -> None:
        """Cancels the phase run when it is still queued."""
        if state.status == "queued":
            state.status = "cancelled"
            state.ended = now_iso()
            state.error = reason
            dropped.append(state.id)

    for kid in children(parent.id):
        if kid.status == "queued":
            queue.update(kid.id, drop)
    return dropped


def phase_label(run: RunState) -> str:
    """Returns `F01·P1`, the phase of a phase run's cycle."""
    cycle = cycles.load(run.cycle)
    return ownership.label(cycle.prd, cycle.phase)


Problems = Callable[[RunState], list[str]]


def _by_phase(
    parent: RunState, kids: list[RunState]
) -> tuple[list[str], dict[str, list[RunState]]]:
    """Returns the labels of the phases a release declared and its phase runs per label."""
    labels = [ownership.label(prd, phase) for prd, phase in phase_pairs(parent)]
    by_label: dict[str, list[RunState]] = {}
    for kid in kids:
        by_label.setdefault(phase_label(kid), []).append(kid)
    return labels, by_label


def is_plan(flow: str) -> bool:
    """Tells whether a flow name is a plan change, `plan:<short name>`."""
    return flow.casefold().startswith(PLAN_PREFIX)


def walked(run: RunState) -> bool:
    """Tells whether a run's QA walked anything of what it was asked to walk.

    A QA that passed did. One that ended otherwise did when its qa.json shows a plan topic walked
    or failed, or `plan:no user-facing change` as its only plan flow (as `qa.coverage_problems`
    counts it); a walk with no plan flow at all did when a flow other than a retest was walked
    or failed.
    """
    qa = run.stages.get("qa")
    if qa is not None and qa.status == "passed":
        return True
    path = paths.qa_file(run.id)
    if not path.is_file():
        return False
    flows = [
        (flow["name"], str(flow.get("result", "")).split(":", 1)[0].strip())
        for flow in json.loads(path.read_text())["flows"]
        if not RETEST.match(flow["name"])
    ]
    plan = [(name, result) for name, result in flows if is_plan(name)]
    if plan and {name.casefold() for name, _ in plan} == {NO_CHANGE_FLOW}:
        return True
    counted = [(n, r) for n, r in plan if n.casefold() != NO_CHANGE_FLOW] if plan else flows
    return any(result in WALK_RESULTS for _, result in counted)


def _current(runs: list[RunState]) -> RunState | None:
    """Returns the run that stands for a phase: its only run, or the newest of a retry chain.

    A chain is the first run followed by its automatic retry (a run whose `retest_of` is the one
    before). Several runs that are no such chain stand for nothing: None, as for no run at all.
    """
    ordered = sorted(runs, key=lambda run: run.id)
    if not ordered:
        return None
    for older, newer in zip(ordered, ordered[1:], strict=False):
        if newer.retest_of != older.id:
            return None
    return ordered[-1]


def _counted(label: str, runs: list[RunState]) -> str:
    """Returns how a phase whose runs stand for nothing is named: no run, or how many."""
    return f"{label} ({'no run' if not runs else f'{len(runs)} runs'})"


def _short(text: str) -> str:
    """Returns `text` cut to REASON_CHARS characters, an ellipsis marking the cut."""
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1].rstrip() + "…"


def _incomplete(kid: RunState) -> str:
    """Returns why a finished phase run's QA does not count as passed, empty when it does.

    A cancelled run names its error; any other names the status and the start of its QA's
    summary (the phase's own report has all of it).
    """
    if kid.status == "cancelled":
        return f"cancelled: {_short(kid.error)}" if kid.error else "cancelled"
    qa = kid.stages.get("qa")
    if qa is not None and qa.status == "passed":
        return ""
    status, summary = (qa.status, qa.summary) if qa else (kid.status, kid.error or "")
    return f"QA {status}" + (f": {_short(summary)}" if summary else "")


def qa_stage(parent: RunState, kids: list[RunState]) -> StageState:
    """Returns the release's QA as one advisory stage, the aggregate of its phases' QA.

    It is `running` while a declared phase's one run has not finished, `failed` (incomplete)
    when a declared phase has no run, several runs or a run whose QA did not pass, each named
    with why, and `passed` otherwise. Whether the phase runs may stand for the release at all is
    `integrity`'s, which blocks; an incomplete QA only warns.
    """
    labels, by_label = _by_phase(parent, kids)
    if not labels:
        return StageState(status="failed", summary="the release declares no phases")
    bad = []
    pending = 0
    for label in labels:
        runs = by_label.get(label, [])
        current = _current(runs)
        if current is None:
            bad.append(_counted(label, runs))
        elif current.status not in queue.FINISHED:
            pending += 1
        elif why := _incomplete(current):
            bad.append(f"{label} ({why if walked(current) else 'no QA: ' + why})")
    if pending:
        summary = f"{pending} of {len(labels)} phases still running their QA"
        return StageState(status="running", summary=summary)
    if bad:
        return StageState(status="failed", summary=f"QA not passed: {', '.join(bad)}")
    return StageState(status="passed", summary=f"QA passed for {len(labels)} phases")


def _kid_problems(parent: RunState, kid: RunState, problems: Problems) -> list[str]:
    """Returns why a finished phase run cannot stand for its phase in the release."""
    found = []
    if kid.repo != parent.repo or kid.tree != parent.tree:
        found.append("another repo or tree")
    return [*found, *problems(kid)]


def integrity(parent: RunState, kids: list[RunState], problems: Problems) -> list[str]:
    """Returns why the phase runs of a release cannot stand for it; each reason blocks its verdict.

    Every (PRD, phase) the release declared needs exactly one run, and a finished one must be on
    the release's own repo and tree, trusted, with reused stages that stand (`problems` returns
    why a run's provenance fails). No phases declared, a phase with no run or with several, or a
    run of an undeclared phase is a problem too. A phase run that was cancelled or whose QA did
    not pass is only incomplete (see `qa_stage`).
    """
    labels, by_label = _by_phase(parent, kids)
    if not labels:
        return ["the release declares no phases"]
    bad = []
    unwalked = []
    for label in labels:
        runs = by_label.get(label, [])
        current = _current(runs)
        if current is None:
            bad.append(_counted(label, runs))
        elif current.status in queue.FINISHED:
            if why := _kid_problems(parent, current, problems):
                bad.append(f"{label} ({'; '.join(why)})")
            elif not walked(current):
                unwalked.append((label, _incomplete(current) or "nothing walked"))
    bad += [f"{label} (not declared)" for label in by_label if label not in labels]
    reasons = [f"phase runs that do not count: {', '.join(bad)}"] if bad else []
    return reasons + ([f"no QA: {_grouped(unwalked)}"] if unwalked else [])


def _grouped(items: list[tuple[str, str]]) -> str:
    """Returns phases with their reasons, one entry per reason: `F02·P1 (why)` when it is the only
    one, `22 phases (why): BUG·P1, BUG·P2, …` when several share it (a fail-fast cancels many)."""
    by_reason: dict[str, list[str]] = {}
    for label, why in items:
        by_reason.setdefault(why, []).append(label)
    parts = []
    for why, labels in by_reason.items():
        if len(labels) == 1:
            parts.append(f"{labels[0]} ({why})")
        else:
            parts.append(f"{len(labels)} phases ({why}): {', '.join(labels)}")
    return ", ".join(parts)


def partial(parent: RunState, kids: list[RunState]) -> list[str]:
    """Returns the phases whose QA walked part of the plan but did not pass, each with why."""
    labels, by_label = _by_phase(parent, kids)
    found = []
    for label in labels:
        current = _current(by_label.get(label, []))
        if current is None or current.status not in queue.FINISHED or not walked(current):
            continue
        if why := _incomplete(current):
            found.append(f"{label} ({why})")
    return found


def unwalked(run: RunState) -> str:
    """Returns why a run whose own QA ended without walking anything is not READY, else ''."""
    qa = run.stages.get("qa")
    if qa is None or qa.status not in ("failed", "skipped") or walked(run):
        return ""
    return f"no QA: {_short(qa.summary or qa.status)}"


def retry(state: RunState, request: Request | None) -> str | None:
    """Queues the one automatic retry of a phase run whose QA walked nothing; returns its id.

    Only a first attempt is retried, never a cancelled one, nor one whose release was cancelled
    or already failed a shared stage (it cannot be READY). The retry is a fresh walk (no reuse) of
    the same plan, a retest of `state` in the same cycle and release.
    """
    if request is None or request.mode != "phase" or request.retest_of:
        return None
    if state.status in ("cancelled", "superseded"):
        return None
    if walked(state) or not paths.state_file(request.release).exists():
        return None
    parent = queue.load(request.release)
    failed = any(parent.stages.get(name, StageState()).status == "failed" for name in SHARED)
    if parent.status == "cancelled" or failed:
        return None
    run_id = paths.new_run_id(request.release)
    plan = paths.plan_file(state.id)
    own = request.plan == str(plan)
    again = replace(
        request,
        id=run_id,
        created=now_iso(),
        retest_of=state.id,
        reuse=False,
        plan=str(paths.plan_file(run_id)) if own else request.plan,
    )
    queue.enqueue(again, plan.read_text() if plan.is_file() else None)
    return run_id


def effective_stages(state: RunState) -> dict[str, StageState]:
    """Returns the stages a verdict is judged on: a release run adds the QA of its phases."""
    if state.mode != "release":
        return state.stages
    return {**state.stages, "qa": qa_stage(state, children(state.id))}


def awaiting_phases(state: RunState) -> bool:
    """Returns whether a finished release run still waits for the QA of its phases."""
    if state.mode != "release" or state.status not in ("passed", "failed"):
        return False
    return qa_stage(state, children(state.id)).status == "running"


def graft(parent: RunState) -> list[str]:
    """Gives every phase run the parent's shared stages, recorded as reused from the release.

    Returns the ids of the phase runs that already finished, whose reports must be rewritten.
    """
    now = now_iso()
    finished = []
    for kid in children(parent.id):

        def apply(state: RunState, parent: RunState = parent) -> None:
            """Records the parent's finished shared stages on the phase run."""
            for name in SHARED:
                held = parent.stages.get(name)
                if held is None or held.status not in ("passed", "failed"):
                    continue
                state.stages[name] = replace(
                    held,
                    started=now,
                    ended=now,
                    reused_from=parent.id,
                    failures=list(held.failures),
                    commands=list(held.commands),
                    specs=list(held.specs),
                    flows=list(held.flows),
                    owners=list(held.owners),
                )

        queue.update(kid.id, apply)
        if kid.status in queue.FINISHED:
            finished.append(kid.id)
    return finished


def sync(run_id: str) -> list[str]:
    """Propagates a finished run through its release; returns the runs whose report changed.

    A finished release run grafts its shared stages onto its phase runs; a finished phase run
    changes its release run's QA, so the release report is rewritten.
    """
    state = queue.load(run_id)
    if state.mode == "release" and state.status in queue.FINISHED:
        return graft(state)
    if state.mode == "phase" and paths.state_file(state.release).exists():
        parent = queue.load(state.release)
        if parent.status in queue.FINISHED:
            return [parent.id]
    return []


def attribute(state: RunState, stages: dict[str, StageState], reasons: list[str]) -> list[str]:
    """Rewrites the Playwright reason of a release or phase run with who owns the failures.

    The release says how many tests failed and whose they are; a phase page says which of its
    own tests failed and which phase blocks it, instead of repeating the failure as its own.
    """
    held = stages.get("playwright")
    if state.mode not in ("release", "phase") or held is None or held.status != "failed":
        return reasons
    if not held.failures or len(held.owners) != len(held.failures):
        return reasons
    if state.mode == "release":
        text = ownership.summary(held.owners) + flake_note(held.flakes)
    else:
        text = ownership.blocked_by(held.owners, phase_label(state))
    return [
        f"playwright: {text}" if reason.startswith("playwright: failed") else reason
        for reason in reasons
    ]


def flake_note(flakes: dict[str, str]) -> str:
    """Returns the flake hunt's tally for a reason line: ` (1 always fails, 2 flaky)`."""
    if not flakes:
        return ""
    counts = Counter(
        "flaky" if v.startswith(("flaky", "intermitente")) else v for v in flakes.values()
    )
    return " (" + ", ".join(f"{n} {label}" for label, n in counts.items()) + ")"


def rerun_passed(label: str) -> bool:
    """Tells whether a flake label says the test passed every rerun: `flaky 0/N`."""
    return label.startswith(("flaky 0/", "intermitente 0/"))


def phases_table(state: RunState, verdict: Callable[[RunState], str]) -> list[str]:
    """Returns the report lines of a release run: one row per phase with its run and results."""
    lines = ["", "## Phases", "", "| phase | run | QA | verdict |", "|---|---|---|---|"]
    for kid in children(state.id):
        qa = kid.stages.get("qa")
        stage = queue.stage_text(qa) if qa else kid.status
        if qa is not None and qa.reused_from:
            stage = f"{qa.status} (QA reused from run {qa.reused_from})"
        lines.append(f"| {phase_label(kid)} | {kid.id} | {stage} | {verdict(kid)} |")
    return lines


def owner_lines(stage: StageState) -> list[str]:
    """Returns the failing-test lines of a stage with their owners and flake labels."""
    if len(stage.owners) != len(stage.failures):
        return list(stage.failures)
    shown = []
    for line, owner in zip(stage.failures, stage.owners, strict=True):
        notes = [f"owner: {owner or ownership.NO_OWNER}"]
        if line in stage.flakes:
            notes.append(stage.flakes[line])
        shown.append(f"{line} ({'; '.join(notes)})")
    return shown
