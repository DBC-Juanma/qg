"""In-place runs: a run works directly on the clean worktree it was asked to verify."""

import fcntl
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO

from qg import paths, proc
from qg.util import die

CHANGED = "the worktree changed during the verification"
LOCK_NAME = "qg-inplace.lock"
LOCK_POLL_SECONDS = 1.0
_HELD: dict[str, IO[str]] = {}


class TreeChanged(Exception):
    """The worktree's HEAD or files moved while the run was working on it."""


@dataclass
class Record:
    """What an in-place run did to a worktree, kept in the run's directory for its teardown."""

    worktree: str
    sha: str
    linked: list[str] = field(default_factory=list)
    moved: dict[str, str] = field(default_factory=dict)


def _git(path: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Runs `git <args>` in `path` with hooks off."""
    return proc.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args])


def toplevel(path: str) -> str | None:
    """Returns the real top directory of the worktree or checkout containing `path`, or None."""
    done = _git(path, "rev-parse", "--show-toplevel")
    return os.path.realpath(done.stdout.strip()) if done.returncode == 0 else None


def head(worktree: str) -> str:
    """Returns the commit the worktree's HEAD sits on."""
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def dirty(worktree: str) -> list[str]:
    """Returns the tracked changes and the untracked files outside gitignored paths."""
    done = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    return [line for line in done.stdout.splitlines() if line]


def refusal(worktree: str, sha: str) -> str | None:
    """Returns why `qg verify` must refuse `worktree`, or None when it may be queued.

    A worktree at the requested commit with uncommitted changes is refused. A worktree whose
    HEAD is another commit is verified in a detached worktree, like a `--ref`.
    """
    if head(worktree) != sha:
        return None
    changes = dirty(worktree)
    if not changes:
        return None
    shown = "; ".join(changes[:5]) + ("; ..." if len(changes) > 5 else "")
    return (
        f"{worktree} has uncommitted changes ({shown}): commit or stash first; "
        "qg verifies a commit and works in place on a clean worktree"
    )


def eligible(worktree: str, sha: str) -> bool:
    """Tells whether a run may work in place on `worktree`: HEAD is `sha` and the tree clean."""
    return bool(worktree) and head(worktree) == sha and not dirty(worktree)


def lock_path(worktree: str) -> Path:
    """Returns the lockfile of `worktree`, in its own git directory."""
    git_dir = _git(worktree, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    return Path(git_dir) / LOCK_NAME


def acquire(run_id: str, worktree: str) -> None:
    """Takes the worktree's lock for the run, waiting while another run holds it."""
    handle = lock_path(worktree).open("a")
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(LOCK_POLL_SECONDS)
    _HELD[run_id] = handle


def release(run_id: str) -> None:
    """Gives the worktree's lock back; a no-op for a run that holds none."""
    handle = _HELD.pop(run_id, None)
    if handle is not None:
        handle.close()


def record_file(run_id: str) -> Path:
    """Returns the path of the run's in-place record."""
    return paths.run_dir(run_id) / "inplace.json"


def save(run_id: str, record: Record) -> None:
    """Writes the run's in-place record."""
    record_file(run_id).write_text(json.dumps(asdict(record), indent=2) + "\n")


def load(run_id: str) -> Record | None:
    """Returns the run's in-place record, or None for a run on its own detached worktree."""
    file = record_file(run_id)
    return Record(**json.loads(file.read_text())) if file.exists() else None


def link_node_modules(record: Record, run_id: str, package_dir: Path, cached: Path) -> None:
    """Hard-links the cached node_modules into the worktree, moving the user's own aside.

    The path must be gitignored, or the tree would read as changed.
    """
    target = package_dir / "node_modules"
    relative = os.path.relpath(target, record.worktree)
    if _git(record.worktree, "check-ignore", "-q", f"{relative}/").returncode != 0:
        die(f"{relative} is not gitignored: qg cannot work in place on {record.worktree}")
    if target.exists() or target.is_symlink():
        aside = paths.run_dir(run_id) / "aside" / relative
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(target, aside)
        record.moved[str(target)] = str(aside)
        save(run_id, record)
    done = proc.run(["cp", "-al", str(cached), str(target)], timeout=600)
    if done.returncode != 0:
        die(f"linking node_modules of {package_dir}: {done.stderr.strip()}")
    record.linked.append(str(target))
    save(run_id, record)


def verify_unchanged(run_id: str) -> None:
    """Raises TreeChanged when the worktree's HEAD moved or its files changed during the run."""
    record = load(run_id)
    if record is not None and (head(record.worktree) != record.sha or dirty(record.worktree)):
        raise TreeChanged(CHANGED)


def cleanup(run_id: str) -> bool:
    """Removes what the run added to the worktree and restores what it moved; True when done.

    The worktree itself is never removed.
    """
    record = load(run_id)
    if record is None:
        return True
    for link in record.linked:
        shutil.rmtree(link, ignore_errors=True)
    left = [link for link in record.linked if Path(link).exists()]
    for original, aside in record.moved.items():
        if Path(aside).exists() and not Path(original).exists():
            shutil.move(aside, original)
    if not left:
        record.linked, record.moved = [], {}
        save(run_id, record)
    release(run_id)
    return not left
