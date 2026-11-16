"""Stages S1-S4: commands run with the run's environment, per-stage logs, timeouts, summaries."""

import fnmatch
import re
import shlex
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from qg import affected, git, paths, proc
from qg.config import HUNT_RERUN_MINUTES, HUNT_RERUNS, Config, Reader, load_yaml
from qg.queue import CommandRun
from qg.sandbox import Sandbox
from qg.util import die, fill, read_tail

MAX_FAILURES = 30
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PYTEST_FINAL = re.compile(
    r"^(?:=+ )?((?:\d+ [A-Za-z]+(?:, \d+ [A-Za-z]+)*)|no tests ran) in ([0-9.]+)s"
    r"(?: \([0-9:]+\))?(?: =+)?\s*$",
    re.MULTILINE,
)
VITEST_TESTS = re.compile(r"^\s*Tests\s+(\d.*?)\s*$", re.MULTILINE)
PLAYWRIGHT_COUNT = re.compile(
    r"^\s+(\d+) (passed|failed|flaky|skipped|interrupted|did not run)\b(?: \([^)]*\))?\s*$",
    re.MULTILINE,
)
PLAYWRIGHT_NAME = re.compile(r"^\s+\[[^\]]+\] › \S.*$")
PYTEST_FAILURE = re.compile(r"^(?:FAILED|ERROR) \S.*$", re.MULTILINE)
VITEST_FAILURE = re.compile(r"^\s*FAIL\s+(\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Parsed:
    """What a test runner printed at the end of one command."""

    summary: str
    failures: list[str]
    tool: str | None
    passed: int
    failed: int
    skipped: int


@dataclass(frozen=True)
class Selection:
    """The Playwright specs to run and the flows of the surface map entries that selected them."""

    specs: list[str]
    flows: list[str]
    commands: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageResult:
    """Outcome of one stage: status, one-line summary, failing tests and what each command did."""

    status: str
    summary: str
    failures: list[str]
    commands: list[CommandRun]
    specs: list[str]
    flows: list[str]
    flakes: dict[str, str] = field(default_factory=dict)


def _count(counts: str, word: str) -> int:
    """Returns the number in front of `word` inside a counts string, 0 when absent."""
    found = re.search(rf"(\d+) {word}\b", counts)
    return int(found.group(1)) if found else 0


def _playwright_failures(text: str) -> list[str]:
    """Returns the failed test names Playwright lists under its final '<n> failed' line."""
    failures = []
    collecting = False
    for line in text.splitlines():
        counts = PLAYWRIGHT_COUNT.match(line)
        if counts:
            collecting = counts.group(2) == "failed"
        elif collecting and PLAYWRIGHT_NAME.match(line):
            failures.append(f"FAILED {line.strip().rstrip('─ ')}")
    return failures


def parse_output(raw: str) -> Parsed:
    """Reads pytest, vitest and Playwright summaries and failing tests out of command output."""
    text = ANSI.sub("", raw)
    summaries: list[str] = []
    tool: str | None = None
    passed = failed = skipped = 0
    pytest_lines = list(PYTEST_FINAL.finditer(text))
    if pytest_lines:
        counts, seconds = pytest_lines[-1].group(1), pytest_lines[-1].group(2)
        summaries.append(f"pytest {counts} in {seconds}s")
        tool = "pytest"
        passed = _count(counts, "passed")
        failed = _count(counts, "failed") + _count(counts, "errors?")
        skipped = _count(counts, "skipped")
    vitest_lines = list(VITEST_TESTS.finditer(text))
    if vitest_lines:
        counts = vitest_lines[-1].group(1)
        summaries.append(f"vitest {counts}")
        if tool is None:
            tool = "vitest"
            passed = _count(counts, "passed")
            failed = _count(counts, "failed")
            skipped = _count(counts, "skipped")
    playwright = {label: int(number) for number, label in PLAYWRIGHT_COUNT.findall(text)}
    if playwright:
        summaries.append(
            "playwright " + ", ".join(f"{n} {label}" for label, n in playwright.items())
        )
        if tool is None:
            tool = "playwright"
            passed = playwright.get("passed", 0) + playwright.get("flaky", 0)
            failed = playwright.get("failed", 0) + playwright.get("interrupted", 0)
            skipped = playwright.get("skipped", 0) + playwright.get("did not run", 0)
    failures = [m.group(0) for m in PYTEST_FAILURE.finditer(text)]
    failures += [f"FAIL {m.group(1)}" for m in VITEST_FAILURE.finditer(text)]
    failures += _playwright_failures(text)
    unique = list(dict.fromkeys(failures))
    return Parsed("; ".join(summaries), unique[:MAX_FAILURES], tool, passed, failed, skipped)


@dataclass(frozen=True)
class Surface:
    """One surface-map entry: the path globs it covers, the flows it names and the specs it ties."""

    globs: tuple[str, ...]
    flows: tuple[str, ...]
    specs: tuple[str, ...]


def surfaces(cfg: Config, repo: str, sha: str) -> list[Surface]:
    """Returns the surface-map entries of commit `sha`; empty when no surface map is configured."""
    playwright = cfg.playwright
    if playwright is None or not playwright.surface_map:
        return []
    source = f"{playwright.surface_map}@{sha[:8]}"
    text = git.show(repo, sha, playwright.surface_map)
    if text is None:
        die(f"stages.playwright.surface_map {playwright.surface_map!r} is not in {sha[:8]}")
    entries = load_yaml(text, source)
    reader = Reader(source)
    if not isinstance(entries, list):
        reader.fail("<root>", "expected a list of {paths, flows, specs} entries")
    found = []
    for index, raw in enumerate(entries):
        path = f"[{index}]"
        entry = reader.mapping(raw, path, required=("paths",), optional=("flows", "specs"))
        found.append(
            Surface(
                globs=reader.texts(entry["paths"], f"{path}.paths"),
                flows=reader.texts(entry.get("flows", []), f"{path}.flows"),
                specs=reader.texts(entry.get("specs", []), f"{path}.specs"),
            )
        )
    return found


def touches(surface: Surface, changed: list[str]) -> list[str]:
    """Returns the changed files that match one of the surface's path globs."""
    return [
        name for name in changed if any(fnmatch.fnmatchcase(name, glob) for glob in surface.globs)
    ]


def select_specs(
    cfg: Config, repo: str, sha: str, exists: Callable[[str], bool], everything: bool = False
) -> Selection:
    """Returns the critical specs plus those the surface map ties to the changed files.

    `everything` (the nightly run) takes every entry's specs, changed or not.
    """
    candidates = list(cfg.playwright.critical)
    flows: list[str] = []
    entries = surfaces(cfg, repo, sha)
    if entries:
        changed = git.changed_files(repo, sha)
        for entry in entries:
            if everything or touches(entry, changed):
                candidates.extend(entry.specs)
                flows.extend(entry.flows)
    specs = [spec for spec in dict.fromkeys(candidates) if exists(spec)]
    return Selection(specs, list(dict.fromkeys(flows)))


def select_tests(
    cfg: Config,
    name: str,
    changed: list[str],
    exists: Callable[[str], bool],
    everything: bool = False,
) -> Selection | None:
    """Returns the affected-test selection of the backend or frontend stage, None when not set.

    `changed` is `git diff --name-only base..sha`; the selection's `specs` hold the labels of
    what runs (test paths or per-package vitest modes) and its commands are added to the stage's
    own commands. A stage with no `affected` configuration runs exactly its commands.
    """
    if name == "backend":
        found = affected.backend(cfg.affected, changed, exists, everything)
    elif name == "frontend":
        found = affected.frontend(cfg.affected, changed, exists, everything)
    else:
        return None
    if found is None:
        return None
    return Selection(
        found.labels, [], tuple(found.commands), tuple(found.notes), tuple(found.problems)
    )


def split_specs(specs: list[str], shards: int) -> list[list[str]]:
    """Deals `specs` round-robin into at most `shards` non-empty lists, keeping their order."""
    count = max(1, min(shards, len(specs)))
    return [specs[index::count] for index in range(count)]


def merge_shards(
    results: list[StageResult], specs: list[str], flows: list[str], shards: int
) -> StageResult:
    """Merges the shards' outcomes into the one Playwright stage result.

    The stage fails when any shard failed; failures are the union of the shards' (first seen
    first, capped), `commands` hold every shard's, and the summary adds the test counts and
    names the shards that failed for another reason than failing tests.
    """
    commands = [run for result in results for run in result.commands]
    counts = {
        label: sum(getattr(run, label) for run in commands)
        for label in ("passed", "failed", "skipped")
    }
    failures = list(dict.fromkeys(item for result in results for item in result.failures))
    totals = ", ".join(f"{number} {label}" for label, number in counts.items() if number)
    parts = [f"playwright {totals or 'no tests'} across {len(results)} of {shards} shards"]
    for index, result in enumerate(results, start=1):
        if result.status == "failed" and not any(run.failed for run in result.commands):
            parts.append(f"shard {index}/{len(results)}: {result.summary}")
    status = "failed" if any(result.status == "failed" for result in results) else "passed"
    return StageResult(status, "; ".join(parts), failures[:MAX_FAILURES], commands, specs, flows)


PYTEST_NODE = re.compile(r"^FAILED\s+(\S+::\S+)")
PLAYWRIGHT_SPOT = re.compile(r"›\s*(\S+?\.\w+):(\d+):\d+")
ALWAYS = "always fails"
UNRUNNABLE = "not rerunnable"


def rerun_spec(failure: str) -> str | None:
    """Returns what to pass to the runner to run just this failed test, None when unknown.

    A pytest line gives its node id (`tests/x.py::test_y`), a Playwright line `file:line`.
    """
    text = ANSI.sub("", failure).strip()
    node = PYTEST_NODE.match(text)
    if node:
        return node.group(1)
    spot = PLAYWRIGHT_SPOT.search(text)
    return f"{spot.group(1)}:{spot.group(2)}" if spot else None


def flake_label(failed: int, runs: int) -> str:
    """Returns `always fails` when every rerun failed, else `flaky N/runs`."""
    return ALWAYS if failed == runs else f"flaky {failed}/{runs}"


def _only_tests_failed(result: StageResult) -> bool:
    """Tells whether a failed stage failed only by the tests it names: every command that exited
    non-zero reported failed tests, and they are as many as the stage lists."""
    exits = all(run.code == 0 or run.failed for run in result.commands)
    return exits and sum(run.failed for run in result.commands) == len(result.failures)


def flake_hunt(sandbox: Sandbox, cfg: Config, result: StageResult) -> StageResult:
    """Re-runs only the failed tests of a failed Playwright stage and labels each one.

    The failed tests run `HUNT_RERUNS` times with the stage's own command on the sandbox's
    ports and database. A test that failed in every rerun is `always fails`, one that passed
    some `flaky N/3` (N reruns failed). The stage passes when it failed only by the tests
    it names and every rerun of them passed, each one `flaky 0/3`: its failures are
    cleared, the summary and `flakes` keep the labels. Otherwise the stage stays failed.
    """
    specs = {line: rerun_spec(line) for line in result.failures}
    runnable = list(dict.fromkeys(spec for spec in specs.values() if spec))
    labels = {line: UNRUNNABLE for line, spec in specs.items() if not spec}
    clean = bool(runnable)
    if runnable:
        failed_runs: dict[str, int] = dict.fromkeys(runnable, 0)
        for index in range(1, HUNT_RERUNS + 1):
            log = paths.logs_dir(sandbox.run_id) / f"playwright-hunt{index}.log"
            again = run_commands(
                sandbox,
                cfg,
                "playwright",
                (cfg.playwright.run,),
                runnable,
                result.flows,
                log,
                limit=HUNT_RERUN_MINUTES,
            )
            clean = clean and again.status == "passed"
            named = {rerun_spec(line) for line in again.failures}
            crashed = again.status == "failed" and not again.failures
            for spec in runnable:
                failed_runs[spec] += spec in named or crashed
        labels |= {
            line: flake_label(failed_runs[spec], HUNT_RERUNS)
            for line, spec in specs.items()
            if spec
        }
    counts = Counter(
        "flaky" if label.startswith(("flaky", "intermitente")) else label
        for label in labels.values()
    )
    note = ", ".join(f"{n} {label}" for label, n in counts.items())
    summary = f"{result.summary}; flake hunt: {note}"
    passing = flake_label(0, HUNT_RERUNS)
    if clean and _only_tests_failed(result) and all(v == passing for v in labels.values()):
        return replace(result, status="passed", summary=summary, failures=[], flakes=labels)
    return replace(result, summary=summary, flakes=labels)


def merge_stress(results: list[StageResult], specs: list[str], flows: list[str]) -> StageResult:
    """Merges N full Playwright passes of one tree: every test that failed at least once.

    A test is the same across passes by its rerun spec (its node id or file:line), else by its
    line. Each failed test is labelled `always fails` (failed in every pass) or `flaky
    k/N` (failed in k of the N passes). The stage fails when any pass failed.
    """
    runs = len(results)
    seen: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for result in results:
        for key in dict.fromkeys(rerun_spec(line) or line for line in result.failures):
            counts[key] += 1
        for line in result.failures:
            seen.setdefault(rerun_spec(line) or line, line)
    failures = list(seen.values())[:MAX_FAILURES]
    flakes = {seen[key]: flake_label(counts[key], runs) for key in seen if seen[key] in failures}
    commands = [run for result in results for run in result.commands]
    broken = any(result.status == "failed" for result in results)
    tally = Counter(
        "flaky" if label.startswith(("flaky", "intermitente")) else label
        for label in flakes.values()
    )
    note = ", ".join(f"{n} {label}" for label, n in tally.items())
    summary = f"stress {runs} passes: {len(seen)} test(s) failed at least once" + (
        f" ({note})" if note else ""
    )
    status = "failed" if broken else "passed"
    return StageResult(status, summary, failures, commands, specs, flows, flakes)


def run_stress(
    sandbox: Sandbox, cfg: Config, selection: Selection | None, passes: int
) -> StageResult:
    """Runs the whole Playwright stage `passes` times on the sandbox and merges the passes."""
    results = [run_stage(sandbox, cfg, "playwright", selection) for _ in range(passes)]
    first = results[0]
    return merge_stress(results, first.specs, first.flows)


def _run_shards(sandbox: Sandbox, cfg: Config, specs: list[str], flows: list[str]) -> StageResult:
    """Runs the Playwright specs as shards at once, each on its own ports, database and log."""
    groups = split_specs(specs, cfg.playwright.shards)
    boxes = [sandbox.for_shard(index, cfg.playwright.shards) for index in range(len(groups))]
    results: list[StageResult | None] = [None] * len(groups)
    errors: list[BaseException] = []

    def work(index: int) -> None:
        """Runs one shard; what it raises is handed to the caller after every shard ended."""
        box = boxes[index]
        try:
            (box.shard_dir / "tmp").mkdir(parents=True, exist_ok=True)
            log = paths.logs_dir(box.run_id) / f"playwright-shard{index}.log"
            results[index] = run_commands(
                box, cfg, "playwright", (cfg.playwright.run,), groups[index], flows, log
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(i,), name=f"shard-{i}") for i in range(len(groups))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    merged_log = paths.logs_dir(sandbox.run_id) / "playwright.log"
    with merged_log.open("a") as out:
        for index in range(len(groups)):
            shard_log = paths.logs_dir(sandbox.run_id) / f"playwright-shard{index}.log"
            out.write(f"===== shard {index + 1}/{len(groups)}: {len(groups[index])} specs =====\n")
            out.write(shard_log.read_text() if shard_log.exists() else "")
    if errors:
        raise errors[0]
    done = [result for result in results if result is not None]
    return merge_shards(done, specs, flows, cfg.playwright.shards)


def run_stage(sandbox: Sandbox, cfg: Config, name: str, selection: Selection | None) -> StageResult:
    """Runs stage `name`'s commands in the sandbox and returns its outcome."""
    specs = selection.specs if selection else []
    flows = selection.flows if selection else []
    if selection and selection.problems:
        return StageResult("failed", "; ".join(selection.problems), [], [], specs, flows)
    if name == "playwright":
        if not specs:
            return StageResult("skipped", "no specs", [], [], [], flows)
        if cfg.playwright.shards > 1:
            return _run_shards(sandbox, cfg, specs, flows)
        templates: tuple[str, ...] = (cfg.playwright.run,)
    else:
        templates = (*cfg.commands(name), *(selection.commands if selection else ()))
    if not templates:
        return StageResult("passed", "no commands", [], [], [], [])
    log = paths.logs_dir(sandbox.run_id) / f"{name}.log"
    return run_commands(sandbox, cfg, name, templates, specs, flows, log, selection)


def run_commands(
    sandbox: Sandbox,
    cfg: Config,
    name: str,
    templates: tuple[str, ...],
    specs: list[str],
    flows: list[str],
    log: Path,
    selection: Selection | None = None,
    limit: int | None = None,
) -> StageResult:
    """Runs `templates` in order in the sandbox, logged to `log`, within the stage's time limit.

    The commands run in static's systemd scope for static, in qa_stack's on the QA lane, else in
    the tests lane's.
    """
    values = {**sandbox.values, "specs": " ".join(shlex.quote(spec) for spec in specs)}
    limit = limit or cfg.timeouts[name]
    deadline = time.monotonic() + limit * 60
    runs: list[CommandRun] = []
    summaries: list[str] = []
    failures: list[str] = []
    for index, template in enumerate(templates, start=1):
        where = f"command {index}/{len(templates)}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return StageResult(
                "failed",
                f"timed out after {limit} min before {where}",
                failures,
                runs,
                specs,
                flows,
            )
        command = fill(template, values)
        with log.open("a") as handle:
            handle.write(f"$ {command}\n")
        start = log.stat().st_size
        lane = "qa_stack" if sandbox.lane == "qa" else "static" if name == "static" else "tests"
        argv = sandbox.wrap(lane, proc.shell(command))
        result = proc.run_logged(
            argv,
            cwd=sandbox.src,
            env=sandbox.env,
            log=log,
            timeout=remaining,
        )
        parsed = parse_output(read_tail(log, start))
        runs.append(
            CommandRun(
                command=template,
                code=result.code,
                tool=parsed.tool,
                passed=parsed.passed,
                failed=parsed.failed,
                skipped=parsed.skipped,
                summary=parsed.summary,
            )
        )
        failures = list(dict.fromkeys([*failures, *parsed.failures]))[:MAX_FAILURES]
        if parsed.summary:
            summaries.append(parsed.summary)
        if result.timed_out:
            summary = f"timed out after {limit} min in {where}"
            return StageResult("failed", summary, failures, runs, specs, flows)
        if result.code != 0 and proc.killed_by_oom(result.code, argv):
            summary = f"{where} out of memory in lane {lane} (exit {result.code})"
            return StageResult("failed", summary, failures, runs, specs, flows)
        if result.code != 0:
            summary = f"{where} exited {result.code}" + (
                f": {parsed.summary}" if parsed.summary else ""
            )
            return StageResult("failed", summary, failures, runs, specs, flows)
    notes = list(selection.notes) if selection else []
    summary = "; ".join([*summaries, *notes]) or f"{len(templates)} command(s) ok"
    return StageResult("passed", summary, failures, runs, specs, flows)
