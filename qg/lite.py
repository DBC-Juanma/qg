"""`qg lite`: the cheap inner loop. A foreground check of one commit with the deterministic stages
only (static commands, the rules ratchet, the affected tests that need no database, the mutant
hunt), in a detached worktree with the cached dependencies and no Postgres, stack or agent.

It never enters the queue and writes nothing under runs/, so no `qg check`, reuse or report can
ever read it: it gives no deploy verdict. At most SLOTS run at once machine-wide, each one only
once MemAvailable fits its tests cap.
"""

import fcntl
import fnmatch
import json
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType

from qg import (
    admission,
    affected,
    config,
    git,
    inplace,
    mutants,
    paths,
    peaks,
    proc,
    rules,
    stages,
)
from qg.config import Config
from qg.probe import scratch
from qg.sandbox import Sandbox, install_deps, venv_for
from qg.util import atomic_write, die, human, read_tail

SLOTS = 2
PEAKS = "peaks.json"
SAMPLE_SECONDS = 0.5
PREFIX = "lite-"
KEEP_SECONDS = 86400
MUTANT_MINUTES = 2
TAIL_LINES = 20
PASSED, FAILED, SKIPPED, INFO = "passed", "failed", "skipped", "info"


@dataclass(frozen=True)
class Step:
    """One line of the outcome: the step, its status, a summary, what to fix and its time."""

    name: str
    status: str
    summary: str
    lines: tuple[str, ...] = ()
    seconds: float = 0.0


def _cancel(signum: int, frame: FrameType | None) -> None:
    """SIGTERM and SIGINT handler: later signals are ignored and the first raises Cancelled."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise proc.Cancelled


def _lower_priority() -> None:
    """Puts this process and everything it starts at nice 10 and the idle I/O class."""
    os.nice(10)
    done = proc.run(["ionice", "-c3", "-p", str(os.getpid())])
    if done.returncode != 0:
        die(f"ionice -c3 failed: {done.stderr.strip()}")


@contextmanager
def _slot() -> Iterator[int]:
    """Holds one of the SLOTS machine-wide lite locks, waiting while all are taken; yields it."""
    paths.LITE.mkdir(parents=True, exist_ok=True)
    began, told = time.monotonic(), False
    while True:
        for index in range(SLOTS):
            handle = (paths.LITE / f"slot-{index}.lock").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            waited = time.monotonic() - began
            print(f"slot      {index + 1} of {SLOTS} (waited {human(waited)})", flush=True)
            try:
                yield index
            finally:
                handle.close()
            return
        if not told:
            print(f"waiting for a lite slot: {SLOTS} lite checks run at once", flush=True)
            told = True
        time.sleep(admission.POLL_SECONDS)


def _history() -> dict[str, list[int]]:
    """Returns the peak bytes of the last lite checks per repository, newest first."""
    path = paths.LITE / PEAKS
    return json.loads(path.read_text()) if path.exists() else {}


def _remember(repo: str, peak: int) -> None:
    """Records the peak of a lite check of `repo`, keeping its last peaks.HISTORY_RUNS."""
    known = _history()
    known[repo] = [peak, *known.get(repo, [])][: peaks.HISTORY_RUNS]
    atomic_write(paths.LITE / PEAKS, json.dumps(known, indent=2) + "\n")


@contextmanager
def _measured(lite_id: str, repo: str) -> Iterator[None]:
    """Samples the biggest scope of the check while the block runs and records it for `repo`."""
    stop = threading.Event()
    seen = [0]

    def loop() -> None:
        """Samples the check's live scopes every SAMPLE_SECONDS until `stop`."""
        while not stop.wait(SAMPLE_SECONDS):
            seen[0] = max(seen[0], *peaks.sample(lite_id).values(), 0)

    thread = threading.Thread(target=loop, name="lite-peaks", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        if seen[0]:
            _remember(repo, seen[0])


def _wait_for_memory(repo: str, memory_max: str) -> None:
    """Blocks until MemAvailable admits the check: its measured peak, else its tests cap."""
    seen = _history().get(repo, [])
    need = admission.need_for(memory_max, max(seen) if seen else None)
    told = False
    while not admission.fits(admission.available_memory(), need):
        if not told:
            print(admission.memory_reason(need), flush=True)
            told = True
        time.sleep(admission.POLL_SECONDS)


def _sweep() -> None:
    """Removes what lite checks older than a day left under the lite directory."""
    for entry in paths.LITE.iterdir():
        if entry.name.startswith(PREFIX) and time.time() - entry.stat().st_mtime > KEEP_SECONDS:
            shutil.rmtree(entry)


def limits(cfg: Config) -> dict[str, tuple[str, str]]:
    """Returns the lane limits of a lite check: the run's, its tests capped like the static lane."""
    lanes = cfg.resources.lanes
    return {**lanes, "tests": lanes["static"]}


def keeps(cfg: Config, name: str) -> bool:
    """Tells whether test path `name` is one `lite.tests` lists as needing no database."""
    assert cfg.lite is not None
    return not cfg.lite.tests or any(fnmatch.fnmatchcase(name, g) for g in cfg.lite.tests)


def view(cfg: Config) -> Config:
    """Returns `cfg` whose affected.backend runs `lite.command` on the tests lite may run only."""
    backend, lite = cfg.affected.backend, cfg.lite
    assert backend is not None and lite is not None

    def kept(tests: tuple[str, ...]) -> tuple[str, ...]:
        """Returns the tests of `tests` that need no database."""
        return tuple(name for name in tests if keeps(cfg, name))

    narrowed = replace(
        backend,
        command=lite.command,
        always=kept(backend.always),
        all_tests=kept(backend.all_tests),
        map=tuple(replace(entry, tests=kept(entry.tests)) for entry in backend.map),
    )
    return replace(cfg, affected=replace(cfg.affected, backend=narrowed))


def _tail(log: Path, start: int) -> tuple[str, ...]:
    """Returns the last lines `log` got after offset `start`, without terminal colours."""
    text = stages.ANSI.sub("", read_tail(log, start))
    return tuple(line for line in text.splitlines() if line.strip())[-TAIL_LINES:]


def static_step(box: Sandbox, cfg: Config) -> Step:
    """Runs stages.static one command at a time, stopping at the first that fails."""
    templates = cfg.command_stages.get("static", ())
    if not templates:
        return Step("static", SKIPPED, "verify.yml has no stages.static")
    log = box.run_dir / "logs" / "static.log"
    for template in templates:
        start = log.stat().st_size if log.exists() else 0
        result = stages.run_commands(box, cfg, "static", (template,), [], [], log)
        if result.status == FAILED:
            summary = f"`{template}`: {result.summary}"
            return Step("static", FAILED, summary, tuple(result.failures) or _tail(log, start))
    return Step("static", PASSED, f"{len(templates)} command(s) ok")


def rules_step(box: Sandbox, cfg: Config, base: str) -> Step:
    """Runs the architecture rules ratchet of `base..sha` in the static lane's scope; with
    `rules.enforce` off its violations are shown but never fail."""
    if cfg.rules is None:
        return Step("rules", SKIPPED, "verify.yml has no rules: section")
    verdict = rules.check(
        box.repo,
        cfg.rules,
        base,
        box.sha,
        lambda argv: box.wrap("static", argv),
        cfg.timeouts["static"] * 60,
        box.run_dir,
    )
    hits = tuple(str(hit) for hit in verdict.regressions)
    if verdict.passed:
        return Step("rules", PASSED, verdict.summary)
    if not cfg.rules.enforce:
        return Step("rules", INFO, f"{verdict.summary} (advisory)", hits)
    return Step("rules", FAILED, verdict.summary, hits)


def tests_step(box: Sandbox, cfg: Config, changed: list[str]) -> Step:
    """Runs the affected tests of `changed` that need no database, with `lite.command`."""
    if cfg.lite is None:
        return Step("tests", SKIPPED, "not configured for lite: add `lite:` to verify.yml")
    narrowed = view(cfg)
    assert narrowed.affected.backend is not None
    found = affected.backend(
        narrowed.affected, changed, lambda name: keeps(cfg, name) and (box.src / name).exists()
    )
    problems = [problem for problem in found.problems if problem != affected.NONE_SELECTED]
    if problems:
        return Step("tests", FAILED, "; ".join(problems))
    if not found.labels:
        return Step("tests", SKIPPED, "no affected test that needs no database")
    log = box.run_dir / "logs" / "tests.log"
    result = stages.run_commands(box, narrowed, "backend", tuple(found.commands), [], [], log)
    lines = tuple(result.failures) or (_tail(log, 0) if result.status == FAILED else ())
    notes = "".join(f"; {note}" for note in found.notes)
    summary = f"{len(found.labels)} test path(s): {result.summary}{notes}"
    return Step("tests", result.status, summary, lines)


def mutants_step(box: Sandbox, cfg: Config, tests: Step) -> Step:
    """Runs the mutant hunt on the added lines, bounded to MUTANT_MINUTES; it only informs."""
    if cfg.mutants is None:
        return Step("mutants", SKIPPED, "verify.yml has no mutants: section")
    if cfg.lite is None:
        return Step("mutants", SKIPPED, "needs lite: to pick the tests each mutant runs")
    if tests.status != PASSED:
        return Step("mutants", SKIPPED, "only after the tests passed")
    minutes = min(MUTANT_MINUTES, cfg.mutants.time_limit_minutes)
    bounded = replace(view(cfg), mutants=replace(cfg.mutants, time_limit_minutes=minutes))
    note = mutants.hunt(box, bounded)
    return Step("mutants", INFO, note or "no added line to mutate")


def _show(step: Step) -> Step:
    """Prints one step: status, summary and time, then what to fix, indented; returns it."""
    print(f"{step.name:<9} {step.status:<8} {step.summary}  ({human(step.seconds)})", flush=True)
    for line in step.lines:
        print(f"    {line}", flush=True)
    return step


def _step(name: str, action: Callable[[], Step]) -> Step:
    """Runs, times and prints one step; a crash of its machinery is that step failing, so the
    other steps still run."""
    began = time.monotonic()
    try:
        step = action()
    except proc.Cancelled:
        raise
    except (Exception, SystemExit) as exc:
        step = Step(name, FAILED, f"could not run: {exc or repr(exc)}")
    return _show(replace(step, seconds=time.monotonic() - began))


def _header(where: str, ref: str, sha: str, base: str, changed: list[str]) -> None:
    """Prints what is checked: the commit, its base, and a dirty worktree's changes left out."""
    print(
        f"qg lite {sha[:12]} ({git.branch_of(where, ref)}) against {base[:12]}, its merge-base "
        f"with {git.base_ref(where)}: {len(changed)} changed file(s)"
    )
    top = inplace.toplevel(where)
    if top and inplace.head(top) == sha and inplace.dirty(top):
        print("note      the worktree has uncommitted changes: lite checks only the commit")


def _check(box: Sandbox, cfg: Config, base: str, changed: list[str]) -> list[Step]:
    """Installs the cached dependencies, then runs the steps one after the other."""
    began = time.monotonic()
    install_deps(box, cfg, box.run_dir / "logs" / "setup.log", cfg.timeouts["setup"] * 60)
    notes = "; ".join(box.notes) or "no dependencies"
    _show(Step("setup", PASSED, notes, seconds=time.monotonic() - began))
    steps = [
        _step("static", lambda: static_step(box, cfg)),
        _step("rules", lambda: rules_step(box, cfg, base)),
    ]
    tests = _step("tests", lambda: tests_step(box, cfg, changed))
    return [*steps, tests, _step("mutants", lambda: mutants_step(box, cfg, tests))]


def run(where: str, ref: str) -> int:
    """Checks commit `ref` of the repository at `where`; returns 0 when green, 1 when red."""
    began = time.monotonic()
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    _lower_priority()
    sha = git.resolve_commit(where, ref)
    repo = git.canonical_repo(where)
    loaded = config.load(repo, sha)
    cfg = loaded.config
    base = rules.stage_base(repo, "", sha)
    changed = git.change_paths(repo, "", sha)
    _header(where, ref, sha, base, changed)
    print(f"config    {loaded.ref} @ {loaded.ref_sha[:12]}")
    if loaded.branch_differs:
        print(f"note      {config.VERIFY_PATH} changed in this branch: not used")
    lite_id = f"{PREFIX}{paths.new_run_id()}"
    home = paths.LITE / lite_id
    lanes = limits(cfg)
    with _slot():
        _sweep()
        (home / "logs").mkdir(parents=True)
        _wait_for_memory(repo, lanes["tests"][1])
        try:
            with scratch(repo, sha, home / "src") as src, _measured(lite_id, repo):
                box = Sandbox(
                    run_id=lite_id,
                    repo=repo,
                    sha=sha,
                    src=src,
                    run_dir=home,
                    venv=venv_for(src),
                    db=cfg.postgres.db,
                    ports={},
                    limits=lanes,
                )
                steps = _check(box, cfg, base, changed)
        finally:
            proc.stop_lanes(lite_id)
    failed = [step.name for step in steps if step.status == FAILED]
    took = human(time.monotonic() - began)
    verdict = f"RED: {', '.join(failed)} failed" if failed else "GREEN"
    print(f"{verdict} ({took}; lite gives no deploy verdict: `qg verify` runs the gate)")
    print(f"logs      {home / 'logs'}")
    return 1 if failed else 0
