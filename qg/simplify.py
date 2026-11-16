"""S0, the simplify pass: a confined headless Claude Code reviews the diff and commits its fixes."""

import fnmatch
import json
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from qg import agent as agents
from qg import audit, backlog, git, jail, paths, proc, redact, settings
from qg.config import Config
from qg.queue import Request
from qg.sandbox import Sandbox, base_env
from qg.util import atomic_write, fill

PROMPT_FILE = Path(__file__).with_name("simplify_prompt.md")
TOOLS = "Read,Edit,Write,Glob,Grep"
STAT_LINES = 200
FILE_LINES = 400
PROTECTED_DIRS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "__tests__",
        "spec",
        "specs",
        "e2e",
        "cypress",
        "playwright",
        "__snapshots__",
        "snapshots",
        "fixtures",
        "mocks",
        "__mocks__",
        "alembic",
        "migrations",
        ".claude",
        ".github",
        ".git",
    }
)
ROOT_PROTECTED_DIRS = frozenset({"scripts", "tools"})
PROTECTED_NAMES = frozenset(
    {
        "conftest.py",
        "quality-gates-notes.md",
        "uv.lock",
        "yarn.lock",
        "package-lock.json",
        "makefile",
        "package.json",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
    }
)
PROTECTED_GLOBS = (
    "*.test.*",
    "*.spec.*",
    "test_*",
    "*_test.*",
    "*.snap",
    ".git*",
    ".env*",
    "*.config.*",
)
ALLOWED_MODES = ("100644", "100755")
GIT = (
    "git",
    "--literal-pathspecs",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
)
IDENTITY = ("-c", "user.name=qg simplify", "-c", "user.email=qg@localhost")


class SimplifyFailure(Exception):
    """A reason the simplify stage cannot go on; its text becomes the stage summary."""


@dataclass(frozen=True)
class SimplifyOutcome:
    """What S0 reports to the executor: the stage result and, when it committed, the new commit."""

    status: str
    summary: str
    cost_usd: float | None = None
    turns: int | None = None
    sha: str | None = None
    tree: str | None = None
    ref: str | None = None


def render_prompt(
    box: Sandbox,
    base: str,
    stat: str,
    files: list[str],
    max_turns: int,
    paths_allowed: tuple[str, ...],
    queued: list[backlog.Item],
) -> str:
    """Fills simplify_prompt.md with the base, sha, diffstat, changed files, output paths and the
    repository's simplify queue."""
    sdir = paths.simplify_dir(box.run_id)
    listed = files[:FILE_LINES]
    more = [f"[{len(files) - FILE_LINES} more files]"] if len(files) > FILE_LINES else []
    stat_lines = stat.splitlines()
    if len(stat_lines) > STAT_LINES:
        stat_lines = [*stat_lines[:STAT_LINES], f"[{len(stat_lines) - STAT_LINES} more lines]"]
    return fill(
        PROMPT_FILE.read_text(),
        {
            "sha": box.sha,
            "base": base,
            "diff_file": str(sdir / "diff.patch"),
            "summary_file": str(sdir / "summary.json"),
            "stat": "\n".join(stat_lines),
            "files": "\n".join(f"- {name}" for name in [*listed, *more]),
            "paths": ", ".join(f"`{name}`" for name in paths_allowed),
            "max_turns": str(max_turns),
            "queued": backlog.prompt_block(queued),
        },
    )


def _git(src: Path, *args: str) -> str:
    """Runs git in the sandbox checkout and returns its stdout; a failure raises SimplifyFailure."""
    result = proc.run([*GIT, "-C", str(src), *args])
    if result.returncode != 0:
        raise SimplifyFailure(f"git {args[0]} failed in the sandbox: {result.stderr.strip()}")
    return result.stdout


def porcelain(src: Path) -> set[str]:
    """Returns the `XY path` entries of `git status` in the sandbox, untracked files listed."""
    raw = _git(src, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {entry for entry in raw.split("\0") if entry}


def snapshot(src: Path) -> tuple[str, dict[str, tuple[int, int]]]:
    """Returns what an agent must leave alone in `src`: its .git file and every ignored file."""
    return (src / ".git").read_text(), ignored_files(src)


def ignored_files(src: Path) -> dict[str, tuple[int, int]]:
    """Returns every ignored file of the sandbox with its size and mtime, symlinks not followed."""
    listing = _git(src, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    found = {}
    for name in listing.split("\0"):
        if name:
            info = os.lstat(src / name)
            found[name] = (info.st_size, info.st_mtime_ns)
    return found


def _paths(entries: set[str]) -> list[str]:
    """Returns the sorted paths of porcelain entries."""
    return sorted(entry[3:] for entry in entries)


def _fold(part: str) -> str:
    """Normalises a path component so case and compatibility spellings cannot dodge a rule."""
    return unicodedata.normalize("NFKC", part).casefold()


def protected(name: str) -> bool:
    """Tells whether a path is a test, fixture, snapshot, lint registry or runner-executed file."""
    parts = [_fold(part) for part in Path(name).parts]
    if PROTECTED_DIRS.intersection(parts[:-1]) or parts[0] in ROOT_PROTECTED_DIRS:
        return True
    return parts[-1] in PROTECTED_NAMES or any(
        fnmatch.fnmatchcase(parts[-1], pattern) for pattern in PROTECTED_GLOBS
    )


def reviewable(box: Sandbox, base: str, source_dirs: tuple[str, ...]) -> list[str]:
    """Returns the files base..sha adds or changes under the source directories, protected
    files left out."""
    return [
        name
        for name in git.paths_between(box.repo, base, box.sha, "AM")
        if name.startswith(source_dirs) and not protected(name) and (box.src / name).is_file()
    ]


def _refuse_unsafe(
    src: Path, changed: list[str], in_diff: set[str], source_dirs: tuple[str, ...]
) -> None:
    """Fails the stage unless every changed path is a protected-free source file the diff touched.

    Everything else is refused, never dropped: a file outside the allowed source directories, a
    file the diff did not change, a test, fixture, snapshot, registry or tooling file, a symlink.
    """
    refused = []
    root = src.resolve()
    for name in changed:
        allowed = name in in_diff and name.startswith(source_dirs) and not protected(name)
        target = src / name
        inside = target.parent.resolve() == (root / Path(name).parent).resolve()
        if not allowed or not inside or target.is_symlink():
            refused.append(name)
    if refused:
        listed = ", ".join(refused[:10]) + (f" (+{len(refused) - 10})" if len(refused) > 10 else "")
        raise SimplifyFailure(
            f"the simplify agent changed {len(refused)} path(s) it may not touch: {listed}"
        )


def _refuse_modes(src: Path) -> None:
    """Fails the stage when a staged change adds a symlink or submodule or alters a file mode."""
    raw = _git(src, "diff", "--cached", "--raw", "-z", "--no-renames").split("\0")
    for meta, name in zip(raw[0::2], raw[1::2], strict=False):
        old, new = meta.split()[0].lstrip(":"), meta.split()[1]
        if new not in ALLOWED_MODES or (old != "000000" and old != new):
            raise SimplifyFailure(f"the simplify agent changed the mode of {name} ({old} to {new})")


def explain(summary: Path, secrets: tuple[str, ...]) -> dict[str, list[str]]:
    """Returns the agent's one-line `changes` (what and why), `left_alone` findings and the
    `unreviewed` files, redacted; a list is empty when its summary does not hold it."""
    try:
        said = json.loads(summary.read_text())
    except (OSError, ValueError):
        said = {}
    explained = {}
    for key in ("changes", "left_alone", "unreviewed"):
        items = said.get(key) if isinstance(said, dict) else None
        explained[key] = [
            redact.clean(" ".join(item.split()), secrets)
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, str)
        ]
    return explained


def _message(run_id: str, request: Request, base: str, changed: list[str], notes: list[str]) -> str:
    """Builds the commit message: `simplify:` first, what changed, the run id last."""
    lines = [
        f"simplify: {len(changed)} file(s) of {request.sha[:8]} against {base[:8]}",
        "",
        *([f"- {note}" for note in notes] or ["- the agent left no summary; see the files below"]),
        "",
        "Files:",
        *[f"- {name}" for name in changed],
        "",
        f"qg run {run_id}",
    ]
    return "\n".join(lines) + "\n"


def _commit(box: Sandbox, request: Request, base: str, changed: list[str], notes: list[str]) -> str:
    """Commits `changed` on top of the sandbox's HEAD and returns the new commit's sha."""
    src = box.src
    if _git(src, "rev-parse", "HEAD").strip() != box.sha:
        raise SimplifyFailure("the sandbox's HEAD moved while the simplify agent ran")
    _git(src, "add", "-A", "--", *changed)
    staged = _git(src, "diff", "--cached", "--name-only", "-z").split("\0")
    if sorted(name for name in staged if name) != sorted(changed):
        raise SimplifyFailure("the staged files differ from the files the agent changed")
    _refuse_modes(src)
    message = paths.simplify_dir(box.run_id) / "commit-message.txt"
    atomic_write(message, _message(box.run_id, request, base, changed, notes))
    _git(src, *IDENTITY, "-c", "commit.gpgsign=false", "commit", "-q", "-F", str(message))
    sha = _git(src, "rev-parse", "HEAD").strip()
    parent = _git(src, "rev-parse", f"{sha}^").strip()
    touched = _git(src, "diff", "--name-only", "-z", f"{box.sha}..{sha}").split("\0")
    if parent != box.sha or sorted(name for name in touched if name) != sorted(changed):
        raise SimplifyFailure("the simplify commit is not the requested sha plus the checked files")
    if porcelain(src):
        raise SimplifyFailure("the sandbox differs from the simplify commit after committing")
    return sha


def run_simplify(box: Sandbox, cfg: Config, request: Request) -> SimplifyOutcome:
    """Runs the confined /simplify agent over `base..sha`; commits and publishes what it fixes.

    A Claude account that cannot run the audit or the agent raises AccountFailure (qg.proc).
    """
    base = request.base
    if not base:
        return SimplifyOutcome("skipped", "no base: there is no diff to simplify")
    started = time.monotonic()
    limits = cfg.simplify
    sdir = paths.simplify_dir(box.run_id)
    sdir.mkdir(exist_ok=True)
    hidden = redact.run_secrets(cfg)
    try:
        atomic_write(sdir / "diff.patch", git.diff_between(box.repo, base, box.sha))
        files = git.paths_between(box.repo, base, box.sha)
        audited = (
            audit.run(box, cfg, base, reviewable(box, base, limits.paths), hidden)
            if cfg.audit is not None and request.mode == "nightly"
            else None
        )
        needed = cfg.timeouts["simplify"] * 60 + jail.LOGIN_MARGIN_SECONDS
        jail.ensure_login(jail.claude_binary(), base_env(), needed)
        if porcelain(box.src):
            raise SimplifyFailure("the sandbox was not clean before the simplify agent ran")
        gitfile, ignored_before = snapshot(box.src)
        queued = backlog.load(box.repo)
        accepted = {item.file for item in queued if item.status == backlog.ACCEPTED}
        in_diff = set(git.paths_between(box.repo, base, box.sha, "AM")) | accepted
        prompt = render_prompt(
            box,
            base,
            git.stat_between(box.repo, base, box.sha),
            files,
            limits.max_turns,
            limits.paths,
            queued,
        )
        limit = min(
            limits.time_limit_minutes * 60,
            cfg.stage_minutes("simplify") * 60 - (time.monotonic() - started),
        )
        agent = agents.confined(
            box,
            prompt,
            name="simplify",
            tools=TOOLS,
            edits=True,
            max_turns=limits.max_turns,
            max_cost_usd=limits.max_cost_usd,
            out_dir=sdir,
            limit=limit,
            needed=needed,
        )
        problems, turns = agent.problems, agent.turns
        cost = _total(agent.cost_usd, audited.cost_usd if audited else None)
        stats = ", ".join(
            [
                *([f"{turns} turns"] if turns is not None else []),
                *([f"${cost:.2f}"] if cost is not None else []),
            ]
        )
        if problems:
            return SimplifyOutcome("failed", "; ".join(problems), cost, turns)
        gitfile_after, ignored_after = snapshot(box.src)
        if gitfile_after != gitfile:
            raise SimplifyFailure("the simplify agent rewrote the sandbox's .git file")
        if ignored_after != ignored_before:
            raise SimplifyFailure("the simplify agent added, removed or changed an ignored file")
        explained = explain(sdir / "summary.json", hidden)
        atomic_write(sdir / "explained.json", json.dumps(explained, indent=2) + "\n")
        added = backlog.ingest(box.repo, box.run_id, box.sha, explained["left_alone"], box.src)
        notes = [
            *([audited.note] if audited else []),
            *([] if (sdir / "summary.json").is_file() else ["the agent wrote no summary.json"]),
            *(
                [f"{len(explained['unreviewed'])} file(s) unreviewed"]
                if explained["unreviewed"]
                else []
            ),
            *([f"{len(added)} queued for the operator"] if added else []),
            *([stats] if stats else []),
        ]
        said = f" ({'; '.join(notes)})" if notes else ""
        changed = _paths(porcelain(box.src))
        if not changed:
            return SimplifyOutcome("passed", "nothing to simplify" + said, cost, turns)
        try:
            _refuse_unsafe(box.src, changed, in_diff, limits.paths)
        except SimplifyFailure:
            atomic_write(sdir / "rejected.diff", _git(box.src, "diff", "HEAD"))
            raise
        sha = _commit(box, request, base, changed, explained["changes"])
        tree = git.tree_of(box.repo, sha)
        atomic_write(sdir / "applied.diff", git.diff_between(box.repo, request.sha, sha))
        try:
            ref = git.publish_simplified(box.repo, box.run_id, sha)
        except SystemExit as exc:
            raise SimplifyFailure(f"the simplified ref could not be created: {exc}") from exc
        closed = backlog.close(box.repo, box.run_id, explained["changes"])
        done = f"; fixed from the queue: {', '.join(closed)}" if closed else ""
        summary = f"{len(changed)} file(s) simplified in {sha[:12]}, published as {ref}{done}"
        return SimplifyOutcome("passed", summary + said, cost, turns, sha, tree, ref)
    except proc.Infrastructure:
        raise
    except (SimplifyFailure, audit.AuditFailure, jail.JailError, settings.SettingsError) as exc:
        return SimplifyOutcome("failed", str(exc))


def _total(*costs: float | None) -> float | None:
    """Returns the sum of the known costs, None when none is known."""
    known = [cost for cost in costs if cost is not None]
    return sum(known) if known else None
