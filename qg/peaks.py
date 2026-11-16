"""Measured memory: each lane's scope peaks during a run, and what past runs peaked at."""

import json
import threading
from pathlib import Path

from qg import paths, proc, queue
from qg.util import atomic_write

HISTORY_RUNS = 10
POLL_SECONDS = 3
COUNTED = ("passed", "failed")


def peaks_file(run_id: str) -> Path:
    """Returns the path of the file that holds a run's measured peaks, bytes per lane."""
    return paths.run_dir(run_id) / "peaks.json"


def read(run_id: str) -> dict[str, int]:
    """Returns the lane peaks recorded for a run; empty when none were."""
    path = peaks_file(run_id)
    if not path.exists():
        return {}
    return {lane: int(size) for lane, size in json.loads(path.read_text()).items()}


def lane_of(unit: str, run_id: str) -> str | None:
    """Returns the lane of a unit named `qg-<run id>-<lane>-<n>.scope`, None for any other unit."""
    prefix = f"qg-{run_id}-"
    if not (unit.startswith(prefix) and unit.endswith(".scope")):
        return None
    lane, _, serial = unit.removeprefix(prefix).removesuffix(".scope").rpartition("-")
    return lane if lane and serial.isdigit() else None


def _count(values: dict[str, str], key: str) -> int:
    """Returns a systemd counter as bytes; `[not set]` and a missing key are 0."""
    return int(values[key]) if values.get(key, "").isdigit() else 0


def parse_show(text: str) -> list[tuple[str, int, int]]:
    """Returns (unit, MemoryPeak, MemoryCurrent) per block of `systemctl show -p Id -p Memory...`.

    A counter systemd does not have yet is `[not set]` and counts as 0.
    """
    rows = []
    for block in text.strip().split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" not in values:
            continue
        rows.append((values["Id"], _count(values, "MemoryPeak"), _count(values, "MemoryCurrent")))
    return rows


def lane_peaks(rows: list[tuple[str, int, int]], run_id: str) -> dict[str, int]:
    """Returns the biggest each lane reached: one scope's peak, or all its live scopes together."""
    peaks: dict[str, int] = {}
    live: dict[str, int] = {}
    for unit, peak, current in rows:
        lane = lane_of(unit, run_id)
        if lane is None:
            continue
        peaks[lane] = max(peaks.get(lane, 0), peak)
        live[lane] = live.get(lane, 0) + current
    return {lane: max(size, live[lane]) for lane, size in peaks.items()}


def sample(run_id: str) -> dict[str, int]:
    """Asks systemd for the peaks of the run's lane scopes alive now."""
    listed = proc.run(
        ["systemctl", "--user", "list-units", "--plain", "--no-legend", f"qg-{run_id}-*.scope"]
    )
    units = [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
    if not units:
        return {}
    shown = proc.run(
        [
            "systemctl",
            "--user",
            "show",
            "-p",
            "Id",
            "-p",
            "MemoryPeak",
            "-p",
            "MemoryCurrent",
            *units,
        ]
    )
    return lane_peaks(parse_show(shown.stdout), run_id)


def record(run_id: str, seen: dict[str, int]) -> None:
    """Raises the run's stored lane peaks to `seen` where it is bigger."""
    known = read(run_id)
    merged = {lane: max(known.get(lane, 0), seen.get(lane, 0)) for lane in {*known, *seen}}
    if merged != known:
        atomic_write(peaks_file(run_id), json.dumps(merged, indent=2) + "\n")


def watch(run_id: str, stop: threading.Event) -> threading.Thread:
    """Starts the thread that records the run's lane peaks every few seconds until `stop`."""

    def loop() -> None:
        """Samples, records, waits; one last sample after stop."""
        while True:
            done = stop.wait(POLL_SECONDS)
            record(run_id, sample(run_id))
            if done:
                return

    thread = threading.Thread(target=loop, name="peaks", daemon=True)
    thread.start()
    return thread


def history(repo: str, lane: str) -> list[int]:
    """Returns the lane's peak of the repo's last completed runs, newest first, at most ten."""
    found: list[int] = []
    for run in sorted(queue.list_runs(), key=lambda run: run.id, reverse=True):
        if run.repo != repo or run.status not in COUNTED:
            continue
        peak = read(run.id).get(lane)
        if peak:
            found.append(peak)
        if len(found) == HISTORY_RUNS:
            break
    return found
