"""Affected tests: which test files a change needs, from the protected configuration's mapping.

The mapping (path globs to test paths) lives in verify.yml on the protected ref, never in the
branch, so a branch cannot choose which tests judge it. Everything here is a pure function of the
changed paths and the configuration.
"""

import fnmatch
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from qg.config import Affected, FrontendPackage

TEST_FILE_GLOBS = ("test_*.py", "*_test.py")
NONE_SELECTED = "no backend tests were selected: the stage cannot pass without any"


@dataclass(frozen=True)
class Selection:
    """The tests one stage runs: labels for the report and fingerprint, and the commands."""

    labels: list[str]
    commands: list[str]
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _match(name: str, globs: tuple[str, ...]) -> bool:
    """Tells whether `name` matches one of the fnmatch `globs`."""
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def _is_test_file(name: str) -> bool:
    """Tells whether a changed path is a Python test module under tests/."""
    path = PurePosixPath(name)
    return path.parts[:1] == ("tests",) and _match(path.name, TEST_FILE_GLOBS)


def _quoted(names: list[str]) -> str:
    """Returns the names shell-quoted and space-separated."""
    return " ".join(shlex.quote(name) for name in names)


def backend(
    aff: Affected, changed: list[str], exists: Callable[[str], bool], everything: bool = False
) -> Selection | None:
    """Returns the pytest selection for `changed`, None when affected tests are not configured.

    The selection is the always list (lint and the core), the tests of every map entry a changed
    path matches, and every changed test module. A changed path that matches no entry and is no
    test is reported in the notes: only the always list covers it. A change to a path in
    `whole_when` (fixtures, root conftest, lockfile) selects everything in `all`. The stage fails
    closed: a path of the always list (or of `all`) that the commit lacks, or an empty selection,
    is a problem, so a branch cannot pass by deleting the tests that judge it.
    """
    spec = aff.backend
    if spec is None:
        return None
    whole = everything or any(_match(name, spec.whole_when) for name in changed)
    wanted = list(spec.all_tests if whole else spec.always)
    notes = []
    if not whole:
        unmapped = []
        for name in changed:
            hits = [entry for entry in spec.map if _match(name, entry.paths)]
            for entry in hits:
                wanted.extend(entry.tests)
            if _is_test_file(name):
                wanted.append(name)
            elif not hits and not name.startswith(("docs/", "tasks/", ".claude/")):
                unmapped.append(name)
        if unmapped:
            notes.append(f"{len(unmapped)} changed path(s) map to no tests; only core covers them")
    else:
        notes.append("all backend tests: nightly or a whole-suite path changed")
    tests = [name for name in dict.fromkeys(wanted) if exists(name)]
    required = spec.all_tests if whole else spec.always
    problems = [
        f"required test path {name} is missing from the commit"
        for name in required
        if not exists(name)
    ]
    if not tests:
        problems.append(NONE_SELECTED)
    commands = [spec.command.replace("{tests}", _quoted(tests))] if tests else []
    return Selection(tests, commands, notes, problems)


def frontend(
    aff: Affected,
    changed: list[str],
    exists: Callable[[str], bool],
    everything: bool = False,
) -> Selection | None:
    """Returns the vitest selection per changed package, None when none is configured.

    A package with a changed file runs `related` (vitest's module graph: the tests that import a
    changed file) on its changed source files; a changed config, lockfile or test setup path in
    `whole_when`, or a changed path of code the package shares (`shared`), runs the package's
    whole suite. So does a changed path the commit no longer has (deleted or renamed away):
    vitest's module graph is read from disk and cannot find the tests that import a file that is
    gone, so `related` would pass them unrun. File names are given as `./name`, so a name that
    starts with `-` is never read as a vitest option. A package with no change runs nothing.
    """
    if not aff.frontend:
        return None
    labels: list[str] = []
    commands: list[str] = []
    for package in aff.frontend:
        mine = _own(package, changed)
        shared = [name for name in changed if _match(name, package.shared)]
        if not mine and not shared and not everything:
            continue
        gone = [name for name in mine if not exists(f"{package.package.rstrip('/')}/{name}")]
        whole = (
            everything
            or bool(shared)
            or bool(gone)
            or any(_match(name, package.whole_when) for name in mine)
        )
        if whole:
            labels.append(f"{package.package}: whole suite")
            commands.append(package.all_command)
        else:
            labels.append(f"{package.package}: related to {', '.join(mine)}")
            commands.append(package.related.replace("{files}", _quoted([f"./{n}" for n in mine])))
    return Selection(labels, commands)


def _own(package: FrontendPackage, changed: list[str]) -> list[str]:
    """Returns the changed paths inside `package`, relative to its directory, sorted."""
    prefix = package.package.rstrip("/") + "/"
    return sorted(name[len(prefix) :] for name in changed if name.startswith(prefix))
