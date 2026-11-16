"""Architecture rules of the static stage: a bundled profile turned into semgrep rules over the
repository's layers, plus comment and markdown hygiene, judged by a ratchet against the base.

The ratchet stores no baseline: the files a change touches are read from git at the base and at
the head, scanned alike, and the check fails only where the head holds more violations of one rule
in one file than the base did. Comments and markdown count only what the change adds."""

import fnmatch
import io
import json
import re
import shutil
import tempfile
import tokenize
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from qg import git, paths, proc
from qg.audit import RULE
from qg.config import Reader, Rules, load_yaml
from qg.queue import CommandRun
from qg.sandbox import Sandbox, base_env
from qg.stages import MAX_FAILURES, StageResult
from qg.util import die

PROFILES = Path(__file__).parent / "profiles"
SOURCE_SUFFIX = ".py"
MARKDOWN_SUFFIX = ".md"
TESTS = "tests"
SEMGREP_KEYS = ("pattern", "pattern-either", "patterns", "pattern-regex")
PRAGMA = re.compile(r"#\s*(noqa\b|type:|pragma\b|fmt:|pyright:|mypy:|ruff:|pylint:)")
ENCODING = re.compile(r"^#.*coding[:=]")
GLOB_TAIL = re.compile(r"^[*?\[\]]*(\.py)?$")
STAGE_COMMAND = "qg rules check"
REGULAR = ("100644", "100755")
UNSAFE = ("\\", "\0", "\n", "\r", "\ufffd")

Wrap = Callable[[list[str]], list[str]]


@dataclass(frozen=True)
class Rule:
    """One rule: its id, the fix it names, where it applies (None: every file but `outside`)
    and, for semgrep, its pattern."""

    id: str
    message: str
    scope: tuple[str, ...] | None
    outside: tuple[str, ...] = ()
    body: dict[str, Any] | None = None

    def applies(self, path: str) -> bool:
        """Tells whether the rule covers repository path `path`."""
        inside = self.scope is None or _matches(path, self.scope)
        return inside and not _matches(path, self.outside)


@dataclass(frozen=True)
class Profile:
    """A profile resolved against one repository: semgrep rules and the two hygiene rules."""

    semgrep: tuple[Rule, ...]
    comments: Rule | None
    markdown: Rule | None


@dataclass(frozen=True)
class Hit:
    """One violation: rule, repository path, line and the fix it names."""

    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        """Returns `path:line rule-id message`."""
        return f"{self.path}:{self.line} {self.rule} {self.message}"


@dataclass(frozen=True)
class Verdict:
    """The ratchet's outcome: the head-side violations of each regression and a summary line."""

    regressions: list[Hit]
    summary: str

    @property
    def passed(self) -> bool:
        """Tells whether the change adds no violation."""
        return not self.regressions


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    """Tells whether `path` matches one of `globs` (fnmatch: `*` also crosses `/`)."""
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


def profile_text(name: str) -> str:
    """Returns the YAML of bundled profile `name`; exits when there is none."""
    path = PROFILES / f"{name}.yml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in PROFILES.glob("*.yml")))
        die(f"rules.profile: no profile {name!r}; bundled: {known}")
    return path.read_text()


def module_segment(glob: str) -> str:
    """Returns the regex of the module name a layer glob stands for: its last concrete segment."""
    parts = [part for part in glob.strip("/").split("/") if part]
    while parts and GLOB_TAIL.match(parts[-1]):
        parts.pop()
    if not parts:
        die(f"rules.layers: {glob!r} names no directory or module to import")
    name = parts[-1].removesuffix(SOURCE_SUFFIX)
    return "".join(
        "[A-Za-z0-9_]*" if char == "*" else "[A-Za-z0-9_]" if char == "?" else re.escape(char)
        for char in name
    )


def import_body(globs: tuple[str, ...]) -> dict[str, Any]:
    """Returns the semgrep patterns of an import of any module the layer `globs` stand for."""
    names = "|".join(dict.fromkeys(module_segment(glob) for glob in globs))
    dotted = rf"^(.*\.)?({names})(\..*)?$"
    return {
        "pattern-either": [
            {"patterns": [{"pattern": "import $M"}, _regex("$M", dotted)]},
            {"patterns": [{"pattern": "from $M import $X"}, _regex("$M", dotted)]},
            {"patterns": [{"pattern": "from $M import $X"}, _regex("$X", rf"^({names})$")]},
        ]
    }


def _regex(variable: str, regex: str) -> dict[str, Any]:
    """Returns a semgrep metavariable-regex clause."""
    return {"metavariable-regex": {"metavariable": variable, "regex": regex}}


def _rule_id(reader: Reader, value: Any, where: str) -> str:
    """Checks a rule id: lowercase letters, digits and dashes."""
    found = reader.text(value, where)
    if not RULE.match(found):
        reader.fail(where, f"{found!r} must match {RULE.pattern}")
    return found


def load_profile(rules: Rules) -> Profile:
    """Returns the profile of `rules` resolved against its layers; exits on any mistake."""
    source = f"qg/profiles/{rules.profile}.yml"
    reader = Reader(source)
    raw = reader.mapping(
        load_yaml(profile_text(rules.profile), source),
        "<root>",
        required=("layers",),
        optional=("description", "forbidden_imports", "banned", "hygiene"),
    )
    names = reader.texts(raw["layers"], "layers")
    unknown = [name for name in rules.layers if name not in names]
    if unknown:
        Reader("rules").fail(
            "layers", f"unknown layer(s) {', '.join(unknown)}; {source} has {names}"
        )
    globs = {**{name: rules.layers.get(name, ()) for name in names}, TESTS: rules.tests}

    def scope(value: Any, where: str) -> tuple[str, ...]:
        """Returns the globs of the layers a profile entry lists."""
        listed = reader.texts(value, where)
        for name in listed:
            if name not in globs:
                reader.fail(where, f"unknown layer {name!r}")
        return tuple(glob for name in listed for glob in globs[name])

    found: list[Rule] = []
    for index, item in enumerate(raw.get("forbidden_imports", [])):
        where = f"forbidden_imports[{index}]"
        entry = reader.mapping(item, where, required=("id", "from", "to", "message"))
        target = scope([entry["to"]], f"{where}.to")
        found.append(
            Rule(
                _rule_id(reader, entry["id"], f"{where}.id"),
                reader.text(entry["message"], f"{where}.message"),
                scope(entry["from"], f"{where}.from"),
                body=import_body(target) if target else None,
            )
        )
    for index, item in enumerate(raw.get("banned", [])):
        where = f"banned[{index}]"
        entry = reader.mapping(
            item, where, required=("id", "message"), optional=("layers", "outside", *SEMGREP_KEYS)
        )
        keys = [key for key in SEMGREP_KEYS if key in entry]
        if len(keys) != 1:
            reader.fail(where, f"needs exactly one of {', '.join(SEMGREP_KEYS)}")
        found.append(
            Rule(
                _rule_id(reader, entry["id"], f"{where}.id"),
                reader.text(entry["message"], f"{where}.message"),
                scope(entry["layers"], f"{where}.layers") if "layers" in entry else None,
                scope(entry.get("outside", []), f"{where}.outside"),
                {keys[0]: entry[keys[0]]},
            )
        )
    hygiene = reader.mapping(raw.get("hygiene", {}), "hygiene", optional=("comments", "markdown"))
    comments = markdown = None
    if "comments" in hygiene:
        entry = reader.mapping(hygiene["comments"], "hygiene.comments", required=("id", "message"))
        comments = Rule(
            _rule_id(reader, entry["id"], "hygiene.comments.id"),
            reader.text(entry["message"], "hygiene.comments.message"),
            None,
        )
    if "markdown" in hygiene:
        entry = reader.mapping(
            hygiene["markdown"], "hygiene.markdown", required=("id", "message"), optional=("allow",)
        )
        allowed = reader.texts(entry.get("allow", []), "hygiene.markdown.allow")
        markdown = Rule(
            _rule_id(reader, entry["id"], "hygiene.markdown.id"),
            reader.text(entry["message"], "hygiene.markdown.message"),
            None,
            (*allowed, *rules.markdown_allow),
        )
    ids = [rule.id for rule in (*found, comments, markdown) if rule is not None]
    if len(set(ids)) != len(ids):
        reader.fail("<root>", "a rule id is used twice")
    stray = [name for name in rules.disable if name not in ids]
    if stray:
        Reader("rules").fail("disable", f"unknown rule id(s) {', '.join(stray)}")
    off = set(rules.disable)
    live = [rule for rule in found if rule.body is not None and rule.scope != ()]
    return Profile(
        tuple(rule for rule in live if rule.id not in off),
        None if comments is None or comments.id in off else comments,
        None if markdown is None or markdown.id in off else markdown,
    )


def semgrep_config(profile: Profile) -> str:
    """Returns the semgrep rules file of a resolved profile; the scopes stay with qg."""
    return yaml.safe_dump(
        {
            "rules": [
                {
                    "id": rule.id,
                    "languages": ["python"],
                    "severity": "ERROR",
                    "message": rule.message,
                    **(rule.body or {}),
                }
                for rule in profile.semgrep
            ]
        },
        sort_keys=False,
    )


def scan(
    profile: Profile, root: Path, targets: dict[str, str], wrap: Wrap, seconds: float
) -> dict[str, list[Hit]]:
    """Runs the profile's semgrep rules on `targets` (file under `root` -> repository path).

    Returns each target's hits on the rules that cover its repository path; a semgrep error,
    a skipped file or an unreadable result exits.
    """
    found: dict[str, list[Hit]] = {target: [] for target in targets}
    if not profile.semgrep or not targets:
        return found
    if shutil.which("semgrep", path=base_env().get("PATH")) is None:
        die("semgrep is not on PATH: `uv tool install semgrep==1.183.0`")
    config, output = root / "rules.semgrep.yml", root / "semgrep.json"
    config.write_text(semgrep_config(profile))
    argv = [
        "semgrep",
        "scan",
        "--config",
        str(config),
        "--json",
        "--output",
        str(output),
        "--metrics=off",
        "--disable-version-check",
        "--quiet",
        "--max-target-bytes=0",
        *targets,
    ]
    result = proc.run(wrap(argv), cwd=root, env=base_env(), timeout=seconds)
    if result.returncode != 0:
        die(f"semgrep failed ({result.returncode}): {result.stderr.strip()[:300]}")
    report = json.loads(output.read_text())
    if report["errors"]:
        die(f"semgrep reported errors: {json.dumps(report['errors'])[:500]}")
    skipped = sorted(set(targets) - set(report["paths"]["scanned"]))
    if skipped:
        die(f"semgrep skipped {len(skipped)} file(s): {', '.join(skipped[:5])}")
    rules = {rule.id: rule for rule in profile.semgrep}
    for item in report["results"]:
        target, rule = item["path"], rules[item["check_id"].rsplit(".", 1)[-1]]
        name = targets[target]
        if rule.applies(name):
            found[target].append(Hit(rule.id, name, item["start"]["line"], rule.message))
    return found


def comment_lines(text: str, name: str) -> list[int]:
    """Returns the lines of `text` holding a comment that is not a pragma, shebang or encoding."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        die(f"{name} cannot be tokenized: {exc}")
    lines = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, body = token.start[0], token.string
        header = row == 1 and body.startswith("#!") or row <= 2 and ENCODING.match(body)
        if not header and not PRAGMA.match(body):
            lines.append(row)
    return lines


def safe_path(name: str) -> str:
    """Returns repository path `name`; exits when it could leave a directory it is joined to."""
    parts = name.split("/")
    odd = any(char in name for char in UNSAFE) or name.startswith("/")
    if odd or any(part in ("", ".", "..") for part in parts):
        die(f"refusing the repository path {name!r}: absolute, `..`, or an unsafe character")
    return name


def materialise(repo: str, files: dict[str, str], root: Path) -> None:
    """Writes each blob of `files` (path under `root` -> blob id) below `root`, never outside."""
    found = git.blobs(repo, list(dict.fromkeys(files.values())))
    top = root.resolve()
    for target, oid in files.items():
        path = root / safe_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(top) or path.is_symlink():
            die(f"refusing to write {target!r} outside {root}")
        path.write_bytes(found[oid])


def added_lines(repo: str, old: str, new: str, text: str) -> set[int]:
    """Returns the lines blob `new` adds over blob `old`; every line when there is no `old`."""
    if not old:
        return set(range(1, text.count("\n") + 2))
    return git.added_lines(git.out(repo, "diff", "-U0", old, new))


def check(
    repo: str,
    rules: Rules,
    base: str,
    head: str,
    wrap: Wrap = lambda argv: argv,
    seconds: float = 600,
    workdir: Path | None = None,
) -> Verdict:
    """Returns the violations `head` adds over `base`: per rule and file, more than the base had.

    Semgrep runs once on both sides of the changed Python files, in a temporary directory under
    `workdir`; nothing is written into the repository.
    """
    profile = load_profile(rules)
    changed = git.raw_changes(repo, base, head)
    news = [new for *_, new in changed]
    if len(set(news)) != len(news):
        die(f"git lists a changed path twice in {base[:12]}..{head[:12]}: refusing the check")
    for *_, old, new in changed:
        safe_path(new)
        if old:
            safe_path(old)
    sources = {
        new: old_oid if old_mode in REGULAR else ""
        for _, old_mode, old_oid, new_mode, new_oid, _, new in changed
        if new.endswith(SOURCE_SUFFIX) and new_mode in REGULAR
    }
    blob = {new: new_oid for *_, new_oid, _, new in changed}
    regressions: list[Hit] = []
    fixed = 0
    with tempfile.TemporaryDirectory(prefix="qg-rules-", dir=workdir) as tmp:
        root = Path(tmp)
        heads = {f"head/{new}": blob[new] for new in sources}
        bases = {f"base/{new}": old for new, old in sources.items() if old}
        materialise(repo, {**heads, **bases}, root)
        names = {target: target.split("/", 1)[1] for target in (*heads, *bases)}
        hits = scan(profile, root, names, wrap, seconds)
        for new in sources:
            after = Counter(hit.rule for hit in hits[f"head/{new}"])
            before = Counter(hit.rule for hit in hits.get(f"base/{new}", []))
            fixed += sum((before - after).values())
            for rule in sorted(after):
                if after[rule] > before[rule]:
                    regressions += [hit for hit in hits[f"head/{new}"] if hit.rule == rule]
        if profile.comments is not None:
            for new, old in sources.items():
                text = (root / f"head/{new}").read_text(errors="replace")
                added = added_lines(repo, old, blob[new], text)
                regressions += [
                    Hit(profile.comments.id, new, line, profile.comments.message)
                    for line in comment_lines(text, new)
                    if line in added
                ]
    if profile.markdown is not None:
        regressions += [
            Hit(profile.markdown.id, new, 1, profile.markdown.message)
            for status, *_, new in changed
            if status == "A" and new.endswith(MARKDOWN_SUFFIX) and profile.markdown.applies(new)
        ]
    regressions.sort(key=lambda hit: (hit.path, hit.line, hit.rule))
    files = len({hit.path for hit in regressions})
    span = f"{base[:12]}..{head[:12]}, {len(changed)} changed file(s)"
    gone = f"; {fixed} old violation(s) gone" if fixed else ""
    if regressions:
        summary = f"rules: {len(regressions)} new violation(s) in {files} file(s) ({span}){gone}"
    else:
        summary = f"rules: no new violations ({span}){gone}"
    return Verdict(regressions, summary)


def baseline(repo: str, rules: Rules, sha: str) -> Counter[tuple[str, str]]:
    """Returns the violations per (rule, file) of the whole tree at `sha`, for the burn-down."""
    profile = load_profile(rules)
    entries = git.tree_entries(repo, sha)
    tracked = [path for _, _, path in entries]
    if len(set(tracked)) != len(tracked):
        die(f"git lists a path twice in the tree of {sha[:12]}: refusing the baseline")
    regular = {
        safe_path(path): oid
        for mode, oid, path in entries
        if path.endswith(SOURCE_SUFFIX) and mode in REGULAR
    }
    sources = list(regular)
    counts: Counter[tuple[str, str]] = Counter()
    with tempfile.TemporaryDirectory(prefix="qg-rules-") as tmp:
        root = Path(tmp)
        materialise(repo, {f"head/{name}": oid for name, oid in regular.items()}, root)
        targets = {f"head/{name}": name for name in sources}
        for found in scan(profile, root, targets, lambda argv: argv, 3600).values():
            counts.update((hit.rule, hit.path) for hit in found)
        if profile.comments is not None:
            for name in sources:
                text = (root / f"head/{name}").read_text(errors="replace")
                counts[(profile.comments.id, name)] += len(comment_lines(text, name))
    if profile.markdown is not None:
        for name in tracked:
            if name.endswith(MARKDOWN_SUFFIX) and profile.markdown.applies(name):
                counts[(profile.markdown.id, name)] += 1
    return +counts


def stage_base(repo: str, base: str, sha: str) -> str:
    """Returns the base the static stage ratchets against: the merge-base of `sha` with the
    run's base, else with the base branch."""
    return git.out(repo, "merge-base", sha, base or git.base_ref(repo))


def in_stage(
    box: Sandbox, rules: Rules, base: str, seconds: float, result: StageResult
) -> StageResult:
    """Adds the ratchet to the static stage's `result`: logged, a regression failing the stage
    unless `rules.enforce` is off, which only reports it."""
    verdict = check(
        box.repo,
        rules,
        stage_base(box.repo, base, box.sha),
        box.sha,
        lambda argv: box.wrap("static", argv),
        seconds,
        box.run_dir,
    )
    lines = [str(hit) for hit in verdict.regressions]
    summary = verdict.summary if rules.enforce else f"{verdict.summary} (advisory)"
    with (paths.logs_dir(box.run_id) / "static.log").open("a") as log:
        log.write("".join(f"{line}\n" for line in [f"$ {STAGE_COMMAND}", *lines, summary]))
    blocking = rules.enforce and not verdict.passed
    run = CommandRun(
        command=STAGE_COMMAND,
        code=1 if blocking else 0,
        tool=None,
        passed=0,
        failed=0,
        skipped=0,
        summary=summary,
    )
    return replace(
        result,
        status="failed" if blocking else result.status,
        summary=f"{result.summary}; {summary}",
        failures=[*result.failures, *(lines if rules.enforce else [])][:MAX_FAILURES],
        commands=[*result.commands, run],
    )
