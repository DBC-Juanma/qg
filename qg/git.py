"""Git queries against a repository; every failure is a fatal, readable error."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from qg.util import die

HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

BASE_CANDIDATES = ("master", "origin/master", "main", "origin/main")
GIT_TIMEOUT = 120.0


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Runs git with `args` inside `repo` and returns the process; a hung git exits with a note."""
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", repo, *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"git {' '.join(args)} timed out after {GIT_TIMEOUT:g}s in {repo}")


def out(repo: str, *args: str) -> str:
    """Returns the stripped stdout of `git <args>` in `repo`; exits with git's message on error."""
    result = _git(repo, *args)
    if result.returncode != 0:
        die(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


MAX_ORIGIN_DEPTH = 3


def canonical_repo(path: str, depth: int = 0) -> str:
    """Returns the real path of the repository that owns `path`, one for all its worktrees.

    A clone whose `origin` is a local path (or file://) of a git repository has that
    repository's identity, followed up to `MAX_ORIGIN_DEPTH` clones deep; a remote origin
    (ssh, https) gives the clone its own.
    """
    own = _own_repo(path)
    origin = _git(own, "config", "--get", "remote.origin.url").stdout.strip()
    local = origin.removeprefix("file://") if origin.startswith(("/", "file://")) else ""
    if local and depth < MAX_ORIGIN_DEPTH and os.path.isdir(local):
        if _git(local, "rev-parse", "--git-dir").returncode == 0:
            return canonical_repo(local, depth + 1)
    return own


def _own_repo(path: str) -> str:
    """Returns the real path of the repository whose common git directory owns `path`."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"git rev-parse timed out after {GIT_TIMEOUT:g}s in {path}")
    if result.returncode != 0:
        die(f"{path} is not inside a git repository")
    return os.path.dirname(os.path.realpath(result.stdout.strip()))


def remove_worktree_entry(repo: str, src: str) -> None:
    """Removes the single admin entry of the worktree at `src` when its gitdir points there."""
    found = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    entries = Path(found.stdout.strip()) / "worktrees"
    if found.returncode != 0 or not entries.is_dir():
        return
    target = os.path.join(os.path.realpath(src), ".git")
    for entry in entries.iterdir():
        pointer = entry / "gitdir"
        if pointer.is_file() and pointer.read_text().strip() == target:
            shutil.rmtree(entry)


def resolve_commit(repo: str, ref: str) -> str:
    """Returns the full sha of the commit `ref` names."""
    return out(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def tree_of(repo: str, sha: str) -> str:
    """Returns the tree hash of commit `sha`."""
    return out(repo, "rev-parse", f"{sha}^{{tree}}")


def branch_of(repo: str, ref: str) -> str:
    """Returns the branch `ref` names or sits on, or 'detached' when it names no branch."""
    name = _git(repo, "rev-parse", "--symbolic-full-name", ref).stdout.strip()
    for prefix in ("refs/heads/", "refs/remotes/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return "detached"


def tip_of_branch(repo: str, branch: str) -> str | None:
    """Returns the sha at the tip of local branch `branch`, or None when it does not exist."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def show(repo: str, sha: str, path: str) -> str | None:
    """Returns the content of `path` at commit `sha`, or None when the commit has no such file."""
    result = _git(repo, "show", f"{sha}:{path}")
    return result.stdout if result.returncode == 0 else None


def blobs(repo: str, oids: list[str]) -> dict[str, bytes]:
    """Returns the bytes of each blob of `oids`, read by one cat-file process; exits when an
    answer is not the blob asked for."""
    if not oids:
        return {}
    if not all(OID.fullmatch(oid) for oid in oids):
        die(f"not object ids: {[oid for oid in oids if not OID.fullmatch(oid)][:3]}")
    asked = "".join(f"{oid}\n" for oid in oids).encode()
    try:
        result = subprocess.run(
            ["git", "-C", repo, "cat-file", "--batch"],
            input=asked,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"git cat-file --batch timed out after {GIT_TIMEOUT:g}s in {repo}")
    if result.returncode != 0:
        die(f"git cat-file --batch failed in {repo}: {result.stderr.decode(errors='replace')}")
    data, found, at = result.stdout, {}, 0
    for oid in oids:
        end = data.index(b"\n", at)
        header = data[at:end].split()
        if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
            die(f"git cat-file answered {data[at:end][:80]!r} for blob {oid} in {repo}")
        start = end + 1
        found[oid] = data[start : start + int(header[2])]
        at = start + int(header[2]) + 1
    if at != len(data):
        die(f"git cat-file answered more than the {len(oids)} blob(s) asked for in {repo}")
    return found


def tree_entries(repo: str, sha: str) -> list[tuple[str, str, str]]:
    """Returns (mode, blob id, path) of every blob of commit `sha`'s tree, read NUL-separated."""
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha)
    if raw.returncode != 0:
        die(f"git ls-tree {sha} failed in {repo}: {raw.stderr.strip()}")
    found = []
    for entry in raw.stdout.split("\0"):
        if entry:
            meta, _, path = entry.partition("\t")
            mode, kind, oid = meta.split()
            if kind == "blob":
                found.append((mode, oid, path))
    return found


def raw_changes(repo: str, base: str, head: str) -> list[tuple[str, str, str, str, str, str, str]]:
    """Returns (status, old mode, old id, new mode, new id, old path, new path) of each file
    `base..head` adds, modifies or renames, from `git diff --raw -z`; old path is empty but for
    a rename."""
    raw = _git(repo, "diff", "--raw", "-z", "--no-abbrev", "-M", "--diff-filter=AMR", base, head)
    if raw.returncode != 0:
        die(f"git diff --raw {base}..{head} failed in {repo}: {raw.stderr.strip()}")
    fields = raw.stdout.split("\0")
    found = []
    index = 0
    while index < len(fields) and fields[index]:
        old_mode, new_mode, old_oid, new_oid, status = fields[index].lstrip(":").split()
        if status.startswith("R"):
            old, new, index = fields[index + 1], fields[index + 2], index + 3
        else:
            old, new, index = "", fields[index + 1], index + 2
        found.append((status[0], old_mode, old_oid, new_mode, new_oid, old, new))
    return found


def ref_exists(repo: str, ref: str) -> bool:
    """Tells whether `ref` names a commit in `repo`."""
    check = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    return check.returncode == 0


def exists_in_tree(repo: str, sha: str, path: str) -> bool:
    """Tells whether commit `sha` contains the file `path`."""
    return _git(repo, "cat-file", "-e", f"{sha}:{path}").returncode == 0


def base_ref(repo: str) -> str:
    """Returns the first of master, origin/master, main, origin/main that exists in `repo`."""
    for candidate in BASE_CANDIDATES:
        check = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        if check.returncode == 0:
            return candidate
    die(f"none of {', '.join(BASE_CANDIDATES)} exists in {repo} to diff against")


def changed_files(repo: str, sha: str) -> list[str]:
    """Returns the files changed between the merge-base of `sha` with the base branch and `sha`."""
    base = out(repo, "merge-base", sha, base_ref(repo))
    names = out(repo, "diff", "--name-only", f"{base}..{sha}")
    return names.splitlines() if names else []


def log_between(repo: str, older: str, newer: str, limit: int) -> list[str] | None:
    """Returns up to `limit`+1 `git log --oneline older..newer` lines, None when git cannot tell."""
    result = _git(
        repo, "log", "--oneline", "--no-decorate", f"--max-count={limit + 1}", f"{older}..{newer}"
    )
    return result.stdout.splitlines() if result.returncode == 0 else None


def base_log(repo: str, sha: str, limit: int) -> tuple[str, list[str]] | None:
    """Returns the base branch name and the commits `sha` adds over it; None without a base."""
    for candidate in BASE_CANDIDATES:
        check = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        if check.returncode == 0:
            lines = log_between(repo, candidate, sha, limit)
            return (candidate, lines) if lines is not None else None
    return None


def commit_log(repo: str, base: str, sha: str) -> list[str]:
    """Returns `git log --no-merges --format='%h %s' base..sha`, newest first."""
    return out(repo, "log", "--no-merges", "--format=%h %s", f"{base}..{sha}").splitlines()


def commit_files(repo: str, base: str, sha: str) -> list[tuple[str, list[str]]]:
    """Returns `(<short sha> <subject>, files)` of each non-merge commit of `base..sha`."""
    raw = out(repo, "log", "--no-merges", "--name-only", "--format=%x00%h %s", f"{base}..{sha}")
    found = []
    for block in raw.split("\0")[1:]:
        head, _, names = block.partition("\n")
        found.append((head.strip(), [name for name in names.splitlines() if name]))
    return found


def ref_sha(repo: str, ref: str) -> str | None:
    """Returns the commit the fully qualified `ref` points at, None when it does not exist."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def parent_of(repo: str, sha: str) -> str | None:
    """Returns the first parent of commit `sha`, None for a root commit."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^")
    return result.stdout.strip() if result.returncode == 0 else None


def paths_between(repo: str, base: str, sha: str, only: str | None = None) -> list[str]:
    """Returns `git diff --name-only --no-renames base..sha`, a rename listing both paths.

    `only` is a --diff-filter such as AM.
    """
    flags = [f"--diff-filter={only}"] if only else []
    return out(repo, "diff", "--name-only", "--no-renames", *flags, f"{base}..{sha}").splitlines()


def is_ancestor(repo: str, older: str, newer: str) -> bool:
    """Tells whether commit `older` is `newer` or one of its ancestors."""
    return _git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def change_paths(repo: str, base: str, sha: str) -> list[str]:
    """Returns the paths a change touches, which decide the affected tests.

    The union of `base..sha` and the change against the base branch: a `--base` newer than it
    should be can add nothing the branch diff already says, so it can never shrink the selection.
    """
    against_branch = changed_files(repo, sha)
    if not base:
        return against_branch
    return sorted({*paths_between(repo, base, sha), *against_branch})


def diff_between(repo: str, base: str, sha: str) -> str:
    """Returns the unified `git diff base..sha`, unstripped."""
    result = _git(repo, "diff", f"{base}..{sha}")
    if result.returncode != 0:
        die(f"git diff {base}..{sha} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def stat_between(repo: str, base: str, sha: str) -> str:
    """Returns `git diff --stat base..sha`."""
    return out(repo, "diff", "--stat", f"{base}..{sha}")


SIMPLIFIED_PREFIX = "refs/heads/qg/simplified/"
NO_OBJECT = "0" * 40


def simplified_ref(run_id: str) -> str:
    """Returns the one ref qg may create in the canonical repository for run `run_id`."""
    return f"{SIMPLIFIED_PREFIX}{run_id}"


def publish_simplified(repo: str, run_id: str, sha: str) -> str:
    """Creates refs/heads/qg/simplified/<run id> at `sha`; refuses to move a ref that exists.

    This is the only ref qg ever writes in a canonical repository. The commit is already in the
    shared object database because the sandbox is a worktree of the same repository.
    """
    ref = simplified_ref(run_id)
    out(repo, "update-ref", "--no-deref", ref, sha, NO_OBJECT)
    return ref


def added_lines(diff: str) -> set[int]:
    """Returns the line numbers a unified diff with no context adds or changes on its new side."""
    lines: set[int] = set()
    for start, count in HUNK.findall(diff):
        lines.update(range(int(start), int(start) + int(count or 1)))
    return lines
