"""The daemon: starts queued runs one at a time under systemd scopes, reaps them, cleans up."""

import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType

from qg import (
    admission,
    config,
    cycles,
    executor,
    notify,
    paths,
    proc,
    prune,
    queue,
    sandbox,
)
from qg.config import Resources
from qg.queue import RunState
from qg.util import now_iso, parse_iso

TICK_SECONDS = 2
JANITOR_SECONDS = 600
RELIEF_SECONDS = 300
_last_relief = -RELIEF_SECONDS

Children = dict[str, "subprocess.Popen[bytes]"]


def spawn(run_id: str, resources: Resources) -> "subprocess.Popen[bytes]":
    """Starts the run's executor inside a named, resource-limited systemd scope."""
    command = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        f"--unit=qg-{run_id}",
        "--collect",
        "-p",
        f"CPUQuota={resources.cpu_quota}",
        "-p",
        f"MemoryMax={resources.memory_max}",
        "-p",
        "TimeoutStopSec=300",
        "nice",
        "-n",
        "10",
        "ionice",
        "-c2",
        "-n7",
        sys.executable,
        "-m",
        "qg",
        "_execute",
        run_id,
    ]
    with (paths.run_dir(run_id) / "executor.log").open("ab") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def _mark_running(state: RunState, budget_minutes: int, slot: str) -> None:
    """Moves a queued run into `slot` and fixes its overall deadline."""
    now = datetime.now(UTC)
    state.status = "running"
    state.slot = slot
    state.waiting = {}
    state.started = now.isoformat(timespec="seconds")
    state.deadline = (now + timedelta(minutes=budget_minutes)).isoformat(timespec="seconds")


def _mark_error(state: RunState, message: str) -> None:
    """Ends a queued or running run as an error; a finished run keeps the verdict it has."""
    if state.status in ("queued", "running"):
        state.status = "error"
        state.error = message
        state.ended = now_iso()
        state.slot = None
        state.stage = None
        state.waiting = {}


def _guarded(label: str, action: Callable[[], None]) -> None:
    """Runs one step of the daemon loop; a failure is printed and the loop goes on."""
    try:
        action()
    except (Exception, SystemExit):
        print(f"qg runner: {label} failed", file=sys.stderr)
        traceback.print_exc()


def _isolated(run_id: str, action: Callable[[], None]) -> None:
    """Runs `action` for one run; a failure marks that run `error` and the daemon goes on."""
    try:
        action()
    except (Exception, SystemExit) as exc:
        traceback.print_exc()
        message = f"runner failed: {exc!r}"
        try:
            queue.update(run_id, lambda state: _mark_error(state, message))
            notify.finished(run_id)
        except Exception:
            traceback.print_exc()


def _preempt_nightly(running: list[RunState], waiting: list[RunState]) -> None:
    """Cancels a running nightly run when a verification waits, so it never blocks a deploy."""
    for run in running:
        if run.mode == "nightly" and not run.cancel_requested:
            if any(other.mode != "nightly" for other in waiting):
                queue.update(run.id, lambda state: setattr(state, "cancel_requested", True))
                proc.stop_scope(run.id)


def _first_need(run: RunState) -> admission.Need:
    """Returns the memory the first thing a queued run does, its setup, requires."""
    cfg = config.parse(queue.load_request(run.id).config, f"request of run {run.id}")
    return admission.lane_need(run.repo, "setup", cfg.resources.lanes["setup"][1])


def _settling(running: list[RunState]) -> bool:
    """Tells whether a run started so recently that its memory is not yet in MemAvailable."""
    now = datetime.now(UTC)
    return any(
        run.started is not None
        and (now - parse_iso(run.started)).total_seconds() < admission.SETTLE_SECONDS
        for run in running
    )


def _relieve_disk() -> None:
    """Prunes under pressure, at most every five minutes, while the free disk keeps runs waiting."""
    global _last_relief
    if admission.disk_reason(admission.free_disk()) is None:
        return
    if time.monotonic() - _last_relief < RELIEF_SECONDS:
        return
    _last_relief = time.monotonic()
    _guarded("disk relief", lambda: prune.prune(pressure=True))


def candidates(runs: list[RunState], now: datetime) -> list[RunState]:
    """Returns the queued runs that may start at `now`, verifications first, each kind FIFO.

    A plain verification still held (`queue.hold_left`) is left out, and while one is held so is
    every nightly run.
    """
    queued = [run for run in runs if run.status == "queued"]
    held = {run.id for run in queued if queue.hold_left(run, now)}
    ready = [run for run in queued if run.id not in held and not (held and run.mode == "nightly")]
    return sorted(ready, key=lambda run: (run.mode == "nightly", run.id))


def start_next(children: Children) -> None:
    """Starts the queued verification the rule below admits, a nightly run only when none waits.

    Four runs at most. Queue order is FIFO, except that when the head's setup does not fit the
    memory now (MemAvailable below its measured need) a later run whose setup fits may start
    first; a run alone on the machine always starts. A nightly run in the way of a verification
    that cannot start is cancelled (see `_preempt_nightly`). Nothing starts while the free disk of
    the state root is below 10 GiB; the queued runs say so in `qg status`.

    A plain verification is not a candidate until it has been queued `queue.HOLD_SECONDS`, so a
    newer commit of its cycle supersedes it first; while one is held no nightly run starts and
    none is preempted for it (see `queue.holds`).
    """
    _relieve_disk()
    with queue.locked():
        runs = queue.list_runs()
        waiting = candidates(runs, datetime.now(UTC))
        running = [run for run in runs if run.status == "running"]
        if not waiting:
            return
        disk = admission.disk_reason(admission.free_disk())
        for run in waiting:
            queue.set_waiting(run.id, "disk", disk)
        if disk:
            return
        settling = _settling(running)
        available = admission.available_memory()
        chosen = admission.pick(waiting, len(running), settling, available, _first_need)
        if chosen is None:
            if not settling:
                _preempt_nightly(running, waiting)
            for run in waiting:
                reason = admission.queue_reason(
                    run, len(running), settling, available, _first_need(run)
                )
                queue.set_waiting(run.id, "queue", reason)
            return
        slot = next(slot for slot in queue.SLOTS if slot not in {run.slot for run in running})

        def start() -> None:
            """Marks the run running and spawns its executor."""
            request = queue.load_request(chosen.id)
            cfg = config.parse(request.config, f"request of run {chosen.id}")
            extra = cfg.extra_minutes(request.mode, request.stress)
            minutes = cfg.deadline_minutes(request.stage_set, request.qa_scale, extra)
            queue.update(chosen.id, lambda state: _mark_running(state, minutes, slot))
            children[chosen.id] = spawn(chosen.id, cfg.resources)

        _isolated(chosen.id, start)


def reap(children: Children) -> None:
    """Collects executors that exited and finishes the runs they left unfinished."""
    for run_id, child in list(children.items()):
        if child.poll() is None:
            continue
        del children[run_id]

        def finish(run_id: str = run_id, child: "subprocess.Popen[bytes]" = child) -> None:
            """Stops the dead executor's scope and abandons the run it left running."""
            proc.stop_scope(run_id)
            if queue.load(run_id).status == "running":
                reason = (
                    f"executor exited with code {child.returncode} before finishing; "
                    "see executor.log"
                )
                executor.abandon(run_id, reason)

        _isolated(run_id, finish)


def _executor_gone(state: RunState) -> bool:
    """Tells whether the executor of a running run is gone: by its pid, else by its scope."""
    if state.executor_pid is not None:
        return not proc.executor_alive(state.executor_pid, state.id)
    return proc.scope_dead(state.id)


def reconcile(children: Children) -> None:
    """Finishes running runs whose executor is not ours and no longer exists."""
    for state in queue.list_runs():
        if state.status != "running" or state.id in children:
            continue

        def check(state: RunState = state) -> None:
            """Abandons the run when its executor is gone, stopping what is left of its scope."""
            if _executor_gone(state):
                proc.stop_scope(state.id)
                executor.abandon(state.id, "executor is gone: its process no longer exists")

        _isolated(state.id, check)


def _mark_expired(state: RunState) -> None:
    """Records that the run outlived its deadline, so its end reads `interrupted` and says why."""
    if state.status == "running":
        state.deadline_exceeded = True


def _expire(state: RunState) -> None:
    """Flags the run as past its deadline and stops its scope; it then ends `interrupted`."""
    queue.update(state.id, _mark_expired)
    proc.stop_scope(state.id)


def enforce_deadlines() -> None:
    """Stops every running run past its overall deadline; it ends `interrupted`, never passed."""
    now = datetime.now(UTC)
    for state in queue.list_runs():
        late = state.deadline is not None and parse_iso(state.deadline) <= now
        if state.status == "running" and late and not state.deadline_exceeded:
            _isolated(state.id, lambda state=state: _expire(state))


def _mark_torn_down(state: RunState) -> None:
    """Records that the leftovers of the run were removed."""
    state.torn_down = True


def retention() -> None:
    """Prunes finished runs, containers, dependency caches and idle cycles.

    Daily, and at once when the free disk is below `prune.LOW_DISK_BYTES`; see `qg.prune`.
    """
    why = prune.due(admission.free_disk())
    if why is None:
        return
    prune.prune(pressure=why == "pressure")
    cutoff = datetime.now(UTC) - timedelta(days=prune.DROP_AFTER_DAYS)
    with queue.locked():
        cycles.fold_twins()
        remaining = {run.id for run in queue.list_runs()}
        for cycle in cycles.list_all():
            idle = cycle.artifact_url is None and not remaining.intersection(cycle.runs)
            if idle and parse_iso(cycle.updated) < cutoff:
                shutil.rmtree(paths.cycle_dir(cycle.id))


def _clean(state: RunState, children: Children) -> None:
    """Stops the scope of a finished run and removes its sandbox leftovers."""
    if not proc.scope_dead(state.id):
        proc.stop_scope(state.id)
    if state.sandbox_created and not state.torn_down:
        if sandbox.teardown(state.id, state.repo):
            queue.update(state.id, _mark_torn_down)


def janitor(children: Children) -> None:
    """Cleans finished runs' scopes and sandboxes, then prunes (see `retention`)."""
    recent = datetime.now(UTC) - timedelta(days=1)
    for state in queue.list_runs():
        if state.status not in queue.FINISHED or state.id in children:
            continue
        leftover = state.sandbox_created and not state.torn_down
        fresh = state.ended is not None and parse_iso(state.ended) > recent
        if leftover or fresh:
            _isolated(state.id, lambda state=state: _clean(state, children))
    _guarded("retention", retention)


def serve() -> int:
    """Runs the daemon loop until SIGTERM or SIGINT; executes nothing while qg is not enabled."""
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """Asks the loop to end after the current tick."""
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    children: Children = {}
    next_janitor = 0.0
    while not stop.is_set():
        _guarded("reap", lambda: reap(children))
        _guarded("reconcile", lambda: reconcile(children))
        _guarded("deadlines", enforce_deadlines)
        if time.monotonic() >= next_janitor:
            _guarded("janitor", lambda: janitor(children))
            next_janitor = time.monotonic() + JANITOR_SECONDS
        if paths.is_enabled():
            _guarded("start", lambda: start_next(children))
        stop.wait(TICK_SECONDS)
    return 0
