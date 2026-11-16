"""Admission: how many runs may run at once, whether the machine has the memory for the next
one, and the one Playwright stage at a time."""

import fcntl
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from qg import config, paths, peaks, proc, queue
from qg.queue import RunState

MAX_RUNS = 4
UNMEASURED_CAP_BYTES = 4 * 2**30
UNMEASURED_MARGIN_BYTES = 3 * 2**29
MEASURED_HEADROOM = 1.25
MEASURED_MARGIN_BYTES = 2**30
MIN_FREE_DISK_BYTES = 10 * 2**30
SETTLE_SECONDS = 20
POLL_SECONDS = 2
STAGE_LANES = {
    "static": "static",
    "backend": "tests",
    "frontend": "tests",
    "playwright": "tests",
    "qa": "qa_stack",
}
STOP = threading.Event()


def parse_meminfo(text: str) -> int:
    """Returns MemAvailable of /proc/meminfo's text in bytes."""
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise ValueError("MemAvailable is not in the meminfo text")


def available_memory() -> int:
    """Returns the bytes the kernel says can be allocated without swapping."""
    return parse_meminfo(paths.MEMINFO.read_text())


@dataclass(frozen=True)
class Need:
    """What admitting a stage requires free, and where the figure comes from."""

    bytes: int
    limit: str
    peak: int | None = None


def need_for(memory_max: str, peak: int | None = None) -> Need:
    """Returns the memory admitting a stage of a lane limited to `memory_max` requires.

    With no measured `peak` it is the limit (at most 4 GiB; `infinity`
    counts as 4 GiB) plus 1.5 GiB.
    With one it is the peak times 1.25 plus 1 GiB, never above the limit plus 1 GiB.
    """
    cap = config.memory_bytes(memory_max)
    if peak is None:
        assumed = UNMEASURED_CAP_BYTES if cap is None else min(cap, UNMEASURED_CAP_BYTES)
        return Need(assumed + UNMEASURED_MARGIN_BYTES, memory_max)
    measured = int(peak * MEASURED_HEADROOM) + MEASURED_MARGIN_BYTES
    if cap is not None:
        measured = min(measured, cap + MEASURED_MARGIN_BYTES)
    return Need(measured, memory_max, peak)


def lane_need(repo: str, lane: str, memory_max: str) -> Need:
    """Returns the need of a lane of `repo`: its biggest recent peak, else the capped rule."""
    seen = peaks.history(repo, lane)
    return need_for(memory_max, max(seen) if seen else None)


def fits(available: int, need: Need) -> bool:
    """Tells whether `available` bytes admit a stage with this need."""
    return available >= need.bytes


def gib(size: int) -> str:
    """Returns a byte count as GiB with one decimal."""
    return f"{size / 2**30:.1f} GiB"


def describe(need: Need) -> str:
    """Returns the need as `qg status` shows it, with its basis."""
    if need.peak is None:
        return (
            f"{gib(need.bytes)} (no history: {need.limit} limit (at most "
            f"{gib(UNMEASURED_CAP_BYTES)}) + {gib(UNMEASURED_MARGIN_BYTES)} margin)"
        )
    return f"{gib(need.bytes)} (measured peak {gib(need.peak)})"


def memory_reason(need: Need) -> str:
    """Returns why a stage waits for memory; the live figure is in `qg status`."""
    return f"waiting for memory: needs {describe(need)} available"


def queue_reason(run: RunState, running: int, settling: bool, available: int, first: Need) -> str:
    """Returns why queued `run` is not starting, for `qg status`."""
    if running >= MAX_RUNS:
        return f"waiting for a free slot ({MAX_RUNS} runs are running)"
    if settling:
        return f"waiting {SETTLE_SECONDS}s for the run that just started to take its memory"
    if not fits(available, first):
        return memory_reason(first)
    return "waiting behind an earlier run in the queue"


def free_disk() -> int:
    """Returns the free bytes of the filesystem that holds the state root and the sandboxes."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(paths.STATE_ROOT).free


def disk_reason(free: int) -> str | None:
    """Returns why no run starts on `free` bytes, None when there is room."""
    if free >= MIN_FREE_DISK_BYTES:
        return None
    return (
        f"waiting for disk: {gib(free)} free on {paths.STATE_ROOT}, "
        f"a run starts at {gib(MIN_FREE_DISK_BYTES)}"
    )


def pick(
    waiting: list[RunState],
    running: int,
    settling: bool,
    available: int,
    first_need: Callable[[RunState], Need],
) -> RunState | None:
    """Returns the queued run to start now, None when none may.

    `waiting` is in queue order (FIFO). Nothing starts at the slot limit, nor while the run
    started last is still settling (its memory is not yet in `available`). A run alone on the
    machine is admitted whatever the memory, as before there were two slots: its limits are
    caps, not use. A nightly run starts only when no verification waits. Otherwise the first
    run in queue order whose first stage (`first_need`) fits starts, so a run may pass the
    head while the head waits on memory.
    """
    if running >= MAX_RUNS or settling or not waiting:
        return None
    if any(run.mode != "nightly" for run in waiting):
        waiting = [run for run in waiting if run.mode != "nightly"]
    if not running:
        return waiting[0]
    return next((run for run in waiting if fits(available, first_need(run))), None)


def oldest_running(run_id: str) -> bool:
    """Tells whether `run_id` is the oldest running run: it never waits for memory."""
    ids = [run.id for run in queue.list_runs() if run.status == "running"]
    return not ids or run_id == min(ids)


def wait_for_memory(run_id: str, stage: str, memory_max: str) -> None:
    """Blocks until the machine has the memory for `stage`; the oldest running run never waits.

    Raises Cancelled when the run is cancelled or the executor is stopping.
    """
    key = f"{stage} memory"
    need = lane_need(queue.load(run_id).repo, STAGE_LANES[stage], memory_max)
    while True:
        if STOP.is_set() or queue.load(run_id).cancel_requested:
            queue.set_waiting(run_id, key, None)
            raise proc.Cancelled
        available = available_memory()
        if oldest_running(run_id) or fits(available, need):
            queue.set_waiting(run_id, key, None)
            return
        queue.set_waiting(run_id, key, memory_reason(need))
        STOP.wait(POLL_SECONDS)


@contextmanager
def playwright_turn(run_id: str) -> Iterator[None]:
    """Holds the machine-wide Playwright lock: no two runs run their Playwright stage together."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    key = "playwright lock"
    with paths.PLAYWRIGHT_LOCK.open("a") as handle:
        while True:
            if STOP.is_set() or queue.load(run_id).cancel_requested:
                queue.set_waiting(run_id, key, None)
                raise proc.Cancelled
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                queue.set_waiting(run_id, key, "waiting for another run's playwright stage")
                STOP.wait(POLL_SECONDS)
        queue.set_waiting(run_id, key, None)
        yield
