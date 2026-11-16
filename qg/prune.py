"""Retention: what a finished run keeps, by age, and what the disk gives back when it is short.

A run's small files (state, request, plan, report, findings, qa, peaks, logs of the verdict) stay
until the run is dropped whole; its bulk (QA home, built bundles, storage, worktrees) and its
read-heavy output (stage logs, evidence) go sooner. `decide` is the pure policy over `Facts`;
`gather` reads them from disk; `apply` executes a plan.
"""

import fcntl
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qg import cycles, deployed, git, paths, proc, queue, report, topics
from qg.queue import RunState
from qg.util import atomic_write, parse_iso

HEAVY_AFTER_DAYS = 3
RESUME_DAYS = 7
DROP_AFTER_DAYS = 45
PRESSURE_AFTER_HOURS = 1
LOW_DISK_BYTES = 30 * 2**30
DAILY_SECONDS = 86400

KEEP = "keep"
BULK = "bulk"
HEAVY = "heavy"
DROP = "drop"

BULK_NAMES = (
    "qa-home",
    "qa-dist",
    "qa-storage",
    "test-storage",
    "simplify-home",
    "verify-home",
    "verify-src",
    "src",
    "aside",
    "pnpm-install.lock",
)
PROBE_BULK = ("home", "src", "retest-src")
READ_HEAVY_NAMES = ("logs", "evidence")
RESUMABLE = ("failed", "cancelled", "interrupted", "error")
CONTAINER = re.compile(r"^qg-(\d{8}-\d{6}-[0-9a-f]{4})-")
CACHE_DAYS = 14
TMP_SECONDS = 3600


@dataclass(frozen=True)
class Facts:
    """What the policy needs to know of one finished run."""

    id: str
    repo: str
    line: str
    status: str
    ended: datetime
    settled: bool = True
    unverified_p0: bool = False
    blocking: bool = False
    lent: bool = False
    cycle_live: bool = False
    cycle_open: bool = False
    cycle_newest_qa: bool = False
    ready_kept: bool = False


def _protected_from_drop(f: Facts) -> bool:
    """Tells whether the run's directory must outlive the drop age."""
    return f.ready_kept or f.blocking or f.lent or f.cycle_open or f.cycle_live


def decide(facts: list[Facts], now: datetime, pressure: bool = False) -> dict[str, str]:
    """Returns KEEP, BULK, HEAVY or DROP for each finished run.

    DROP removes the directory; HEAVY the bulk and the read-heavy output; BULK only the bulk.
    The latest run of each (repo, branch) and a run whose P0 awaits `verify-p0` keep everything.
    A failed, cancelled, interrupted or errored run keeps everything for a week (it may be resumed)
    unless the disk is short. A run whose evidence a command can still read (open findings, a QA
    another run reuses, the newest QA of a cycle, an open cycle within the resume window) loses
    only its bulk.
    """
    latest: dict[tuple[str, str], str] = {}
    for f in facts:
        key = (f.repo, f.line)
        if key not in latest or f.id > latest[key]:
            latest[key] = f.id
    heavy_after = timedelta(hours=PRESSURE_AFTER_HOURS if pressure else HEAVY_AFTER_DAYS * 24)
    plan: dict[str, str] = {}
    for f in facts:
        age = now - f.ended
        if not f.settled:
            plan[f.id] = KEEP
        elif age > timedelta(days=DROP_AFTER_DAYS) and not _protected_from_drop(f):
            plan[f.id] = DROP
        elif age <= heavy_after:
            plan[f.id] = KEEP
        elif latest[(f.repo, f.line)] == f.id or f.unverified_p0 or f.cycle_live:
            plan[f.id] = KEEP
        elif f.status in RESUMABLE and age <= timedelta(days=RESUME_DAYS) and not pressure:
            plan[f.id] = KEEP
        elif (
            f.blocking
            or f.lent
            or f.cycle_newest_qa
            or (f.cycle_open and age <= timedelta(days=RESUME_DAYS))
        ):
            plan[f.id] = BULK
        else:
            plan[f.id] = HEAVY
    return plan


def _cycle_open(cycle: cycles.Cycle) -> bool:
    """Tells whether the cycle still has a topic that is not `works` or a blocking finding."""
    found = topics.load(cycle.id)
    if any(topic.verdict != topics.WORKS for topic in found.values()):
        return True
    for run_id in reversed(cycle.runs):
        if paths.findings_file(run_id).exists():
            return any(report.blocking(item) for item in report.load_findings(run_id))
    return False


def _holding(runs: list[RunState]) -> set[str]:
    """Returns the runs with an open blocking finding, while another run of their tree remains.

    Deleting such a run would let the tree's other runs go READY without that finding.
    """
    trees: dict[tuple[str, str], list[RunState]] = {}
    for run in runs:
        if report.trusted(run):
            trees.setdefault((run.repo, run.tree), []).append(run)
    holding: set[str] = set()
    for group in trees.values():
        if len(group) > 1:
            holding |= {
                run.id
                for run in group
                if any(report.blocking(item) for item in report.load_findings(run.id))
            }
    return holding


def _line(run: RunState) -> str:
    """Returns the branch line a run belongs to; nightly runs have their own."""
    return f"nightly:{run.branch}" if run.mode == "nightly" else run.branch


def _ready(run: RunState) -> bool:
    """Tells whether a passed or failed run's verdict is READY; a run that left QA out is READY
    only for the stages it ran, never for a deploy, so it is not."""
    if run.status not in ("passed", "failed") or report.qa_notice(run):
        return False
    return report.verdict_of(run, queue.load_request(run.id))[0] == report.READY


def _ready_kept(finished: list[RunState], now: datetime) -> set[str]:
    """Returns the READY runs that still gate a deploy: the newest READY of each branch line and
    every READY run of a tree the deployed record names. Only runs past the drop age are judged."""
    old = [
        r
        for r in finished
        if parse_iso(r.ended or r.created) < now - timedelta(days=DROP_AFTER_DAYS)
    ]
    if not old:
        return set()
    shipped = {
        (repo, entry["tree"]) for repo, envs in deployed.load().items() for entry in envs.values()
    }
    lines: dict[tuple[str, str], list[RunState]] = {}
    for run in finished:
        if run.status in ("passed", "failed"):
            lines.setdefault((run.repo, _line(run)), []).append(run)
    kept: set[str] = set()
    for group in lines.values():
        for run in sorted(group, key=lambda r: r.id, reverse=True):
            if _ready(run):
                kept.add(run.id)
                break
    kept &= {r.id for r in old}
    for run in old:
        if (run.repo, run.tree) in shipped or (run.repo, run.requested_tree) in shipped:
            if run.id not in kept and _ready(run):
                kept.add(run.id)
    return kept


def gather(now: datetime) -> list[Facts]:
    """Reads the policy's facts for every finished run; queued and running runs are left out."""
    runs = queue.list_runs()
    finished = [r for r in runs if r.status in queue.FINISHED]
    live = {r.id for r in runs if r.status not in queue.FINISHED}
    lent = {s.reused_from for r in runs for s in r.stages.values() if s.reused_from}
    live_cycles = {r.cycle for r in runs if r.id in live and r.cycle}
    holding = _holding(finished)
    ready_kept = _ready_kept(finished, now)
    open_cycles: set[str] = set()
    newest_qa: set[str] = set()
    for cycle in cycles.list_all():
        if _cycle_open(cycle):
            open_cycles.add(cycle.id)
        with_qa = [r for r in cycle.runs if paths.qa_file(r).is_file()]
        if with_qa:
            newest_qa.add(with_qa[-1])
    found = []
    for run in finished:
        blocking = [item for item in report.load_findings(run.id) if report.blocking(item)]
        found.append(
            Facts(
                id=run.id,
                repo=run.repo,
                line=_line(run),
                status=run.status,
                ended=parse_iso(run.ended or run.created),
                settled=not run.sandbox_created or run.torn_down,
                unverified_p0=any(report.unverified(item) for item in blocking),
                blocking=run.id in holding or bool(blocking),
                lent=run.id in lent,
                cycle_live=run.cycle in live_cycles,
                cycle_open=run.cycle in open_cycles,
                cycle_newest_qa=run.id in newest_qa,
                ready_kept=run.id in ready_kept,
            )
        )
    return found


@dataclass
class Plan:
    """What a prune removes: paths per run with their sizes, whole runs, containers, caches."""

    paths: list[tuple[str, Path, int]] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    containers: list[str] = field(default_factory=list)
    thumbs: list[tuple[Path, int]] = field(default_factory=list)
    caches: list[tuple[Path, int]] = field(default_factory=list)

    def total(self) -> int:
        """Returns the bytes the plan frees."""
        return (
            sum(size for _, _, size in self.paths)
            + sum(self.dropped.values())
            + sum(size for _, size in self.thumbs)
            + sum(size for _, size in self.caches)
        )


def size_of(path: Path) -> int:
    """Returns the disk bytes under `path`, a hard-linked file once, links not followed."""
    seen: set[tuple[int, int]] = set()
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        info = current.lstat()
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
        if current.is_dir() and not current.is_symlink():
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    return total


def bulk_paths(run_id: str) -> list[Path]:
    """Returns the existing bulk entries of a run's directory."""
    root = paths.run_dir(run_id)
    found = [root / name for name in BULK_NAMES if os.path.lexists(root / name)]
    probes = root / "probes"
    if probes.is_dir():
        for one in probes.iterdir():
            found += [one / name for name in PROBE_BULK if os.path.lexists(one / name)]
    return found


def heavy_paths(run_id: str) -> list[Path]:
    """Returns the existing read-heavy entries of a run's directory."""
    root = paths.run_dir(run_id)
    return [root / name for name in READ_HEAVY_NAMES if os.path.lexists(root / name)]


def stale_containers(known: dict[str, RunState]) -> list[str]:
    """Returns the names of qg's docker containers whose run is not queued or running."""
    listing = proc.run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=60)
    stale = []
    for name in listing.stdout.split():
        match = CONTAINER.match(name)
        if match is None:
            continue
        run = known.get(match.group(1))
        if run is None or run.status in queue.FINISHED:
            stale.append(name)
    return stale


def old_thumbs(now: datetime) -> list[tuple[Path, int]]:
    """Returns the thumbnail caches of cycles untouched for the drop age; the pages embed them."""
    cutoff = now - timedelta(days=DROP_AFTER_DAYS)
    found = []
    for cycle in cycles.list_all():
        thumbs = paths.thumbs_dir(cycle.id)
        if thumbs.is_dir() and parse_iso(cycle.updated or cycle.created) < cutoff:
            found.append((thumbs, size_of(thumbs)))
    return found


def _cache_key_busy(lock: Path) -> bool:
    """Tells whether another process holds the cache entry's lock."""
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def stale_caches() -> list[tuple[Path, int]]:
    """Returns the venv and node cache entries unused for 14 days (use touches them) and the
    temporary installs older than an hour, none whose lock another process holds."""
    now = time.time()
    found = []
    for root in (paths.VENVS, paths.NODE):
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            temporary = entry.name.startswith(".tmp-")
            age = now - entry.stat().st_mtime
            if age <= (TMP_SECONDS if temporary else CACHE_DAYS * 86400):
                continue
            if not temporary and _cache_key_busy(root / f"{entry.name}.lock"):
                continue
            found.append((entry, size_of(entry)))
    return found


def make_plan(now: datetime, pressure: bool = False, docker: bool = True) -> Plan:
    """Decides what to remove now."""
    plan = Plan()
    with queue.locked():
        decided = decide(gather(now), now, pressure)
        known = {run.id: run for run in queue.list_runs()}
    for run_id, action in decided.items():
        if action == DROP:
            plan.dropped[run_id] = size_of(paths.run_dir(run_id))
        elif action in (BULK, HEAVY):
            targets = bulk_paths(run_id) + (heavy_paths(run_id) if action == HEAVY else [])
            plan.paths += [(run_id, target, size_of(target)) for target in targets]
    if docker:
        plan.containers = stale_containers(known)
    plan.thumbs = old_thumbs(now)
    plan.caches = stale_caches()
    return plan


def _remove(target: Path, repo: str) -> None:
    """Removes a directory; a git worktree goes through git first, then its admin entry."""
    if (target / ".git").is_file():
        proc.run(["git", "-C", repo, "worktree", "remove", "--force", str(target)], timeout=600)
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    else:
        shutil.rmtree(target, ignore_errors=True)
    if repo:
        git.remove_worktree_entry(repo, str(target))


def apply(plan: Plan) -> None:
    """Executes a plan: removes the paths, the dropped runs, the containers, thumbnails and caches,
    then prunes the worktree records of every repo a removed run verified."""
    repos: dict[str, str] = {}
    for run in queue.list_runs():
        repos[run.id] = run.repo
    for run_id, target, _ in plan.paths:
        _remove(target, repos.get(run_id, ""))
    for run_id in plan.dropped:
        _remove(paths.run_dir(run_id), repos.get(run_id, ""))
    if plan.containers:
        proc.run(["docker", "rm", "-fv", *plan.containers], timeout=600)
    for thumbs, _ in plan.thumbs:
        shutil.rmtree(thumbs, ignore_errors=True)
    for entry, _ in plan.caches:
        if not entry.name.startswith(".tmp-"):
            (entry.parent / f"{entry.name}.lock").unlink(missing_ok=True)
        shutil.rmtree(entry, ignore_errors=True)
    touched = {repos[r] for r in {*plan.dropped, *(r for r, _, _ in plan.paths)} if repos.get(r)}
    for repo in touched:
        if Path(repo).is_dir():
            proc.run(["git", "-C", repo, "worktree", "prune"], timeout=120)


def _row(label: str, count: int, size: int) -> str:
    """Returns one line of the plan's table."""
    return f"  {label:<20} {count:>4} x {size / 2**20:>10.1f} MiB"


def describe(plan: Plan) -> str:
    """Returns the plan as text: the count and size per kind of path, and the containers."""
    by_name: dict[str, list[int]] = {}
    for _, target, size in plan.paths:
        by_name.setdefault(target.name, []).append(size)
    lines = [_row(name, len(sizes), sum(sizes)) for name, sizes in sorted(by_name.items())]
    lines.append(_row("whole runs", len(plan.dropped), sum(plan.dropped.values())))
    lines.append(_row("cycle thumbnails", len(plan.thumbs), sum(s for _, s in plan.thumbs)))
    lines.append(_row("dependency caches", len(plan.caches), sum(s for _, s in plan.caches)))
    lines.append(f"  docker containers    {len(plan.containers):>4}  {' '.join(plan.containers)}")
    lines.append(f"  total                {plan.total() / 2**30:.2f} GiB")
    return "\n".join(lines)


def prune(dry_run: bool = False, pressure: bool = False) -> Plan:
    """Plans a prune and, unless `dry_run`, applies it and records the time."""
    plan = make_plan(datetime.now(UTC), pressure)
    if not dry_run:
        apply(plan)
        _stamp()
    return plan


def _stamp() -> None:
    """Records that a prune just ran."""
    atomic_write(paths.STATE_ROOT / "prune.stamp", json.dumps({"at": time.time()}))


def due(free: int) -> str | None:
    """Returns why the daemon should prune now: `pressure` when the free disk is low, `daily`
    when none ran for a day, else None."""
    if free < LOW_DISK_BYTES:
        return "pressure"
    stamp = paths.STATE_ROOT / "prune.stamp"
    if not stamp.exists() or time.time() - json.loads(stamp.read_text())["at"] > DAILY_SECONDS:
        return "daily"
    return None
