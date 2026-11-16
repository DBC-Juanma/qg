"""The mutant hunt: after the backend stage passed, small mutations of the Python lines the change
adds are run against the tests that cover each file; a mutant they do not catch is a gap in those
tests. It only reports, in the stage summary and runs/<id>/mutants.json; it never fails a stage.
"""

import ast
import json
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from qg import affected, git, simplify, stages
from qg.config import Affected, Config
from qg.probe import ProbeFailure, scratch
from qg.sandbox import Sandbox
from qg.util import atomic_write

SWAPS: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.And: ast.Or,
    ast.Or: ast.And,
}
SYMBOLS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.Gt: ">",
    ast.LtE: "<=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.And: "and",
    ast.Or: "or",
}
SHOWN = 5


@dataclass(frozen=True)
class Mutant:
    """One mutation of a changed line: where, what, and how the tests took it.

    `outcome` is killed (the tests failed), survived (they passed) or undecided (they could not
    run cleanly or ran out of time).
    """

    file: str
    line: int
    mutation: str
    outcome: str = ""


@dataclass(frozen=True)
class Site:
    """Where one mutant goes: its line, the bytes `start`:`end` it replaces with `text`, and the
    mutation it makes."""

    line: int
    start: int
    end: int
    text: str
    mutation: str


def _starts(raw: bytes) -> list[int]:
    """Returns the byte offset where each line of `raw` starts."""
    starts = [0]
    for line in raw.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _swap(raw: bytes, starts: list[int], left: ast.expr, right: ast.expr, op: type) -> tuple:
    """Returns the edit that swaps the operator `op` written between `left` and `right`, or None
    when that text is more than the operator (a parenthesis, a comment)."""
    a = starts[left.end_lineno - 1] + left.end_col_offset
    b = starts[right.lineno - 1] + right.col_offset
    between = raw[a:b].decode()
    if " ".join(between.split()) != SYMBOLS[op]:
        return None
    lead = between[: len(between) - len(between.lstrip())]
    trail = between[len(between.rstrip()) :]
    return a, b, f"{lead}{SYMBOLS[SWAPS[op]]}{trail}", f"`{SYMBOLS[op]}` -> `{SYMBOLS[SWAPS[op]]}`"


def _edit(raw: bytes, starts: list[int], node: ast.AST) -> tuple | None:
    """Returns how to mutate `node` in place: start and end byte, new text and description; None
    when it is no mutation site. Only that text changes, so format and comments stay."""
    if isinstance(node, ast.Compare) and type(node.ops[0]) in SWAPS:
        return _swap(raw, starts, node.left, node.comparators[0], type(node.ops[0]))
    if isinstance(node, ast.BoolOp):
        return _swap(raw, starts, node.values[0], node.values[1], type(node.op))
    if isinstance(node, ast.Constant) and node.lineno == node.end_lineno:
        span = (
            starts[node.lineno - 1] + node.col_offset,
            starts[node.lineno - 1] + node.end_col_offset,
        )
        if isinstance(node.value, bool):
            return *span, str(not node.value), f"`{node.value}` -> `{not node.value}`"
        if type(node.value) is int:
            return *span, str(node.value + 1), f"`{node.value}` -> `{node.value + 1}`"
        return None
    if isinstance(node, ast.Return) and node.value is not None:
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            return None
        value = node.value
        a = starts[value.lineno - 1] + value.col_offset
        b = starts[value.end_lineno - 1] + value.end_col_offset
        return a, b, "None", "`return …` -> `return None`"
    return None


def sites(source: str, lines: set[int]) -> list[Site]:
    """Returns the mutations of `source` on `lines`, in walk order."""
    raw = source.encode()
    starts = _starts(raw)
    found = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        edit = _edit(raw, starts, node) if line in lines else None
        if edit is not None:
            found.append(Site(line, *edit))
    return found


def mutate(source: str, site: Site) -> str:
    """Returns `source` with the mutant `site` applied."""
    raw = source.encode()
    return (raw[: site.start] + site.text.encode() + raw[site.end :]).decode()


def _by_line(found: list[Site]) -> list[Site]:
    """Returns `found` reordered one changed line at a time: every line's first mutant, then
    every line's second, and so on."""
    lines: dict[int, list[Site]] = {}
    for site in found:
        lines.setdefault(site.line, []).append(site)
    ordered = []
    while any(lines.values()):
        ordered += [queue.pop(0) for queue in lines.values() if queue]
    return ordered


def pick(candidates: dict[str, list[Site]], limit: int) -> list[tuple[str, Site]]:
    """Returns up to `limit` sites, one file and one changed line at a time, so a few long files
    or lines do not take every mutant."""
    queues = {name: _by_line(found) for name, found in candidates.items()}
    chosen: list[tuple[str, Site]] = []
    while len(chosen) < limit and any(queues.values()):
        for name in sorted(queues):
            if queues[name] and len(chosen) < limit:
                chosen.append((name, queues[name].pop(0)))
    return chosen


def _candidates(
    box: Sandbox, cfg: Config
) -> tuple[dict[str, list[Site]], dict[str, tuple[str, ...]], list[str]]:
    """Returns the mutation sites of every changed Python product file of the run's commit, the
    commands of the tests verify.yml's map gives each, and the files it set aside, with why: one
    it could not parse or one the map gives no test."""
    assert cfg.affected.backend is not None
    mapped = replace(cfg.affected, backend=replace(cfg.affected.backend, always=()))
    base = git.out(box.repo, "merge-base", box.sha, git.base_ref(box.repo))
    found = {}
    covering = {}
    skipped = []
    for name in git.changed_files(box.repo, box.sha):
        source = box.src / name
        if (
            not name.endswith(".py")
            or not name.startswith(cfg.simplify.paths)
            or simplify.protected(name)
            or not source.is_file()
        ):
            continue
        diff = git.out(box.repo, "diff", "-U0", f"{base}..{box.sha}", "--", name)
        try:
            here = sites(source.read_text(), git.added_lines(diff))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            skipped.append(f"{name}: {type(exc).__name__}")
            continue
        commands = _covering(mapped, box.src, name) if here else None
        if here and commands is None:
            skipped.append(f"{name}: no tests in the map")
        elif here and commands is not None:
            found[name], covering[name] = here, commands
    return found, covering, skipped


def _tests(
    box: Sandbox, cfg: Config, src: Path, commands: tuple[str, ...], deadline: float, log: Path
) -> set[str] | None:
    """Runs `commands` in `src` within `deadline`; returns the FAILED and ERROR lines, None when
    they did not finish."""
    minutes = int((deadline - time.monotonic()) // 60)
    if minutes < 1:
        return None
    limit = min(cfg.timeouts["backend"], minutes)
    ran = stages.run_commands(
        replace(box, src=src), cfg, "backend", commands, [], [], log, limit=limit
    )
    if ran.summary.startswith("timed") or (ran.status != "passed" and not ran.failures):
        return None
    return set(ran.failures)


def _covering(mapped: Affected, src: Path, name: str) -> tuple[str, ...] | None:
    """Returns the commands of the tests `mapped` selects for `name` in `src`, None when it
    cannot select them."""
    found = affected.backend(mapped, [name], lambda path: (src / path).exists())
    assert found is not None
    return None if found.problems else tuple(found.commands)


def _try(
    box: Sandbox,
    cfg: Config,
    src: Path,
    name: str,
    site: Site,
    commands: tuple[str, ...],
    before: set[str],
    deadline: float,
    log: Path,
) -> str:
    """Runs `commands` in `src` with the mutant `site` of `name`; returns the outcome: killed only
    by a failure the unmutated run `before` does not have."""
    target = src / name
    original = target.read_text()
    target.write_text(mutate(original, site))
    shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
    try:
        after = _tests(box, cfg, src, commands, deadline, log)
    finally:
        target.write_text(original)
        shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
    if after is None:
        return "undecided"
    return "killed" if after - before else "survived"


def hunt(box: Sandbox, cfg: Config) -> str:
    """Runs the mutant hunt of the run's commit on a scratch worktree; returns the note for the
    backend stage's summary, empty when verify.yml configures no hunt or nothing changed."""
    if cfg.mutants is None:
        return ""
    deadline = time.monotonic() + cfg.mutants.time_limit_minutes * 60
    candidates, covering, skipped = _candidates(box, cfg)
    unread = f"; not analyzed: {', '.join(skipped)}" if skipped else ""
    chosen = pick(candidates, cfg.mutants.max)
    if not chosen:
        return f"mutants: none{unread}" if skipped else ""
    home = box.run_dir / "mutants"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    tried: list[Mutant] = []
    baselines: dict[tuple[str, ...], set[str] | None] = {}
    try:
        with scratch(box.repo, box.sha, home / "src") as src:
            for number, (name, site) in enumerate(chosen, start=1):
                commands = covering[name]
                if commands not in baselines:
                    unmutated = home / f"baseline-{len(baselines) + 1}.log"
                    baselines[commands] = _tests(box, cfg, src, commands, deadline, unmutated)
                before = baselines[commands]
                log = home / f"mutant-{number}.log"
                outcome = (
                    "undecided"
                    if before is None
                    else _try(box, cfg, src, name, site, commands, before, deadline, log)
                )
                tried.append(Mutant(name, site.line, site.mutation, outcome))
    except ProbeFailure as exc:
        return f"mutants: could not be tried ({exc})"
    body = json.dumps([asdict(m) for m in tried], indent=2, ensure_ascii=False) + "\n"
    atomic_write(box.run_dir / "mutants.json", body)
    alive = [m for m in tried if m.outcome == "survived"]
    left = sum(1 for m in tried if m.outcome == "undecided")
    shown = "; ".join(f"{m.file}:{m.line} {m.mutation}" for m in alive[:SHOWN])
    more = f" (+{len(alive) - SHOWN})" if len(alive) > SHOWN else ""
    note = f"mutants: {len(alive)} alive of {len(tried)}"
    if left:
        note += f", {left} undecided"
    return note + (f" ({shown}{more})" if alive else "") + unread
