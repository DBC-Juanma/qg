"""`qg init`: onboard a repository, or reform one.

It reads the git tree at the protected ref (never the working directory, so a bare clone works),
detects the Python project, the yarn packages, the test layout and the profile's layers, and
proposes verify.yml: a whole minimal file when the ref has none, else only the optional sections
it lacks, appended so its text survives byte for byte. It counts the proposed rules over the whole
tree, writes `.claude/verify.yml` into the working tree only with --write and commits nothing.
"""

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from qg import config, git, inplace, paths, proc, rules
from qg.config import VERIFY_PATH, Config, Rules
from qg.util import atomic_write, die

UV_LOCK = "uv.lock"
PYPROJECT = "pyproject.toml"
PACKAGE = "package.json"
YARN_LOCK = "yarn.lock"
UNIT_DIRS = ("unit",)
POSTGRES_IMAGE = "postgres:16-alpine"
NODE_INSTALL = "yarn --cwd {pkg} install --frozen-lockfile"
RUFF = ("uv run ruff check .", "uv run ruff format --check .")
DB_PLACEHOLDERS = ("{db}", "{pg_port}", "{pg_url_async}", "{pg_url_sync}")
NO_NEWLINE = "\\ No newline at end of file"


@dataclass(frozen=True)
class Detected:
    """What the tree at the protected ref holds: the Python project, the yarn packages (and the
    node packages qg cannot install), the layers found as path globs, those not found, the test
    directories and the unit-test directories among them."""

    uv_lock: bool
    ruff: bool
    node: tuple[str, ...]
    node_skipped: tuple[str, ...]
    layers: dict[str, tuple[str, ...]]
    missing: tuple[str, ...]
    tests: tuple[str, ...]
    units: tuple[str, ...]


@dataclass(frozen=True)
class Proposal:
    """The proposed verify.yml: its whole text, what is added to the protected one, the values the
    operator must review and its parsed configuration."""

    text: str
    added: str
    config: Config
    review: list[str] = field(default_factory=list)


def _parents(name: str) -> list[str]:
    """Returns every directory above repository path `name`, outermost first."""
    parts = PurePosixPath(name).parts[:-1]
    return ["/".join(parts[: depth + 1]) for depth in range(len(parts))]


def _outermost(found: list[str]) -> tuple[str, ...]:
    """Returns the directories of `found` that lie inside no other one of them, sorted."""
    chosen = set(found)
    return tuple(sorted(d for d in chosen if not any(p in chosen for p in _parents(d))))


def detect(repo: str, entries: list[tuple[str, str, str]], profile: rules.Profile) -> Detected:
    """Returns what the tree `entries` (mode, blob id, path) hold, reading blobs from git only."""
    files = {path for _, _, path in entries}
    sources = [rules.safe_path(path) for path in files if path.endswith(rules.SOURCE_SUFFIX)]
    python_dirs = {parent for name in sources for parent in _parents(name)}

    def named(candidates: tuple[str, ...]) -> list[str]:
        """Returns the directories holding Python files whose own name is one of `candidates`."""
        return [d for d in python_dirs if PurePosixPath(d).name in candidates]

    test_dirs = _outermost(named(profile.detect.get(rules.TESTS, ())))

    def outside_tests(directory: str) -> bool:
        """Tells whether `directory` lies outside every test directory."""
        return not any(directory == t or directory.startswith(f"{t}/") for t in test_dirs)

    layers: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for layer, candidates in profile.detect.items():
        if layer == rules.TESTS:
            continue
        found = _outermost([d for d in named(candidates) if outside_tests(d)])
        if found:
            layers[layer] = tuple(f"{d}/**" for d in found)
        else:
            missing.append(layer)
    units = _outermost([d for d in named(UNIT_DIRS) if not outside_tests(d)])
    packages = sorted(
        rules.safe_path(path).removesuffix(PACKAGE).rstrip("/") or "."
        for path in files
        if PurePosixPath(path).name == PACKAGE and "node_modules" not in PurePosixPath(path).parts
    )
    locked = [p for p in packages if (YARN_LOCK if p == "." else f"{p}/{YARN_LOCK}") in files]
    pyproject = [oid for _, oid, path in entries if path == PYPROJECT]
    ruff = bool(pyproject) and b"ruff" in git.blobs(repo, pyproject)[pyproject[0]]
    return Detected(
        uv_lock=UV_LOCK in files,
        ruff=ruff,
        node=tuple(locked),
        node_skipped=tuple(p for p in packages if p not in locked),
        layers=layers,
        missing=tuple(missing),
        tests=tuple(f"{d}/**" for d in test_dirs),
        units=tuple(f"{d}/**" for d in units),
    )


def _dump(data: dict[str, Any]) -> str:
    """Returns `data` as block-style YAML in the given key order."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=4096)


def _rules_section(found: Detected, profile: str) -> dict[str, Any]:
    """Returns the proposed `rules:` mapping: advisory, the detected layers and tests."""
    section: dict[str, Any] = {
        "profile": profile,
        "enforce": False,
        "layers": {name: list(globs) for name, globs in found.layers.items()},
    }
    if found.tests:
        section["tests"] = list(found.tests)
    return section


def _lite_section(command: str, found: Detected, review: list[str]) -> dict[str, Any]:
    """Returns the proposed `lite:` mapping on `affected.backend.command`; notes what to review."""
    section: dict[str, Any] = {"command": command}
    if found.units:
        section["tests"] = list(found.units)
    else:
        review.append(
            "lite.tests left out: no unit-test directory found, so lite runs every affected test; "
            "list the globs of the tests that need no database"
        )
    if any(name in command for name in DB_PLACEHOLDERS):
        review.append("lite.command uses a database placeholder, and lite has no Postgres")
    return section


def _whole(found: Detected, name: str, profile: str, review: list[str]) -> dict[str, Any]:
    """Returns a complete minimal verify.yml for a repository that has none."""
    if not found.uv_lock:
        die(f"no {UV_LOCK} at the root: qg syncs the Python project with uv (commit {UV_LOCK})")
    deps: dict[str, Any] = {"python_sync": "uv sync --frozen"}
    if found.node:
        deps |= {"node_packages": list(found.node), "node_install": NODE_INSTALL}
        review.append(
            f"node packages {', '.join(found.node)}: add stages.frontend and affected.frontend"
        )
    db = re.sub(r"[^a-z0-9_]", "_", name.lower().removesuffix(".git")) + "_verify"
    review.append(f"postgres: {POSTGRES_IMAGE} and database {db}")
    static = list(RUFF) if found.ruff else []
    if not found.ruff:
        review.append("stages.static is empty: ruff is not in pyproject.toml; add the linters")
    backend = [f"uv run pytest -q {' '.join(t.removesuffix('/**') for t in found.tests)}"]
    if not found.tests:
        backend = []
        review.append("stages.backend is empty: no test directory found")
    else:
        review.append(f"stages.backend `{backend[0]}`: pass the run's database ({{pg_url_async}})")
    review.append(
        "deploy: prod and staging gate static and backend only; add playwright and qa to prod "
        "once configured, and affected: to run only the tests a change needs (then qg init "
        "proposes lite:)"
    )
    data: dict[str, Any] = {
        "version": 1,
        "deps": deps,
        "postgres": {"image": POSTGRES_IMAGE, "db": db},
        "stages": {"static": static, "backend": backend},
        "deploy": {
            "prod": {"stages": ["static", "backend"]},
            "staging": {"stages": ["static", "backend"]},
        },
    }
    if found.layers:
        data["rules"] = _rules_section(found, profile)
    return data


def _validated(text: str, source: str) -> Config:
    """Returns the configuration `text` parses to, its rules resolved against their profile."""
    parsed = config.parse(config.load_yaml(text, source), source)
    if parsed.rules is not None:
        rules.load_profile(parsed.rules)
    return parsed


def propose(existing: str | None, found: Detected, name: str, profile: str) -> Proposal:
    """Returns the proposal for the protected verify.yml text `existing` (None: there is none)."""
    review: list[str] = []
    source = f"proposed {VERIFY_PATH}"
    if existing is None:
        text = _dump(_whole(found, name, profile, review))
        return Proposal(text, text, _validated(text, source), review)
    current = _validated(existing, f"{VERIFY_PATH} at the protected ref")
    sections: dict[str, Any] = {}
    if current.rules is None and found.layers:
        sections["rules"] = _rules_section(found, profile)
    elif current.rules is None:
        review.append(f"rules: not proposed: no layer of {profile} found")
    backend = current.affected.backend
    if current.lite is None and backend is not None:
        sections["lite"] = _lite_section(backend.command, found, review)
    elif current.lite is None:
        review.append("lite: not proposed: it needs affected.backend to select the tests")
    added = _dump(sections) if sections else ""
    joint = "" if not existing or existing.endswith("\n") else "\n"
    text = existing + joint + added if added else existing
    parsed = _validated(text, source)
    if backend is not None and "lite" in sections:
        if parsed.lite is None or parsed.lite.command != backend.command:
            die("the proposed lite.command does not read back as affected.backend.command")
    return Proposal(text, joint + added if added else "", parsed, review)


def _runner_note() -> str:
    """Returns whether the runner daemon started before this qg's configuration schema changed:
    a daemon that did rejects the new keys in memory and stalls the queue."""
    shown = proc.run(
        [
            "systemctl",
            "--user",
            "show",
            paths.SERVICE_UNIT,
            "-p",
            "ActiveState",
            "-p",
            "ExecMainStartTimestamp",
            "--timestamp=unix",
        ]
    )
    values = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    state, started = values.get("ActiveState", "unknown"), values.get("ExecMainStartTimestamp", "")
    if state != "active" or not started.removeprefix("@").isdigit():
        return f"runner    {paths.SERVICE_UNIT} is {state}: start it once verify.yml is committed"
    began = int(started.removeprefix("@"))
    schema = Path(config.__file__)
    changed = schema.stat().st_mtime
    if changed <= began:
        return f"runner    {paths.SERVICE_UNIT} started after {schema} changed: it knows the keys"
    return (
        f"WARNING   {paths.SERVICE_UNIT} started {datetime.fromtimestamp(began):%Y-%m-%d %H:%M}, "
        f"before {schema} changed ({datetime.fromtimestamp(changed):%Y-%m-%d %H:%M}): it may "
        "reject keys it does not know and stall the queue; restart it when no run is in flight "
        "and before the commit lands"
    )


def _write(repo: str, ref: str, existing: bytes | None, proposal: Proposal) -> Path:
    """Writes the proposal into the working tree of `repo` when the file there is the ref's."""
    top = inplace.toplevel(repo)
    if top is None:
        die(f"{repo} has no working tree: --write needs a checkout")
    target = Path(top) / VERIFY_PATH
    if not target.parent.resolve().is_relative_to(Path(top)) or target.is_symlink():
        die(f"refusing to write {target}: it leaves the working tree")
    if target.exists() and target.read_bytes() != existing:
        die(f"{target} differs from {ref}: commit, stash or remove it first")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, proposal.text)
    return target


def run(repo: str, profile_name: str, write: bool, count: bool) -> int:
    """Detects, proposes, optionally writes and counts; prints every step; returns 0."""
    if not config.PROFILE.match(profile_name):
        die(f"--profile {profile_name!r} is not a profile name")
    profile = rules.load_profile(Rules(profile=profile_name, layers={}))
    ref = config.protected_ref(repo)
    branch = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
    sha = git.resolve_commit(repo, ref)
    key = git.canonical_repo(repo)
    print(f"qg init {repo} at {branch} ({sha[:12]}), profile {profile_name}")
    entries = git.tree_entries(repo, sha)
    found = detect(repo, entries, profile)
    print(f"python    {UV_LOCK if found.uv_lock else f'no {UV_LOCK}'}, ruff {found.ruff}")
    for package in found.node:
        print(f"node      {package} (yarn)")
    for package in found.node_skipped:
        print(f"skipped   node package {package}: no {YARN_LOCK}, qg installs yarn packages only")
    for layer, globs in found.layers.items():
        print(f"layer     {layer:<13} {', '.join(globs)}")
    for layer in found.missing:
        names = ", ".join(profile.detect[layer])
        print(f"missing   {layer:<13} no directory named {names} holds Python files")
    print(f"tests     {', '.join(found.tests) or 'none found'}")
    print(f"unit      {', '.join(found.units) or 'none found'}")
    blob = [oid for _, oid, path in entries if path == VERIFY_PATH]
    existing = git.blobs(repo, blob)[blob[0]] if blob else None
    try:
        text = None if existing is None else existing.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        die(f"{VERIFY_PATH} at {branch} is not UTF-8: {exc}")
    proposal = propose(text, found, Path(key).name, profile_name)
    for item in proposal.review:
        print(f"review    {item}")
    cfg = proposal.config
    if text is not None and not proposal.added:
        done = cfg.rules is not None and (cfg.lite is not None or cfg.affected.backend is None)
        print(f"proposed  nothing{': already configured' if done else ''}")
    else:
        what = "a whole minimal file" if text is None else "appended to the existing text"
        print(f"proposed  {VERIFY_PATH}, {what}:")
        before = [] if text is None else text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            before,
            proposal.text.splitlines(keepends=True),
            "/dev/null" if text is None else f"a/{VERIFY_PATH}",
            f"b/{VERIFY_PATH}",
        )
        for line in diff:
            print(line if line.endswith("\n") else f"{line}\n{NO_NEWLINE}\n", end="")
        if write:
            print(f"written   {_write(repo, branch, existing, proposal)}")
        else:
            print("dry run   nothing written: --write writes it into the working tree")
    print(_runner_note())
    if cfg.rules is None:
        print("baseline  skipped: no rules")
    elif not count:
        print("baseline  skipped: --no-baseline")
    else:
        for line in rules.burn_down(repo, key, cfg.rules, sha):
            print(line)
    print("next steps:")
    print(f"  1. commit {VERIFY_PATH} to {branch}: qg reads it only from there, qg never commits")
    print(
        f"  2. before that commit lands, {paths.SERVICE_UNIT} must run a qg that knows these keys"
    )
    print("  3. `qg lite` checks a commit in the foreground: static, rules, Postgres-free tests")
    print(
        "  4. `qg rules baseline` tracks the burn-down; set rules.enforce: true on "
        f"{branch} to make a new violation fail the static stage"
    )
    return 0
