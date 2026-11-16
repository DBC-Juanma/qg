"""What a phase's QA covers, and whether a later commit touched it.

Every decision fails closed: a diff git cannot compute counts as touched.
"""

import fnmatch
import json
import re

from qg import cycles, git, paths
from qg.queue import RunState

PLAN_MARK = "## Plan given with --plan"
PATHS_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?paths:[ \t]*(.+)$", re.MULTILINE)
QA_MODES = ("phase", "verify")
SURFACE_DIRS = ("frontend/", "src/")
PHASE_TAG = re.compile(
    r"(?<![A-Z0-9])([A-Z][A-Z0-9]*-\d+)\s*[-·:.]\s*(P\d+(?:\.\d+)?)(?![0-9A-Z]|\.\d)"
)


def snapshot(explicit: str) -> str:
    """Returns the plan.md body that records the plan the operator gave a run with no base."""
    return f"# Plan given without a base\n\n{PLAN_MARK}\n\n{explicit.rstrip()}\n"


def explicit_plan(run_id: str) -> str:
    """Returns the plan the operator gave a run (the part of its plan.md after the commit list)."""
    file = paths.plan_file(run_id)
    if not file.is_file():
        return ""
    _, mark, rest = file.read_text(errors="replace").partition(PLAN_MARK)
    return rest.strip() if mark else ""


def declared_paths(text: str) -> list[str]:
    """Returns the globs a plan declares on `paths: a/**, b.py` lines."""
    found = []
    for line in PATHS_LINE.findall(text):
        found += [item.strip() for item in line.split(",") if item.strip()]
    return found


def _hit(glob: str, path: str) -> bool:
    """Tells whether `path` matches a declared glob, a `**/` standing for any directories or none.

    A path under the glob's literal prefix counts too, so a glob never misses what it names.
    """
    prefix = re.split(r"[*?\[]", glob, maxsplit=1)[0]
    return (
        fnmatch.fnmatchcase(path, glob)
        or fnmatch.fnmatchcase(path, glob.replace("**/", ""))
        or (bool(prefix) and path.startswith(prefix))
    )


def _anchored(repo: str, shas: tuple[str, ...], globs: list[str]) -> bool:
    """Tells whether some declared glob matches a file of the repository at any of `shas`.

    A `paths:` line that names nothing in the repository (prose, a typo) declares no scope.
    """
    for sha in shas:
        files = git.out(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
        if any(_hit(glob, name) for glob in globs for name in files):
            return True
    return False


def _finding_owners(repo: str) -> dict[str, set[str]]:
    """Returns each finding id of the repository's phase cycles with the `PRD:PHASE` keys that hold it."""
    owners: dict[str, set[str]] = {}
    for cycle in cycles.list_all():
        if cycle.repo != repo or not cycle.prd or not cycle.phase:
            continue
        for run_id in cycle.runs:
            file = paths.findings_file(run_id)
            if not file.is_file():
                continue
            for item in json.loads(file.read_text()).get("findings", []):
                owners.setdefault(str(item["id"]), set()).add(f"{cycle.prd}:{cycle.phase}")
    return owners


def attributed(subject: str, owners: dict[str, set[str]]) -> set[str]:
    """Returns the `PRD:PHASE` keys a commit subject names by phase tag, branch name or finding id.

    Empty when it names none: such a commit belongs to every phase.
    """
    upper = subject.upper()
    keys = {f"{prd}:{phase}" for prd, phase in PHASE_TAG.findall(upper)}
    for finding, held in owners.items():
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(finding)}(?![A-Za-z0-9])", subject):
            keys |= held
    return keys


def _commits(repo: str, older: str, newer: str, owners: dict[str, set[str]]):
    """Yields (keys, files) of each first-parent commit in `older..newer`."""
    log = git.out(repo, "log", "--first-parent", "--format=%H%x1f%s", f"{older}..{newer}")
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        files = git.out(repo, "diff", "--name-only", "--no-renames", f"{sha}^", sha).splitlines()
        yield attributed(subject, owners), files


def _release_touched(repo: str, source: RunState, base: str, new_sha: str, key: str):
    """Returns the files changed since `source` that touch the phase `key`, by commit attribution.

    The phase's own files are those of the commits in `base..source.sha` it owns or nobody does.
    A later commit it owns counts whole; one nobody owns, or another phase owns, counts only
    where it meets the phase's own files.
    """
    owners = _finding_owners(repo)
    own: set[str] = set()
    for keys, files in _commits(repo, base, source.sha, owners):
        if not keys or key in keys:
            own.update(files)
    hit: set[str] = set()
    for keys, files in _commits(repo, source.sha, new_sha, owners):
        hit.update(files if key in keys else (name for name in files if name in own))
    return (
        sorted(hit)
        if own
        else [
            name
            for name in git.paths_between(repo, source.sha, new_sha)
            if name.startswith(SURFACE_DIRS)
        ]
    )


def touched(repo: str, source: RunState, base: str, new_sha: str) -> list[str]:
    """Returns the files changed since `source` that belong to the phase it walked.

    The phase's files are the paths its plan declares; without any, the files of the commits
    attributed to it in `base..source.sha` (by phase tag, branch name or finding id; a commit
    attributed to no phase counts for all); without a base, every frontend/ or src/ file counts.
    """
    changed = git.paths_between(repo, source.sha, new_sha)
    declared = declared_paths(explicit_plan(source.id))
    if declared and _anchored(repo, (source.sha, new_sha), declared):
        return [name for name in changed if any(_hit(glob, name) for glob in declared)]
    if base:
        cycle = cycles.load(source.cycle) if cycles.exists(source.cycle) else None
        if cycle and cycle.prd and cycle.phase:
            return _release_touched(repo, source, base, new_sha, f"{cycle.prd}:{cycle.phase}")
        own = set(git.paths_between(repo, base, source.sha))
        if own:
            return [name for name in changed if name in own]
    return [name for name in changed if name.startswith(SURFACE_DIRS)]


def carried_problems(
    source: RunState, base: str, cycle: str, new_sha: str, new_plan: str
) -> list[str]:
    """Returns why the QA of phase run `source` does not stand for `new_sha`; empty when it does.

    It stands when the same cycle, with the same plan the operator gave, passed on an earlier
    commit and the commits since touch nothing of the phase.
    """
    if source.cycle != cycle:
        return ["it belongs to another phase"]
    held = explicit_plan(source.id)
    if not held or held != new_plan:
        return ["the phase plan is not the one it walked"]
    try:
        hit = touched(source.repo, source, base, new_sha)
    except SystemExit:
        return ["git cannot tell what changed since"]
    if hit:
        names = ", ".join(hit[:3]) + (f" and {len(hit) - 3} more" if len(hit) > 3 else "")
        return [f"commits since touch the phase: {names}"]
    return []
