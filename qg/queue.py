"""File queue: one directory per run with request.json and state.json, guarded by an flock."""

import contextlib
import fcntl
import json
import math
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from qg import cycles, paths
from qg.util import atomic_write, die, now_iso, parse_iso

FINISHED = ("passed", "failed", "cancelled", "interrupted", "superseded", "error")
SLOTS = ("auto", "auto2", "auto3", "auto4")
HOLD_SECONDS = 120


@dataclass
class CommandRun:
    """One command of a stage: its exit code and what the test runner printed at the end."""

    command: str
    code: int
    tool: str | None = None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    summary: str = ""


@dataclass
class StageState:
    """Progress and outcome of one stage of a run."""

    status: str = "pending"
    started: str | None = None
    ended: str | None = None
    summary: str = ""
    failures: list[str] = field(default_factory=list)
    commands: list[CommandRun] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    turns: int | None = None
    fingerprint: str = ""
    reused_from: str | None = None
    owners: list[str] = field(default_factory=list)
    flakes: dict[str, str] = field(default_factory=dict)


def stage_text(stage: StageState) -> str:
    """Returns a stage's status for people: `passed (reused from <run>)` when it was reused."""
    if stage.reused_from:
        return f"{stage.status} (reused from {stage.reused_from})"
    return stage.status


@dataclass
class Request:
    """What was asked for one run, with the validated verify.yml it will use; never changes."""

    id: str
    repo: str
    branch: str
    sha: str
    tree: str
    feature: str
    plan: str
    env: str
    qa: bool
    retest_of: str | None
    stage_set: list[str]
    config: dict[str, Any]
    cycle: str
    created: str
    config_ref: str = ""
    config_sha: str = ""
    config_branch_differs: bool = False
    base: str = ""
    qa_scale: float = 1.0
    explicit_plan: str = ""
    reuse: bool = True
    mode: str = "verify"
    prd: str = ""
    phase: str = ""
    release: str = ""
    phases: list[str] = field(default_factory=list)
    stress: int = 1
    worktree: str = ""


@dataclass
class RunState:
    """The mutable state of a run, rewritten at every transition."""

    id: str
    repo: str
    branch: str
    sha: str
    tree: str
    feature: str
    plan: str
    env: str
    qa: bool
    retest_of: str | None
    stage_set: list[str]
    cycle: str
    findings_path: str
    created: str
    status: str = "queued"
    stage: str | None = None
    stages: dict[str, StageState] = field(default_factory=dict)
    slot: str | None = None
    waiting: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    cancel_requested: bool = False
    sandbox_created: bool = False
    torn_down: bool = False
    superseded_by: str | None = None
    executor_pid: int | None = None
    deadline: str | None = None
    deadline_exceeded: bool = False
    config_ref: str = ""
    config_sha: str = ""
    config_branch_differs: bool = False
    requested_sha: str = ""
    requested_tree: str = ""
    base: str = ""
    simplified_ref: str = ""
    mode: str = "verify"
    release: str = ""
    phases: list[str] = field(default_factory=list)
    error: str | None = None
    started: str | None = None
    ended: str | None = None
    updated: str = ""


class _Lock:
    """Process-wide bookkeeping of the reentrant queue lock.

    `threads` makes it reentrant per thread and exclusive between threads: the executor's lanes
    update the state from several threads, and the file lock alone cannot tell them apart.
    """

    depth = 0
    fd = -1
    threads = threading.RLock()


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Holds the machine-wide queue lock, reentrant; SIGTERM and SIGINT wait while it is taken."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        with _Lock.threads:
            if _Lock.depth == 0:
                paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
                fd = os.open(paths.QUEUE_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                _Lock.fd = fd
            _Lock.depth += 1
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                previous = None
                yield
            finally:
                _Lock.depth -= 1
                if _Lock.depth == 0:
                    os.close(_Lock.fd)
                    _Lock.fd = -1
    finally:
        if previous is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def stage_from_dict(raw: dict[str, Any]) -> StageState:
    """Rebuilds a StageState from its JSON form."""
    commands = [CommandRun(**item) for item in raw.get("commands", [])]
    return StageState(**{**raw, "commands": commands})


def _state_from_dict(raw: dict[str, Any]) -> RunState:
    """Rebuilds a RunState from its JSON form."""
    stages = {name: stage_from_dict(item) for name, item in raw["stages"].items()}
    return RunState(**{**raw, "stages": stages})


def load(run_id: str) -> RunState:
    """Reads run `run_id`'s state; exits when the run does not exist."""
    path = paths.state_file(run_id)
    if not path.exists():
        die(f"no run {run_id!r}")
    return _state_from_dict(json.loads(path.read_text()))


def load_request(run_id: str) -> Request:
    """Reads run `run_id`'s immutable request."""
    return Request(**json.loads(paths.request_file(run_id).read_text()))


def save(state: RunState) -> None:
    """Writes the state atomically, stamping its update time."""
    state.updated = now_iso()
    atomic_write(paths.state_file(state.id), json.dumps(asdict(state), indent=2) + "\n")


def update(run_id: str, mutate: Callable[[RunState], None]) -> RunState:
    """Applies `mutate` to the run's state under the queue lock, saves it and returns it."""
    with locked():
        state = load(run_id)
        mutate(state)
        save(state)
        return state


def set_waiting(run_id: str, key: str, reason: str | None) -> None:
    """Records why the run waits under `key`, or clears it; writes only when it changes."""
    with locked():
        state = load(run_id)
        if state.waiting.get(key) == reason:
            return
        if reason is None:
            del state.waiting[key]
        else:
            state.waiting[key] = reason
        save(state)


def _readable_request(run_id: str) -> Request | None:
    """Returns the run's request, None when request.json is missing or unreadable."""
    try:
        return load_request(run_id)
    except (OSError, ValueError, TypeError):
        return None


def _quarantine(run_id: str, exc: Exception) -> RunState:
    """Sets a state.json that cannot be read aside and writes an `error` state in its place.

    The state is rebuilt from the run's request when that is readable, flagged so the janitor
    tears down whatever sandbox the run left; without a request only the identity is kept.
    """
    with locked():
        try:
            return _state_from_dict(json.loads(paths.state_file(run_id).read_text()))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        request = _readable_request(run_id)
        message = f"state.json unreadable ({exc!r}); kept as state.json.corrupt"
        os.replace(paths.state_file(run_id), paths.run_dir(run_id) / "state.json.corrupt")
        names = ("setup", *(request.stage_set if request else ()))
        state = RunState(
            id=run_id,
            repo=request.repo if request else "",
            branch=request.branch if request else "",
            sha=request.sha if request else "",
            tree=request.tree if request else "",
            feature=request.feature if request else "",
            plan=request.plan if request else "",
            env=request.env if request else "",
            qa=request.qa if request else False,
            retest_of=request.retest_of if request else None,
            stage_set=list(request.stage_set) if request else [],
            cycle=request.cycle if request else "",
            findings_path=str(paths.findings_file(run_id)),
            created=request.created if request else now_iso(),
            status="error",
            stages={name: StageState() for name in names},
            sandbox_created=request is not None,
            config_ref=request.config_ref if request else "",
            config_sha=request.config_sha if request else "",
            config_branch_differs=request.config_branch_differs if request else False,
            requested_sha=request.sha if request else "",
            requested_tree=request.tree if request else "",
            base=request.base if request else "",
            mode=request.mode if request else "verify",
            error=message,
            ended=now_iso(),
        )
        save(state)
        print(f"qg: run {run_id}: {message}", file=sys.stderr)
        return state


_SNAPSHOT: list[RunState] | None = None


@contextlib.contextmanager
def snapshot() -> Iterator[None]:
    """Serves every `list_runs()` inside the block from one read of the disk.

    For a read-only command (`qg status`) that asks for the runs hundreds of times while it
    renders; the runner, which needs fresh state on every poll, never enters it.
    """
    global _SNAPSHOT
    _SNAPSHOT = _read_runs()
    try:
        yield
    finally:
        _SNAPSHOT = None


def list_runs() -> list[RunState]:
    """Returns the state of every run on disk, ordered by id (oldest first).

    A run whose state.json cannot be parsed is marked `error` (see `_quarantine`) and listed as
    such, so one corrupt file never stops a caller, the runner loop included. Inside
    `snapshot()` the disk is read once and every call gets that same list.
    """
    if _SNAPSHOT is not None:
        return list(_SNAPSHOT)
    return _read_runs()


def _read_runs() -> list[RunState]:
    """Reads every run's state.json; see `list_runs`."""
    if not paths.RUNS.exists():
        return []
    found = []
    for entry in sorted(paths.RUNS.iterdir()):
        file = entry / "state.json"
        if not file.exists():
            continue
        try:
            found.append(_state_from_dict(json.loads(file.read_text())))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            found.append(_quarantine(entry.name, exc))
    return found


def resolve(prefix: str) -> str:
    """Returns the id of the one run whose id equals or starts with `prefix`."""
    ids = [entry.name for entry in paths.RUNS.iterdir()] if paths.RUNS.exists() else []
    if prefix in ids:
        return prefix
    matches = [i for i in ids if i.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        die(f"ambiguous run {prefix!r}: {', '.join(matches)}")
    die(f"no run {prefix!r}")


def enqueue(request: Request, plan_text: str | None = None) -> tuple[RunState, list[str]]:
    """Queues the run and appends it to its cycle; returns its state and the runs it superseded.

    `plan_text`, when given, is written to the run's plan.md before the run becomes visible.
    """
    with locked():
        paths.ensure()
        superseded = []
        if request.branch != "detached":
            for old in list_runs():
                same_line = (
                    old.repo == request.repo
                    and old.branch == request.branch
                    and old.cycle == request.cycle
                )
                same_kind = (old.mode == "nightly") == (request.mode == "nightly")
                if old.status == "queued" and same_line and same_kind:
                    old.status = "superseded"
                    old.superseded_by = request.id
                    old.ended = now_iso()
                    save(old)
                    superseded.append(old.id)
        paths.logs_dir(request.id).mkdir(parents=True)
        atomic_write(paths.request_file(request.id), json.dumps(asdict(request), indent=2) + "\n")
        if plan_text is not None:
            atomic_write(paths.plan_file(request.id), plan_text)
        state = RunState(
            id=request.id,
            repo=request.repo,
            branch=request.branch,
            sha=request.sha,
            tree=request.tree,
            feature=request.feature,
            plan=request.plan,
            env=request.env,
            qa=request.qa,
            retest_of=request.retest_of,
            stage_set=list(request.stage_set),
            cycle=request.cycle,
            findings_path=str(paths.findings_file(request.id)),
            created=request.created,
            config_ref=request.config_ref,
            config_sha=request.config_sha,
            config_branch_differs=request.config_branch_differs,
            requested_sha=request.sha,
            requested_tree=request.tree,
            base=request.base,
            mode=request.mode,
            release=request.release,
            phases=list(request.phases),
            stages={name: StageState() for name in ("setup", *request.stage_set)},
        )
        save(state)
        cycles.append_run(
            request.id,
            request.cycle,
            request.repo,
            request.branch,
            request.feature,
            request.plan,
            request.prd,
            request.phase,
        )
        return state, superseded


def holds(request: Request) -> bool:
    """Tells whether a run of `request` waits HOLD_SECONDS in the queue before it may start.

    Only a plain `qg verify` does: mode verify, reuse on (`--force` and `--stress` turn it off),
    no `retest_of` (a retest, a resume, an automatic retry) and a branch, since a detached commit
    is never superseded. Nightly, release and phase runs never wait.
    """
    return (
        request.mode == "verify"
        and request.reuse
        and request.retest_of is None
        and request.branch != "detached"
    )


def hold_left(state: RunState, now: datetime) -> int:
    """Returns the seconds queued run `state` still waits for a newer commit of its cycle to
    supersede it (see `enqueue`), 0 when it may start."""
    if state.status != "queued" or not holds(load_request(state.id)):
        return 0
    waited = (now - parse_iso(state.created)).total_seconds()
    return max(0, math.ceil(HOLD_SECONDS - waited))


def hold_text(seconds: int) -> str:
    """Returns why a held run is not starting, as `qg status` and `qg verify` print it."""
    return f"starts in {seconds}s: waiting for a newer commit of this cycle to supersede it"


def position(state: RunState) -> tuple[int, int]:
    """Returns how many queued runs are ahead of `state` and how many runs are running."""
    runs = list_runs()
    ahead = sum(1 for r in runs if r.status == "queued" and r.id < state.id)
    running = sum(1 for r in runs if r.status == "running")
    return ahead, running


def latest_per_branch(runs: list[RunState]) -> list[RunState]:
    """Returns the newest non-superseded run of every repo+branch, newest first."""
    newest: dict[tuple[str, str, str], RunState] = {}
    for run in runs:
        if run.status == "superseded":
            continue
        key = (run.repo, run.branch, run.mode)
        if key not in newest or run.id > newest[key].id:
            newest[key] = run
    return sorted(newest.values(), key=lambda r: r.id, reverse=True)
