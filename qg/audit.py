"""The nightly audit inside S0: deterministic tools over the night's diff, then, per lens, one
confined agent per commit part not audited before, then a refuter that tries to disprove that
part's findings; what it confirms goes to the queue, what it refutes only to the report, and the
part goes to the ledger so no later night reads it again. Nothing is capped but the time a hung
agent or audit may take, and reaching it fails the audit."""

import fnmatch
import json
import re
import shutil
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qg import agent as agents
from qg import backlog, git, jail, paths, proc, redact
from qg.config import Config
from qg.sandbox import Sandbox, base_env
from qg.util import atomic_write, fill, now_iso

PROMPTS = Path(__file__).parent
REFUTER_PROMPT = PROMPTS / "audit_refute_prompt.md"
LENS_TOOLS = "Read,Glob,Grep,Write"
LENS_MODEL = "sonnet"
SEVERITIES = ("P0", "P1", "P2", "P3")
CONFIRMED, REFUTED = "confirmed", "refuted"
TOOL_SECONDS = 600
CHUNK_FILES = 25
REFUTE_BATCH = 8
AGENT_MINUTES = 30
READ_ATTEMPTS = 3
NUMBERED = re.compile(r"^\s*(\d+)\t(.*)$", re.MULTILINE)
READ_LINE_CHARS = 2000
FOLD_CHARS = 1000
SHOWN_WHOLE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
SEMGREP_PACKS = ("python", "fastapi", "typescript", "react", "sql-injection", "jwt")
SEMGREP_URL = "https://semgrep.dev/c/p/{pack}"
SEMGREP_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs")
RUFF_RULES = ("S", "PERF")
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "requirements*.txt",
    "yarn.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
)
TOOLS = ("ruff", "semgrep", "gitleaks", "osv-scanner")
NEW_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
RULE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class AuditFailure(Exception):
    """A reason the audit cannot go on; its text becomes the simplify stage's summary."""


class UnitFailure(AuditFailure):
    """A reason one unit cannot be finished; the lens goes on with the others and fails after."""


@dataclass(frozen=True)
class Hit:
    """One result of a deterministic tool, kept only on a line the night's diff adds."""

    tool: str
    file: str
    line: int
    rule: str
    message: str


@dataclass(frozen=True)
class Unit:
    """What one lens agent reviews: one commit's reviewed files, or one part of them when the
    commit holds more than CHUNK_FILES; `others` are the files it changes outside the review,
    such as a lockfile."""

    sha: str
    subject: str
    files: tuple[str, ...]
    others: tuple[str, ...]
    part: int
    parts: int


@dataclass(frozen=True)
class Audited:
    """What the audit tells S0: one note for the stage summary and what its agents cost."""

    note: str
    cost_usd: float


def fetch_rules() -> list[Path]:
    """Downloads the semgrep registry packs into STATE_ROOT/audit/semgrep; returns their files."""
    paths.AUDIT_RULES.mkdir(parents=True, exist_ok=True)
    written = []
    for pack in SEMGREP_PACKS:
        with urllib.request.urlopen(SEMGREP_URL.format(pack=pack), timeout=60) as answer:
            body = answer.read().decode()
        target = paths.AUDIT_RULES / f"{pack}.yml"
        atomic_write(target, body)
        written.append(target)
    return written


def rule_files() -> list[Path]:
    """Returns the downloaded semgrep packs; fails when one is missing."""
    found = [paths.AUDIT_RULES / f"{pack}.yml" for pack in SEMGREP_PACKS]
    missing = [path.name for path in found if not path.is_file()]
    if missing:
        raise AuditFailure(f"semgrep rules missing ({', '.join(missing)}): run `qg audit-rules`")
    return found


def added_by_file(diff: str) -> dict[str, set[int]]:
    """Returns, per file of a unified diff with no context, the new-side lines it adds."""
    heads = list(NEW_FILE.finditer(diff))
    ends = [head.start() for head in heads[1:]] + [len(diff)]
    return {
        head.group(1): git.added_lines(diff[head.end() : end])
        for head, end in zip(heads, ends, strict=True)
    }


def _tool(command: list[str], src: Path, ok: tuple[int, ...] = (0,)) -> str:
    """Runs a deterministic tool in the sandbox checkout and returns its stdout."""
    result = proc.run(command, cwd=src, env=base_env(), timeout=TOOL_SECONDS)
    if result.returncode not in ok:
        raise AuditFailure(
            f"{command[0]} failed ({result.returncode}): {result.stderr.strip()[:300]}"
        )
    return result.stdout


def _relative(name: str, src: Path) -> str:
    """Returns a tool's path relative to the checkout."""
    path = Path(name)
    return str(path.relative_to(src)) if path.is_absolute() else name


def ruff_hits(src: Path, files: list[str], added: dict[str, set[int]]) -> list[Hit]:
    """Returns ruff's security and performance results on added lines of the Python files."""
    python = [name for name in files if name.endswith(".py")]
    if not python:
        return []
    raw = _tool(
        [
            "ruff",
            "check",
            "--no-cache",
            "--exit-zero",
            "--extend-select",
            ",".join(RUFF_RULES),
            "--output-format",
            "json",
            *python,
        ],
        src,
    )
    hits = []
    for item in json.loads(raw or "[]"):
        name, line, code = _relative(item["filename"], src), item["location"]["row"], item["code"]
        if code and code.rstrip("0123456789") in RUFF_RULES and line in added.get(name, ()):
            hits.append(Hit("ruff", name, line, code, item["message"]))
    return hits


def semgrep_hits(src: Path, files: list[str], added: dict[str, set[int]]) -> list[Hit]:
    """Returns semgrep's results on added lines, from the downloaded registry packs."""
    targets = [name for name in files if name.endswith(SEMGREP_SUFFIXES)]
    if not targets:
        return []
    configs = [arg for path in rule_files() for arg in ("--config", str(path))]
    raw = _tool(
        [
            "semgrep",
            "scan",
            *configs,
            "--json",
            "--metrics=off",
            "--disable-version-check",
            "--quiet",
            *targets,
        ],
        src,
    )
    hits = []
    for item in json.loads(raw)["results"]:
        name, line = _relative(item["path"], src), item["start"]["line"]
        if line in added.get(name, ()):
            rule = item["check_id"].rsplit(".", 1)[-1]
            hits.append(Hit("semgrep", name, line, rule, item["extra"]["message"].strip()))
    return hits


def gitleaks_hits(src: Path, base: str, sha: str, out: Path) -> list[Hit]:
    """Returns the secrets gitleaks finds in the commits base..sha, redacted by gitleaks."""
    _tool(
        [
            "gitleaks",
            "git",
            f"--log-opts={base}..{sha}",
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
            "--report-format",
            "json",
            "--report-path",
            str(out),
            str(src),
        ],
        src,
    )
    return [
        Hit("gitleaks", item["File"], item["StartLine"], item["RuleID"], item["Description"])
        for item in json.loads(out.read_text() or "[]")
    ]


def lockfiles(src: Path, tracked: list[str]) -> list[str]:
    """Returns the tracked dependency lockfiles of the checkout."""
    return [
        name
        for name in tracked
        if any(fnmatch.fnmatchcase(Path(name).name, pattern) for pattern in LOCKFILES)
        and (src / name).is_file()
    ]


def osv_hits(src: Path, locks: list[str]) -> list[Hit]:
    """Returns the known vulnerabilities osv-scanner reports for the packages of the lockfiles."""
    if not locks:
        return []
    raw = _tool(
        [
            "osv-scanner",
            "scan",
            "source",
            "--format",
            "json",
            *[arg for name in locks for arg in ("--lockfile", name)],
        ],
        src,
        ok=(0, 1),
    )
    hits = []
    for result in json.loads(raw).get("results") or []:
        name = _relative(result["source"]["path"], src)
        for found in result.get("packages") or []:
            package = found["package"]
            for vuln in found.get("vulnerabilities") or []:
                summary = vuln.get("summary") or vuln.get("details", "")[:160]
                message = f"{package['name']} {package['version']}: {summary}"
                hits.append(Hit("osv-scanner", name, 0, vuln["id"], message))
    return hits


def collect(box: Sandbox, base: str, files: list[str], adir: Path) -> list[Hit]:
    """Runs every deterministic tool over the reviewed `files` and the commits base..sha; returns
    the hits they keep."""
    missing = [name for name in TOOLS if shutil.which(name, path=base_env().get("PATH")) is None]
    if missing:
        raise AuditFailure(f"audit tools not on PATH: {', '.join(missing)}")
    added = added_by_file(git.out(box.repo, "diff", "-U0", f"{base}..{box.sha}"))
    tracked = git.out(box.repo, "ls-files", "-z").split("\0")
    return [
        *ruff_hits(box.src, files, added),
        *semgrep_hits(box.src, files, added),
        *gitleaks_hits(box.src, base, box.sha, adir / "gitleaks.json"),
        *osv_hits(box.src, lockfiles(box.src, tracked)),
    ]


def _text(raw: dict[str, Any], key: str, where: str) -> str:
    """Returns a non-empty string field of an agent's JSON, one line, or fails the audit."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuditFailure(f"{where}: `{key}` must be a non-empty string")
    return " ".join(value.split())


def read_findings(path: Path, src: Path, lens: str) -> list[dict]:
    """Returns a lens's findings, most severe first; a missing or malformed file fails the
    audit."""
    try:
        said = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AuditFailure(f"the {lens} lens left no valid {path.name}: {exc}") from exc
    if not isinstance(said, dict) or not isinstance(said.get("findings"), list):
        raise AuditFailure(f"the {lens} lens's {path.name} has no `findings` list")
    findings = []
    for index, raw in enumerate(said["findings"]):
        where = f"{lens} finding {index}"
        if not isinstance(raw, dict):
            raise AuditFailure(f"{where}: expected an object")
        found = {key: _text(raw, key, where) for key in ("file", "symbol", "rule", "title")}
        found |= {key: _text(raw, key, where) for key in ("severity", "evidence", "fix")}
        line = raw.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 0:
            raise AuditFailure(f"{where}: `line` must be an integer >= 0")
        if found["severity"] not in SEVERITIES:
            raise AuditFailure(f"{where}: `severity` must be one of {', '.join(SEVERITIES)}")
        if not RULE.match(found["rule"]):
            raise AuditFailure(f"{where}: `rule` must be a lowercase-hyphen slug")
        if not (src / found["file"]).is_file():
            raise AuditFailure(f"{where}: {found['file']} is not a file of the checkout")
        findings.append({**found, "line": line})
    findings.sort(key=lambda found: SEVERITIES.index(found["severity"]))
    return findings


def read_verdicts(path: Path, count: int) -> dict[int, dict[str, str]]:
    """Returns the refuter's verdict per finding index; an index it left out has none. A missing
    or malformed file fails the audit."""
    try:
        said = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AuditFailure(f"the refuter left no valid {path.name}: {exc}") from exc
    if not isinstance(said, dict) or not isinstance(said.get("verdicts"), list):
        raise AuditFailure(f"the refuter's {path.name} has no `verdicts` list")
    verdicts = {}
    for raw in said["verdicts"]:
        index = raw.get("index") if isinstance(raw, dict) else None
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
            raise AuditFailure(f"the refuter's verdict {raw!r} names no finding")
        verdict = _text(raw, "verdict", f"verdict {index}")
        if verdict not in (CONFIRMED, REFUTED):
            raise AuditFailure(f"verdict {index}: `verdict` must be {CONFIRMED} or {REFUTED}")
        severity = raw.get("severity", "")
        if verdict == CONFIRMED and severity not in SEVERITIES:
            raise AuditFailure(f"verdict {index}: a confirmation gives a `severity` P0..P3")
        verdicts[index] = {
            "verdict": verdict,
            "severity": severity,
            "why": _text(raw, "why", f"verdict {index}"),
        }
    return verdicts


def finding_key(lens: str, found: dict) -> str:
    """Returns what identifies a finding in the queue: its lens, file, rule and symbol."""
    return "\0".join((lens, found["file"], found["rule"], found["symbol"]))


def queue_item(lens: str, found: dict, severity: str, secrets: tuple[str, ...]) -> backlog.Item:
    """Returns the queue item of a confirmed finding, identified by lens, file, rule and symbol."""
    title = redact.clean(found["title"], secrets)
    return backlog.Item(
        id="",
        file=found["file"],
        text=f"{found['file']}:{found['line']} [{severity} {lens}/{found['rule']}] {title}",
        run_id="",
        sha="",
        when="",
        lens=lens,
        severity=severity,
        evidence=redact.clean(found["evidence"], secrets),
        fix=redact.clean(found["fix"], secrets),
        key=finding_key(lens, found),
    )


def known_block(items: list[backlog.Item], lens: str) -> str:
    """Returns what a lens's prompt says of the queue: findings it must not report again."""
    listed = [
        f"- [{item.id}] {item.text} ({item.status})"
        for item in items
        if item.lens == lens and item.status != backlog.DONE
    ]
    return "\n".join(listed) if listed else "None yet."


def units(repo: str, base: str, sha: str, files: list[str]) -> tuple[list[Unit], list[str]]:
    """Returns the units of base..sha, oldest commit first, merges left out, and the commits that
    change none of the reviewed `files`."""
    reviewed = set(files)
    found: list[Unit] = []
    no_code = []
    for commit in git.out(repo, "rev-list", "--reverse", "--no-merges", f"{base}..{sha}").split():
        subject = git.out(repo, "log", "-1", "--format=%s", commit)
        listed = git.out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
        changed = tuple(name for name in listed.split("\0") if name)
        mine = sorted(name for name in changed if name in reviewed)
        others = tuple(name for name in changed if name not in reviewed)
        if not mine:
            no_code.append(f"{commit[:9]} {subject}")
            continue
        chunks = [mine[i : i + CHUNK_FILES] for i in range(0, len(mine), CHUNK_FILES)]
        found += [
            Unit(commit, subject, tuple(chunk), others, number, len(chunks))
            for number, chunk in enumerate(chunks, 1)
        ]
    return found, no_code


def ledger_file(repo: str) -> Path:
    """Returns the ledger of the canonical repository path `repo`: the units its lenses finished."""
    return paths.AUDIT_LEDGER / backlog.store(repo).name


def audited(repo: str, lens: str) -> set[tuple[str, str]]:
    """Returns the (commit, file) pairs of `repo` that the lens finished in a recorded unit."""
    target = ledger_file(repo)
    if not target.is_file():
        return set()
    return {
        (entry["commit"], name)
        for entry in json.loads(target.read_text())
        if entry["lens"] == lens
        for name in entry["files"]
    }


def record(repo: str, lens: str, unit: Unit, run_id: str) -> None:
    """Adds to the ledger of `repo` a unit the lens finished: every line read, every finding
    judged, what the refuter confirmed queued."""
    target = ledger_file(repo)
    entries = json.loads(target.read_text()) if target.is_file() else []
    entries.append(
        {
            "lens": lens,
            "commit": unit.sha,
            "part": unit.part,
            "parts": unit.parts,
            "files": list(unit.files),
            "run_id": run_id,
            "when": now_iso(),
        }
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def pending(found: list[Unit], done: set[tuple[str, str]]) -> list[Unit]:
    """Returns the units with a file missing from `done`, the lens's finished (commit, file)
    pairs."""
    return [unit for unit in found if not all((unit.sha, name) in done for name in unit.files)]


def unit_hits(unit: Unit, hits: list[Hit]) -> list[Hit]:
    """Returns the hits on the unit's files; its first part also gets those on the files its commit
    changes outside the review, such as a lockfile."""
    files = set(unit.files) | (set(unit.others) if unit.part == 1 else set())
    return [hit for hit in hits if hit.file in files]


def _hits_text(hits: list[Hit]) -> str:
    """Returns the hits as JSON lines."""
    return "".join(json.dumps(asdict(hit), ensure_ascii=False) + "\n" for hit in hits)


def _remaining(deadline: float) -> float:
    """Returns the seconds left before the audit's deadline; fails the audit when none are."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise AuditFailure("the audit ran out of its time limit")
    return left


def _trusted(agent: agents.AgentResult) -> None:
    """Fails the audit when an agent's run cannot be trusted."""
    if agent.problems:
        raise AuditFailure("; ".join(agent.problems))


@dataclass(frozen=True)
class Reads:
    """What an agent's successful Read calls returned: per checkout file, per line number, the
    longest text shown of that line, and the files they opened at all (an image shows no numbered
    lines)."""

    lines: dict[str, dict[int, int]]
    opened: set[str]


def lines_read(output: Path, src: Path) -> Reads:
    """Returns what the agent's successful Read calls returned, from its verbose JSON output. A
    missing or unreadable output fails the audit."""
    try:
        events = json.loads(output.read_text())
    except (OSError, ValueError) as exc:
        raise AuditFailure(f"{output.name} is not a readable verbose output: {exc}") from exc
    if not isinstance(events, list):
        raise AuditFailure(f"{output.name} holds no message list: was the agent run verbose?")
    blocks = [
        block
        for event in events
        if isinstance(event, dict) and event.get("type") in ("assistant", "user")
        for block in (event.get("message") or {}).get("content") or []
        if isinstance(block, dict)
    ]
    root = src.resolve()
    asked = {}
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == "Read":
            target = Path(str((block.get("input") or {}).get("file_path", ""))).resolve()
            if target.is_relative_to(root):
                asked[block.get("id")] = str(target.relative_to(root))
    reads = Reads({}, set())
    for block in blocks:
        name = asked.get(block.get("tool_use_id"))
        if block.get("type") != "tool_result" or name is None or block.get("is_error"):
            continue
        reads.opened.add(name)
        content = block.get("content")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(
                str(part.get("text", "")) for part in content or [] if isinstance(part, dict)
            )
        )
        shown = reads.lines.setdefault(name, {})
        for number, line in NUMBERED.findall(text):
            shown[int(number)] = max(shown.get(int(number), 0), len(line))
    return reads


def merge_reads(into: Reads, more: Reads) -> None:
    """Adds what `more` read to `into`, keeping per line the longest text shown."""
    into.opened.update(more.opened)
    for name, shown in more.lines.items():
        mine = into.lines.setdefault(name, {})
        for number, length in shown.items():
            mine[number] = max(mine.get(number, 0), length)


def file_lines(src: Path, name: str) -> list[str] | None:
    """Returns a checkout file's lines split on newlines only, as the Read tool numbers them; None
    for a format Read shows whole (SHOWN_WHOLE). Anything but a regular file inside the checkout
    fails the audit: qg reads it outside the jail."""
    path = src / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(src.resolve()):
        raise AuditFailure(f"{name} is not a regular file of the checkout")
    if path.suffix.lower() in SHOWN_WHOLE:
        return None
    raw = path.read_bytes()
    if not raw:
        return []
    return raw.decode(errors="replace").split("\n")[: raw.count(b"\n") + (not raw.endswith(b"\n"))]


def fold_long_lines(src: Path, files: tuple[str, ...]) -> str:
    """Returns the lines of `files` of READ_LINE_CHARS or more, each under a `file:line` heading
    and folded every FOLD_CHARS characters, for the lens to read whole; empty when there are
    none."""
    out = []
    for name in files:
        for number, line in enumerate(file_lines(src, name) or [], 1):
            if len(line) >= READ_LINE_CHARS:
                out.append(f"== {name}:{number}")
                out += [line[i : i + FOLD_CHARS] for i in range(0, len(line), FOLD_CHARS)]
    return "\n".join(out) + "\n" if out else ""


def unread_ranges(
    src: Path, files: tuple[str, ...], reads: Reads, folded: bool = False
) -> dict[str, str]:
    """Returns, per file not read to its last line, its unread line ranges as `a-b, c-d`: a line
    counts once a Read showed it whole, or, at READ_LINE_CHARS or more, when `folded` (its folded
    copy was read to the end); an image or PDF counts once opened."""
    missing = {}
    for name in files:
        lines = file_lines(src, name)
        if lines is None:
            if name not in reads.opened:
                missing[name] = "the whole file"
            continue
        seen = reads.lines.get(name, {})
        gaps: list[list[int]] = []
        for number, line in enumerate(lines, 1):
            if seen.get(number, -1) >= len(line) or (folded and len(line) >= READ_LINE_CHARS):
                continue
            if gaps and gaps[-1][1] == number - 1:
                gaps[-1][1] = number
            else:
                gaps.append([number, number])
        if gaps:
            missing[name] = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in gaps)
    return missing


def _agent(
    box: Sandbox,
    prompt: str,
    *,
    name: str,
    out_dir: Path,
    readable: tuple[Path, ...],
    deadline: float,
    verbose: bool = False,
    model: str = "opus",
) -> agents.AgentResult:
    """Runs one uncapped, read-only audit agent on `model` for at most AGENT_MINUTES or what the
    audit has left, with the login renewed to cover it; fails the audit when its run cannot be
    trusted."""
    limit = min(AGENT_MINUTES * 60, _remaining(deadline))
    valid = limit + jail.LOGIN_MARGIN_SECONDS
    jail.ensure_login(jail.claude_binary(), base_env(), valid)
    result = agents.confined(
        box,
        prompt,
        name=name,
        tools=LENS_TOOLS,
        edits=False,
        max_turns=None,
        max_cost_usd=None,
        out_dir=out_dir,
        limit=limit,
        needed=valid,
        readable=readable,
        verbose=verbose,
        retry_on_limit=True,
        model=model,
    )
    _trusted(result)
    return result


def _own_dir(adir: Path, name: str) -> Path:
    """Creates the fresh, empty output directory of one agent; no other agent can write it."""
    target = adir / name
    target.mkdir()
    return target


def _review(
    box: Sandbox,
    adir: Path,
    lens: str,
    name: str,
    unit: Unit,
    hits: list[Hit],
    known: str,
    deadline: float,
    gaps: dict[str, str],
) -> tuple[list[dict], Reads, bool, float]:
    """Runs the lens over one unit, sent back to the `gaps` it left unread when there are some;
    returns its findings, what it read, whether it read the folded copy of the over-long lines
    to the end (true when there are none) and its cost."""
    udir = adir / "units"
    patch = udir / f"{name}.patch"
    atomic_write(
        patch,
        git.out(box.repo, "show", "--format=commit %H%n%n%B", unit.sha, "--", *unit.files) + "\n",
    )
    hits_file = udir / f"{name}.hits.jsonl"
    atomic_write(hits_file, _hits_text(unit_hits(unit, hits)))
    long = udir / f"{name}.long.txt"
    folded = fold_long_lines(box.src, unit.files)
    if folded:
        atomic_write(long, folded)
    lens_dir = _own_dir(adir, f"{lens}-{name}")
    findings_file = lens_dir / "findings.json"
    listed = (
        [f"- {file} (lines {gaps[file]} still unread)" for file in unit.files if file in gaps]
        if gaps
        else [f"- {file}" for file in unit.files]
    ) + (
        [
            f"- {long} (the lines above of {READ_LINE_CHARS} characters or more, which Read cuts,"
            f" folded every {FOLD_CHARS}: read it to its last line)"
        ]
        if folded
        else []
    )
    prompt = fill(
        (PROMPTS / f"audit_{lens}_prompt.md").read_text(),
        {
            "sha": box.sha,
            "commit": unit.sha,
            "subject": unit.subject,
            "part": str(unit.part),
            "parts": str(unit.parts),
            "diff_file": str(patch),
            "hits_file": str(hits_file),
            "findings_file": str(findings_file),
            "files": "\n".join(listed),
            "known": known,
        },
    )
    agent = _agent(
        box,
        prompt,
        name=f"audit-{lens}-{name}",
        out_dir=lens_dir,
        readable=(udir,),
        deadline=deadline,
        verbose=True,
        model=LENS_MODEL,
    )
    try:
        findings = read_findings(findings_file, box.src, lens)
    except AuditFailure as exc:
        raise UnitFailure(f"{unit.sha[:9]} part {unit.part}: {exc}") from exc
    read = lines_read(agent.output, box.src)
    whole = not folded or not unread_ranges(udir, (long.name,), lines_read(agent.output, udir))
    found = [{**finding, "commit": unit.sha} for finding in findings]
    return found, read, whole, agent.cost_usd or 0.0


def _read_unit(
    box: Sandbox,
    adir: Path,
    lens: str,
    index: int,
    unit: Unit,
    hits: list[Hit],
    known: str,
    deadline: float,
) -> tuple[list[dict], float]:
    """Runs the lens over one unit until qg has seen it read every line of every file, at most
    READ_ATTEMPTS runs; returns its findings deduplicated by file, rule and symbol, most severe
    first, and the cost. A line still unread fails the unit."""
    findings: list[dict] = []
    spent = 0.0
    read = Reads({}, set())
    folded = False
    gaps: dict[str, str] = {}
    for attempt in range(READ_ATTEMPTS):
        name = f"{index:02d}" + (f"-again{attempt}" if attempt else "")
        said, seen, whole, cost = _review(box, adir, lens, name, unit, hits, known, deadline, gaps)
        folded = folded or whole
        findings += said
        spent += cost
        merge_reads(read, seen)
        gaps = unread_ranges(box.src, unit.files, read, folded)
        if not gaps:
            break
    if gaps:
        left = "; ".join(f"{file} lines {ranges}" for file, ranges in gaps.items())
        raise UnitFailure(
            f"the {lens} lens left {unit.sha[:9]} part {unit.part} unread after "
            f"{READ_ATTEMPTS} runs: {left}"
        )
    findings.sort(key=lambda found: SEVERITIES.index(found["severity"]))
    kept: dict[tuple[str, str, str], dict] = {}
    for found in findings:
        kept.setdefault((found["file"], found["rule"], found["symbol"]), found)
    return list(kept.values()), spent


def _audit_lens(
    box: Sandbox,
    adir: Path,
    lens: str,
    todo: list[Unit],
    hits: list[Hit],
    deadline: float,
    secrets: tuple[str, ...],
) -> tuple[dict[str, Any], float]:
    """Runs the lens over the units of `todo`, oldest first, one at a time: reads it whole, has
    the refuter judge its findings that are neither queued nor judged earlier this run, queues
    what it confirms and records the unit in the ledger once each of them has a verdict; returns
    the lens's part of audit.json and the cost. A unit it cannot finish is left out of the ledger
    and the lens goes on; after the last unit, the audit fails naming each of them."""
    said: dict[str, Any] = {"queued": [], "known": 0, "refuted": [], "unverified": []}
    judged: set[str] = set()
    spent = 0.0
    unfinished: list[str] = []
    for index, unit in enumerate(todo):
        items = backlog.load(box.repo)
        try:
            found, cost = _read_unit(
                box, adir, lens, index, unit, hits, known_block(items, lens), deadline
            )
        except UnitFailure as exc:
            unfinished.append(str(exc))
            continue
        spent += cost
        keys = {item.key for item in items if item.key}
        said["known"] += sum(finding_key(lens, one) in keys for one in found)
        fresh = [one for one in found if finding_key(lens, one) not in keys | judged]
        verdicts, cost = _refute(box, adir, lens, f"{index:02d}", fresh, deadline)
        spent += cost
        judged |= {finding_key(lens, fresh[i]) for i in verdicts}
        confirmed = [
            queue_item(lens, one, verdicts[i]["severity"], secrets)
            for i, one in enumerate(fresh)
            if verdicts.get(i, {}).get("verdict") == CONFIRMED
        ]
        added = backlog.ingest_audit(box.repo, box.run_id, box.sha, confirmed)
        said["queued"] += [f"[{item.id}] {item.text}" for item in added]
        said["refuted"] += [
            redact.clean(
                f"{one['file']}:{one['line']} {one['title']}; {verdicts[i]['why']}", secrets
            )
            for i, one in enumerate(fresh)
            if verdicts.get(i, {}).get("verdict") == REFUTED
        ]
        unjudged = [
            redact.clean(f"{one['file']}:{one['line']} {one['title']}", secrets)
            for i, one in enumerate(fresh)
            if i not in verdicts
        ]
        said["unverified"] += unjudged
        if not unjudged:
            record(box.repo, lens, unit, box.run_id)
    if unfinished:
        raise AuditFailure("; ".join(unfinished))
    return said, spent


def _refute(
    box: Sandbox, adir: Path, lens: str, unit: str, findings: list[dict], deadline: float
) -> tuple[dict[int, dict[str, str]], float]:
    """Has the refuter judge the findings of the unit named `unit` in batches of REFUTE_BATCH;
    returns the verdict per finding index and the cost. The refuter sees the claims, never a
    lens's reasoning or fix."""
    verdicts: dict[int, dict[str, str]] = {}
    spent = 0.0
    for start in range(0, len(findings), REFUTE_BATCH):
        batch = findings[start : start + REFUTE_BATCH]
        name = f"{unit}-{start // REFUTE_BATCH:02d}"
        claims = _own_dir(adir, f"{lens}-claims-{name}") / "claims.json"
        atomic_write(
            claims,
            json.dumps(
                [
                    {"index": i, **{k: v for k, v in found.items() if k != "fix"}}
                    for i, found in enumerate(batch)
                ],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        refuter_dir = _own_dir(adir, f"{lens}-refuter-{name}")
        verdicts_file = refuter_dir / "verdicts.json"
        refuter = _agent(
            box,
            fill(
                REFUTER_PROMPT.read_text(),
                {
                    "sha": box.sha,
                    "lens": lens,
                    "claims_file": str(claims),
                    "verdicts_file": str(verdicts_file),
                },
            ),
            name=f"audit-{lens}-refuter-{name}",
            out_dir=refuter_dir,
            readable=(claims.parent,),
            deadline=deadline,
        )
        spent += refuter.cost_usd or 0.0
        said = read_verdicts(verdicts_file, len(batch))
        verdicts |= {start + i: verdict for i, verdict in said.items()}
    return verdicts, spent


def run(
    box: Sandbox, cfg: Config, base: str, files: list[str], secrets: tuple[str, ...]
) -> Audited:
    """Runs the tools, then each lens unit by unit over the reviewed `files` of base..sha, the
    units its ledger records left out; queues what the refuter confirms as each unit ends and
    writes audit/audit.json for the report."""
    assert cfg.audit is not None
    deadline = time.monotonic() + cfg.audit.time_limit_minutes * 60
    adir = paths.audit_dir(box.run_id)
    adir.mkdir()
    (adir / "units").mkdir()
    links = [name for name in files if (box.src / name).is_symlink()]
    files = [name for name in files if name not in links]
    hits = collect(box, base, files, adir)
    atomic_write(adir / "hits.jsonl", _hits_text(hits))
    found_units, no_code = units(box.repo, base, box.sha, files)
    spent = 0.0
    commits = len({unit.sha for unit in found_units})
    report: dict[str, Any] = {
        "files": len(files),
        "hits": len(hits),
        "commits": commits,
        "units": len(found_units),
        "no_code": no_code,
        "links": links,
        "lenses": {},
    }
    notes = []
    for lens in cfg.audit.lenses:
        todo = pending(found_units, audited(box.repo, lens))
        said, cost = _audit_lens(box, adir, lens, todo, hits, deadline, secrets)
        spent += cost
        said |= {"read": len(todo), "earlier": len(found_units) - len(todo)}
        report["lenses"][lens] = said
        notes.append(
            f"{lens}: {len(todo)} part(s) read, {said['earlier']} audited before, "
            f"{len(said['queued'])} queued, {len(said['refuted'])} refuted"
            + (f", {len(said['unverified'])} unverified" if said["unverified"] else "")
        )
    report["cost_usd"] = round(spent, 2)
    atomic_write(adir / "audit.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    scope = f"{commits} commit(s) in {len(found_units)} part(s), {len(no_code)} without code"
    tools = f"{len(hits)} tool hit(s) on {len(files)} file(s)"
    return Audited(f"audit: {scope}; {'; '.join(notes)}; {tools}; ${spent:.2f}", spent)
