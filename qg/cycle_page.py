"""The cycle page: one self-contained HTML page per feature cycle, rebuilt after each run."""

import base64
import hashlib
import html
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC
from pathlib import Path
from string import Template
from typing import Any

from qg import acs, config, cycles, git, ownership, paths, proc, queue, redact, release, report
from qg.cycles import Cycle
from qg.queue import Request, RunState, StageState
from qg.report import Finding
from qg.util import atomic_write, die, elapsed, human, now_iso, parse_iso

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
TEXT_SUFFIX = ".txt"
TEXT_PREFIX = "txt:"
TEXT_MAX_BYTES = 40 * 1024
IMAGE_TOOLS = ("ffmpeg", "magick", "convert")
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
REJECTED = "rejected"
MAX_WIDTH = 1280
IMAGE_BUDGET = 8 * 1024 * 1024
COMMIT_LIMIT = 40
FONTS = (
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
    "&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap"
)
STAGE_LABEL = {
    "passed": "OK",
    "failed": "FAILED",
    "skipped": "SKIPPED",
    "running": "RUNNING",
    "pending": "PENDING",
}
STAGE_TONE = {
    "passed": "ok",
    "failed": "bad",
    "skipped": "skip",
    "running": "warn",
    "pending": "skip",
}
RUN_LABEL = {"error": "ERROR", "cancelled": "CANCELLED", "interrupted": "INTERRUPTED"}
FINDING_LABEL = {
    "open": "open",
    "fixed": "fixed",
    "acknowledged": "acknowledged",
    "dismissed": "dismissed",
}
ENV_ORDER = ("prod", "staging")


@dataclass
class QaStep:
    """One step of a plan flow: its caption, width, locale and screenshot."""

    caption: str
    width: int
    screenshot: str
    locale: str = ""


@dataclass
class QaFlow:
    """One flow the QA stage walked: where and how; plan flows also carry their steps."""

    name: str
    widths: list[int] = field(default_factory=list)
    locales: list[str] = field(default_factory=list)
    result: str = ""
    summary: str = ""
    steps: list[QaStep] = field(default_factory=list)

    @property
    def is_plan(self) -> bool:
        """Tells whether the flow is a plan change, `plan:<short name>`."""
        return self.name.casefold().startswith("plan:")


def flow_from_dict(raw: dict[str, Any]) -> QaFlow:
    """Rebuilds a QaFlow from its JSON form."""
    return QaFlow(**{**raw, "steps": [QaStep(**step) for step in raw.get("steps", [])]})


@dataclass
class Attempt:
    """What one finished run contributes to the cycle page, frozen when the run finished."""

    run_id: str
    sha: str
    tree: str
    env: str
    status: str
    started: str | None
    ended: str | None
    error: str | None
    retest_of: str | None
    stages: dict[str, StageState]
    findings: list[Finding]
    thumbs: dict[str, str | None]
    verdicts: dict[str, list[str]]
    critical_specs: list[str]
    qa_flows: list[QaFlow]
    commits: list[str]
    commits_note: str
    run_dir: str
    files: list[str]
    requested_sha: str = ""
    simplified_ref: str = ""
    branch: str = ""
    log_ids: list[str] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float | None:
        """Wall-clock duration of the run."""
        return elapsed(self.started, self.ended)

    @property
    def ready(self) -> bool:
        """Tells whether the run satisfied the policy of its own env."""
        return self.status in ("passed", "failed") and self.verdicts.get(self.env) == []

    @property
    def failed_stage(self) -> str | None:
        """Name of the first stage that failed, if any."""
        return next((n for n, s in self.stages.items() if s.status == "failed"), None)


@dataclass
class Mention:
    """One attempt's view of a finding: the state it had there and the flow it named."""

    attempt: int
    state: str
    flow: str
    sha: str


@dataclass
class Merged:
    """A finding as the cycle sees it: its latest mention and where it was first and last seen."""

    finding: Finding
    first: int
    last: int
    state: str
    fixed_in: int | None
    thumbs: dict[str, str | None]
    tree: str = ""
    history: list[Mention] = field(default_factory=list)

    def reached(self, state: str) -> Mention | None:
        """Returns the first mention in which the finding was in `state`, None when never."""
        return next((m for m in self.history if m.state == state), None)


def image_tool() -> str | None:
    """Returns the first of ffmpeg, magick, convert found on PATH, None when there is none."""
    return next((name for name in IMAGE_TOOLS if shutil.which(name)), None)


def _convert_command(tool: str, source: Path, target: Path) -> list[str]:
    """Returns the command that writes `source` to `target` as a JPEG at most 1280 px wide."""
    if tool == "ffmpeg":
        scale = f"scale='min({MAX_WIDTH},iw)':-2"
        flags = ["-nostdin", "-loglevel", "error", "-y", "-i", str(source)]
        return ["ffmpeg", *flags, "-vf", scale, "-frames:v", "1", "-q:v", "6", str(target)]
    return [tool, str(source), "-resize", f"{MAX_WIDTH}x>", "-quality", "70", f"jpg:{target}"]


def thumbnail(source: Path, cache: Path) -> str | None:
    """Returns the name of the cached JPEG of image `source`, creating it; None if impossible."""
    tool = image_tool()
    if tool is None or not source.is_file() or source.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    stat = source.stat()
    key = hashlib.sha256(f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    target = cache / f"{key}.jpg"
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = cache / f"{key}.part.jpg"
        done = proc.run(_convert_command(tool, source, partial))
        if done.returncode != 0 or not partial.exists():
            return None
        partial.rename(target)
    return target.name


def _evidence_source(path: str, run_dir: Path) -> Path | None:
    """Resolves an evidence path to a regular image under runs/<id>/evidence/, else None."""
    candidate = Path(path)
    real = Path(os.path.realpath(candidate if candidate.is_absolute() else run_dir / candidate))
    root = Path(os.path.realpath(run_dir / "evidence"))
    if not real.is_relative_to(root) or not real.is_file():
        return None
    allowed = (*IMAGE_SUFFIXES, TEXT_SUFFIX)
    if real.suffix.lower() not in allowed or real.stat().st_size > EVIDENCE_MAX_BYTES:
        return None
    return real


def _qa_flows(run_id: str) -> list[QaFlow]:
    """Reads the flows the QA stage recorded in qa.json; a run without the file walked none."""
    path = paths.qa_file(run_id)
    if not path.exists():
        return []
    return [flow_from_dict(item) for item in json.loads(path.read_text())["flows"]]


def _commits(repo: str, previous: "Attempt | None", sha: str) -> tuple[list[str], str]:
    """Returns the commits this attempt adds over the previous one (or the base), and a label."""
    if previous is not None:
        if previous.sha == sha:
            return [], "same commit as the previous attempt"
        label = f"commits since the previous attempt ({previous.sha[:8]}..{sha[:8]})"
        lines = git.log_between(repo, previous.sha, sha, COMMIT_LIMIT)
    else:
        found = git.base_log(repo, sha, COMMIT_LIMIT)
        if found is None:
            return [], ""
        label = f"branch commits over {found[0]}"
        lines = found[1]
    if lines is None:
        return [], f"{label}: not available"
    return lines, label


def snapshot(state: RunState, request: Request, previous: Attempt | None) -> Attempt:
    """Freezes a finished run as an attempt: stages, findings, thumbnails, verdicts and commits."""
    cfg = config.parse(request.config, f"request of run {state.id}")
    findings = report.load_findings(state.id)
    known = report.tree_findings(state)
    run_dir = paths.run_dir(state.id)
    cache = paths.thumbs_dir(state.cycle)
    thumbs: dict[str, str | None] = {}
    for finding in findings:
        for path in finding.evidence:
            source = _evidence_source(path, run_dir)
            if source is None:
                thumbs[path] = REJECTED
            elif source.suffix.lower() == TEXT_SUFFIX:
                text = source.read_bytes()[:TEXT_MAX_BYTES].decode("utf-8", errors="replace")
                thumbs[path] = TEXT_PREFIX + text
            else:
                thumbs[path] = thumbnail(source, cache)
    flows = _qa_flows(state.id)
    for flow in flows:
        for step in flow.steps:
            source = _evidence_source(step.screenshot, run_dir)
            thumbs[step.screenshot] = REJECTED if source is None else thumbnail(source, cache)
    commits, note = _commits(state.repo, previous, state.sha)
    log_ids = _log_ids(run_dir)
    candidates = [paths.report_md(state.id), paths.report_json(state.id), run_dir / "executor.log"]
    candidates += sorted(paths.logs_dir(state.id).glob("*.log"))
    return Attempt(
        run_id=state.id,
        sha=state.sha,
        tree=state.tree,
        env=state.env,
        status=state.status,
        started=state.started,
        ended=state.ended,
        error=state.error,
        retest_of=state.retest_of,
        stages=state.stages,
        findings=findings,
        thumbs=thumbs,
        verdicts={env: report.judge(state, policy, known) for env, policy in cfg.deploy.items()},
        critical_specs=list(cfg.playwright.critical) if cfg.playwright else [],
        qa_flows=flows,
        commits=commits,
        commits_note=note,
        run_dir=str(run_dir),
        files=[str(path) for path in candidates if path.exists()],
        requested_sha=state.requested_sha,
        simplified_ref=state.simplified_ref,
        branch=state.branch,
        log_ids=log_ids,
        warnings=report.warnings_of(state),
    )


def _log_ids(run_dir: Path) -> list[str] | None:
    """Returns the ids of the log problems the run's stack log reproduces; None without a log."""
    from qg import qa

    ids = qa.stack_log_ids(run_dir / "logs" / "qa-stack.log")
    return None if ids is None else sorted(ids)


def _attempt_from_dict(raw: dict[str, Any]) -> Attempt:
    """Rebuilds an Attempt from its JSON form."""
    raw = {**raw, "log_ids": raw["log_ids"] if "log_ids" in raw else _log_ids(Path(raw["run_dir"]))}
    return Attempt(
        **{
            **raw,
            "stages": {name: queue.stage_from_dict(item) for name, item in raw["stages"].items()},
            "findings": [report.finding_from_dict(item) for item in raw["findings"]],
            "qa_flows": [flow_from_dict(item) for item in raw["qa_flows"]],
        }
    )


def load_attempts(cycle: Cycle) -> list[Attempt]:
    """Returns the recorded attempts of the cycle in run order; unfinished runs have none."""
    found = []
    for run_id in cycle.runs:
        path = paths.attempts_dir(cycle.id) / f"{run_id}.json"
        if path.exists():
            found.append(_attempt_from_dict(json.loads(path.read_text())))
    return found


def record(state: RunState, request: Request) -> None:
    """Writes the attempt snapshot of a finished run into its cycle."""
    cycle = cycles.load(state.cycle)
    earlier = [a for a in load_attempts(cycle) if a.run_id < state.id]
    attempt = snapshot(state, request, earlier[-1] if earlier else None)
    directory = paths.attempts_dir(cycle.id)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / f"{state.id}.json", json.dumps(asdict(attempt), indent=2) + "\n")


def merge_findings(attempts: list[Attempt]) -> list[Merged]:
    """Merges findings of all attempts by id.

    The first mention gives the description, every mention adds evidence, the latest decides
    severity, open or fixed, acknowledgement, dismissal and verification, and names the tree it
    was seen on. An
    attempt whose QA walked no flow only carries earlier findings forward: it keeps their tree, except the runner's own findings.
    """
    merged: dict[str, Merged] = {}
    for number, attempt in enumerate(attempts, start=1):
        for latest in attempt.findings:
            before = merged.get(latest.id)
            base = before.finding if before else latest
            evidence = list(dict.fromkeys([*base.evidence, *latest.evidence]))
            status = latest.status
            if (
                status == "open"
                and before is not None
                and latest.id.startswith("L-")
                and attempt.log_ids is not None
                and latest.id not in attempt.log_ids
                and attempt.tree != before.tree
            ):
                status = "fixed"
            finding = replace(
                base,
                severity=latest.severity,
                status=status,
                acknowledged=latest.acknowledged,
                dismissed=latest.dismissed,
                closed=latest.closed,
                verification=latest.verification,
                probe=latest.probe,
                evidence=evidence,
            )
            if finding.status == "fixed":
                state, fixed_in = "fixed", number
            elif finding.dismissed is not None:
                state, fixed_in = "dismissed", None
            elif finding.acknowledged is not None:
                state, fixed_in = "acknowledged", None
            else:
                state, fixed_in = "open", None
            thumbs = {**(before.thumbs if before else {}), **attempt.thumbs}
            first = before.first if before else number
            seen = Mention(number, state, latest.flow, attempt.sha)
            history = [*(before.history if before else []), seen]
            observed = attempt.qa_flows or latest.id.startswith(report.RUNNER_PREFIXES)
            seen_tree = attempt.tree if observed or before is None else before.tree
            merged[latest.id] = Merged(
                finding, first, number, state, fixed_in, thumbs, seen_tree, history
            )
    rank = {"open": 0, "acknowledged": 1, "dismissed": 2, "fixed": 3}
    return sorted(
        merged.values(),
        key=lambda m: (rank[m.state], report.SEVERITIES.index(m.finding.severity), m.finding.id),
    )


SECRETS: tuple[str, ...] = ()


def esc(value: object) -> str:
    """Redacts the run's secrets and credential patterns, masks emails, then HTML-escapes."""
    return html.escape(redact.clean(str(value), SECRETS), quote=True)


def fmt_time(text: str | None) -> str:
    """Formats a stored timestamp as 2026-10-14 12:01 UTC; a dash when unknown."""
    if text is None:
        return "-"
    return parse_iso(text).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _chip(label: str, tone: str) -> str:
    """Renders a square status chip with a semantic tone: ok, bad, warn or skip."""
    return f'<span class="chip {tone}">{esc(label)}</span>'


def _problem_tone(attempt: Attempt) -> str:
    """Returns bad when something failed or broke in the attempt, warn when it is only pending."""
    open_p0 = any(report.blocking(f) and f.severity == "P0" for f in attempt.findings)
    broken = (
        attempt.failed_stage
        or attempt.error
        or open_p0
        or attempt.status not in ("passed", "failed")
    )
    return "bad" if broken else "warn"


def _attempt_tone(attempt: Attempt) -> str:
    """Returns the tone of an attempt's verdict for its own env: ok, bad or warn."""
    return "ok" if attempt.ready else _problem_tone(attempt)


def _plural(count: int, one: str, many: str) -> str:
    """Returns '1 test' or '3 tests': the count with the singular or plural word."""
    return f"{count} {one if count == 1 else many}"


def _attempt_label(attempt: Attempt) -> str:
    """Returns READY, NOT READY or the word of an abnormal end."""
    if attempt.status in RUN_LABEL:
        return RUN_LABEL[attempt.status]
    return report.READY if attempt.ready else report.NOT_READY


def _verdict_env(attempt: Attempt) -> str | None:
    """Returns the environment whose verdict the page shows: the strictest, production's."""
    return next((env for env in (*ENV_ORDER, *attempt.verdicts) if env in attempt.verdicts), None)


def _seal(attempt: Attempt) -> str:
    """Renders the page's one verdict seal for the latest attempt, with its reasons and warnings."""
    reasons = attempt.verdicts[_verdict_env(attempt)]
    shown = "; ".join(reasons[:3]) + (f" (+{len(reasons) - 3})" if len(reasons) > 3 else "")
    ready = attempt.status in ("passed", "failed") and not reasons
    label = report.READY if ready else report.NOT_READY
    tone = "ok" if ready else _problem_tone(attempt)
    why = f'<span class="why">{esc(shown)}</span>' if reasons else ""
    warned = "".join(f'<span class="why">{esc(item)}</span>' for item in attempt.warnings)
    return f'<div class="seal {tone}"><span class="v">{label}</span>{why}{warned}</div>'


def _closure(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Renders the closed banner, the reopen history and the attempts made after the closure."""
    parts = []
    for item in cycle.reopened:
        parts.append(
            f'<p class="note">Reopened on {esc(fmt_time(item["at"]))}: {esc(item["reason"])} '
            f'(deploy <span class="mono">{esc(item["sha"][:8])}</span> reverted)</p>'
        )
    if cycle.closed_at:
        parts.append(
            f'<div class="seal ok"><span class="v">Closed · deployed '
            f'<span class="mono">{esc(cycle.closed_sha[:8])}</span></span>'
            f'<span class="why">{esc(cycle.closed_env)} · {esc(fmt_time(cycle.closed_at))} · '
            f'run <span class="mono">{esc(cycle.closed_run)}</span></span></div>'
        )
        late = [(n, a) for n, a in enumerate(attempts, start=1) if a.run_id > cycle.closed_run]
        if late:
            rows = "".join(
                f'<tr><td>{n}</td><td class="mono">{esc(a.run_id)}</td>'
                f'<td class="mono">{esc(a.sha[:8])}</td><td>{esc(_attempt_label(a))}</td>'
                f"<td>{esc(fmt_time(a.ended))}</td></tr>"
                for n, a in late
            )
            parts.append(
                "<h3>After closing (should not have happened)</h3>"
                '<div class="scroll"><table><thead><tr><th>Attempt</th><th>Run</th><th>Commit</th>'
                f"<th>Result</th><th>End</th></tr></thead><tbody>{rows}</tbody></table></div>"
            )
    return f'<section id="cierre">{"".join(parts)}</section>' if parts else ""


AC_LABEL = {
    acs.OK: ("✅", "ok"),
    acs.FAILED: ("❌", "bad"),
    acs.UNTESTED: ("no test", "skip"),
    acs.UNKNOWN: ("no result", "warn"),
    acs.NOT_RUN: ("not run", "skip"),
}


def phase_criteria(cycle: Cycle, attempts: list[Attempt]) -> list[acs.Criterion]:
    """Returns the AC table of the cycle's PRD phase for its latest attempt; empty without a PRD.

    The ids are the `@<prd>-<phase>-ACnn` tags of the features at the attempt's commit, plus the
    ids the cycle's plan file names, so a criterion nobody tagged reads `no test`.
    """
    if not cycle.prd:
        return []
    latest = attempts[-1]
    covered = acs.scan(cycle.repo, latest.sha, cycle.prd, cycle.phase)
    named: list[str] = []
    plan = Path(cycle.plan) if cycle.plan else None
    if plan is not None and plan.is_file():
        named = acs.tag_pattern(cycle.prd, cycle.phase).findall(plan.read_text(errors="replace"))
    stage = latest.stages.get("playwright")
    status = stage.status if stage else None
    specs = stage.specs if stage else []
    texts = acs.spec_texts(cycle.repo, latest.sha, specs)
    return acs.criteria(
        covered,
        status,
        stage.failures if stage else [],
        named,
        lambda feature: acs.running_specs(feature, texts, specs),
    )


def _phase_line(cycle: Cycle) -> str:
    """Renders PRD id and phase, the link back to the PRD artifact and open or closed."""
    if not cycle.prd:
        return ""
    name = esc(f"{cycle.prd} · {cycle.phase}")
    link = f'<a href="{esc(cycle.prd_artifact_url)}">{name}</a>' if cycle.prd_artifact_url else name
    state = "Closed" if cycle.closed_at else "Open"
    return f"PRD {link} · {state} · "


def _acs_section(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Renders the AC table: each criterion's result and the scenarios that cover it."""
    found = phase_criteria(cycle, attempts)
    if not cycle.prd:
        return ""
    if not found:
        body = '<p class="empty">No acceptance criterion tagged in the features.</p>'
    else:
        rows = ""
        for item in found:
            label, tone = AC_LABEL[item.status]
            tests = "".join(
                f'<li><span class="mono">{esc(t.file)}</span> · {esc(t.scenario)}</li>'
                for t in item.tests
            )
            rows += (
                f'<tr><td class="mono">{esc(item.id)}</td>'
                f'<td><span class="chip {tone}">{esc(label)}</span></td>'
                f"<td>{'<ul>' + tests + '</ul>' if tests else '—'}</td></tr>"
            )
        body = (
            '<div class="scroll"><table><thead><tr><th>Criterion</th><th>Result</th>'
            f"<th>Covering test</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    return f'<section id="criterios"><h2>Acceptance criteria</h2>{body}</section>'


def _header(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Renders the page header: feature, repo and branch, verdict seals and key facts."""
    latest = attempts[-1]
    seals = _seal(latest) if _verdict_env(latest) else ""
    facts = [
        ("Verified commit", f'<span class="mono">{esc(latest.sha[:8])}</span>'),
        ("Attempts", str(len(attempts))),
        ("Last attempt", esc(fmt_time(latest.ended))),
    ]
    if latest.simplified_ref:
        facts.append(
            ("Requested commit", f'<span class="mono">{esc(latest.requested_sha[:8])}</span>')
        )
        name = latest.simplified_ref.removeprefix("refs/heads/")
        facts.append(("Simplified in", f'<span class="mono">{esc(name)}</span>'))
    items = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in facts)
    return (
        '<header class="head">'
        f'<p class="eyebrow">{_phase_line(cycle)}Verification cycle · '
        f'<span class="mono">{esc(Path(cycle.repo).name)}</span>'
        f' / <span class="mono">{esc(cycle.branch)}</span></p>'
        f"<h1>{esc(cycle.feature)}</h1>"
        f'<div class="seals">{seals}</div><dl class="facts">{items}</dl></header>'
    )


def _qa_ran(attempts: list[Attempt]) -> bool:
    """Tells whether the QA stage ran in any attempt."""
    return any(
        a.stages.get("qa") and a.stages["qa"].status in ("passed", "failed") for a in attempts
    )


STAGE_ORDER = ("static", "backend", "frontend", "playwright", "qa")
QA_FAILURES = (
    ("subscription access", "did not start: Claude account", "QA did not start: Claude account"),
    ("not logged in", "did not start: Claude account", "QA did not start: Claude account"),
    ("account cannot run", "Claude account", "QA: Claude account"),
    ("claude login", "Claude login", "QA: Claude login"),
    ("stack not ready", "stack did not start", "QA: stack did not start"),
    ("stack died", "stack died", "QA: stack died"),
    ("no readable json", "agent left no result", "QA: agent left no result"),
    ("qa agent failed", "agent failed", "QA: agent failed"),
)
STATIC_KINDS = (("ruff", "lint"), ("lint", "lint"), ("typecheck", "types"))
TITLE_LIMIT = 400
REASON_LIMIT = 60
SENTENCE_LIMIT = 160
BLOCKER = re.compile(r"\b([A-Z]-[0-9a-f]+) \(run ([\w-]+)(?:, (?:unverified|sin verificar))?\)")
RESULT_TONE = {"walked": "ok", "fixed": "ok", "still_present": "bad", "blocked": "warn"}


def _tip(text: str) -> str:
    """Returns the text cut to what a tooltip can carry."""
    return text if len(text) <= TITLE_LIMIT else text[:TITLE_LIMIT] + "…"


def _owner_label(cycle: Cycle) -> str:
    """Returns the phase label of a cycle, empty when it has no PRD phase."""
    return ownership.label(cycle.prd, cycle.phase) if cycle.prd else ""


def _stage_failure(attempt: Attempt, name: str, mine: str) -> tuple[str, str]:
    """Returns the short reason a failed stage gives in a stage chip and in the attempts grid."""
    stage = attempt.stages[name]
    text = f"{stage.summary} {' '.join(stage.failures)}".lower()
    if name == "setup":
        tail = "worktree unfit" if "gitignored" in text else "failed"
        return tail, f"setup: {tail}"
    if name == "qa":
        for needle, tail, grid in QA_FAILURES:
            if needle in text:
                return tail, grid
        return "failed", "QA failed"
    if stage.owners and len(stage.owners) == len(stage.failures):
        others = sorted({o for o in stage.owners if o and o != mine})
        if others and mine not in stage.owners:
            blocked = f"blocked by {', '.join(others)}"
            return blocked, blocked
    failed = sum(c.failed for c in stage.commands)
    if failed:
        tail = _plural(failed, "test", "tests")
    else:
        broken = next((c.command for c in stage.commands if c.code), "")
        tail = next((kind for key, kind in STATIC_KINDS if key in broken), "failed")
    return tail, f"{name} failed: {tail}"


def _blockers(attempt: Attempt, cycle_id: str) -> tuple[str, str]:
    """Returns the reason and tooltip of an attempt held back by findings or another phase."""
    env = _verdict_env(attempt)
    reasons = attempt.verdicts[env] if env else []
    own, foreign, titles = [], set(), []
    for line in reasons:
        severity = "P0" if "P0 finding" in line else ""
        for finding_id, run_id in BLOCKER.findall(line):
            state = queue.load(run_id) if paths.state_file(run_id).exists() else None
            if state is not None and state.cycle != cycle_id and cycles.exists(state.cycle):
                foreign.add(_owner_label(cycles.load(state.cycle)))
            else:
                own.append(f"{severity} {finding_id}".strip())
            found = [f for f in report.load_findings(run_id) if f.id == finding_id]
            titles.append(f"{finding_id}: {found[0].title}" if found else finding_id)
    parts = list(dict.fromkeys(own))
    if foreign:
        parts.append(f"blocked by {', '.join(sorted(foreign))}")
    if parts:
        return " · ".join(parts), _tip("\n".join(titles))
    first = reasons[0] if reasons else ""
    short = first if len(first) <= REASON_LIMIT else first[:REASON_LIMIT] + "…"
    return short, _tip("\n".join(reasons))


def attempt_reason(attempt: Attempt, cycle: Cycle) -> tuple[str, str]:
    """Returns the few words that say why an attempt ended as it did, and the full detail."""
    if attempt.status == "cancelled":
        dropped = release.FAIL_FAST.fullmatch(attempt.error or "")
        if dropped:
            return f"cancelled: the release's shared stage {dropped[1]} failed", _tip(
                attempt.error or ""
            )
        return "cancelled by the operator", _tip(attempt.error or "")
    if attempt.status == "interrupted":
        preempted = (attempt.error or "").startswith("preempted for run ")
        return (attempt.error if preempted else "interrupted"), _tip(attempt.error or "")
    failed = next(
        (
            n
            for n in ("setup", *STAGE_ORDER)
            if attempt.stages.get(n) and attempt.stages[n].status == "failed"
        ),
        None,
    )
    if failed:
        grid = _stage_failure(attempt, failed, _owner_label(cycle))[1]
        return grid, _tip(attempt.stages[failed].summary or attempt.error or "")
    if attempt.status == "error":
        return "run error", _tip(attempt.error or "")
    if attempt.ready:
        return "all passed", ""
    return _blockers(attempt, cycle.id)


def _attempts_grid(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Renders the attempts as one compact table, a row each, newest first."""
    rows = []
    for number in range(len(attempts), 0, -1):
        attempt = attempts[number - 1]
        reason, detail = attempt_reason(attempt, cycle)
        added = (
            f' <span class="meta">+{len(attempt.commits)}</span>'
            if number > 1 and attempt.commits
            else ""
        )
        when = (
            parse_iso(attempt.started).astimezone(UTC).strftime("%d/%m %H:%M")
            if attempt.started
            else "-"
        )
        retest = f"retest of {attempt.retest_of}" if attempt.retest_of else ""
        rows.append(
            f'<tr><td class="num">{number}</td>'
            f'<td class="mono" title="{esc(f"{attempt.env} {retest}")}">'
            f"{esc(attempt.sha[:8])}{added}</td>"
            f"<td>{_chip(_attempt_label(attempt), _attempt_tone(attempt))}</td>"
            f'<td class="mono when">{esc(when)}</td>'
            f'<td class="num dur">{esc(human(attempt.seconds))}</td>'
            f'<td title="{esc(detail)}">{esc(reason)}</td></tr>'
        )
    return (
        '<section id="intentos"><h2>Attempts</h2><div class="scroll">'
        '<table class="grid"><thead><tr>'
        '<th class="num">#</th><th>commit</th><th>verdict</th><th class="when">when (UTC)</th>'
        '<th class="num dur">duration</th><th>reason</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def _stage_chip(attempt: Attempt, name: str, mine: str) -> str:
    """Renders one stage as a chip with its short result; folded detail goes in the tooltip."""
    stage = attempt.stages[name]
    label = "QA" if name == "qa" else name
    counts = [c for c in stage.commands if c.tool]
    if stage.status == "failed":
        text, tone = f"{label} ✗ {_stage_failure(attempt, name, mine)[0]}", "bad"
    else:
        total = sum(c.passed for c in counts)
        extra = f" {total}" if counts and total else ""
        if name == "qa":
            extra = f" {_plural(len(attempt.qa_flows), 'flow', 'flows')}"
        text, tone = f"{label} ✓{extra}", "ok"
    mark = " ↺" if stage.reused_from else ""
    reused = f"reused from {stage.reused_from}\n" if stage.reused_from else ""
    tip = _tip(f"{reused}{stage.summary}")
    return f'<span class="chip {tone}" title="{esc(tip)}">{esc(text + mark)}</span>'


def _stage_line(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Renders the latest attempt's stages as one line of chips, in execution order."""
    latest = attempts[-1]
    mine = _owner_label(cycle)
    names = [n for n in ("setup", *STAGE_ORDER) if n in latest.stages]
    names = [n for n in names if latest.stages[n].status in ("passed", "failed")]
    names = [n for n in names if n != "setup" or latest.stages[n].status == "failed"]
    chips = [_stage_chip(latest, n, mine) for n in names]
    qa = latest.stages.get("qa")
    if qa and qa.status == "passed":
        p0 = sum(1 for f in latest.findings if f.severity == "P0")
        spent = f" · ${qa.cost_usd:.2f}" if qa.cost_usd is not None else ""
        found = f"{len(latest.findings)} findings ({p0} P0){spent}"
        chips.append(f'<span class="meta">{found}</span>')
    body = "".join(chips) or '<span class="meta">no stages run</span>'
    return (
        f'<section id="etapas"><h2>Stages of the last attempt</h2>'
        f'<div class="f-head">{body}</div></section>'
    )


def _ran(attempts: list[Attempt], stage: str) -> list[tuple[int, Attempt]]:
    """Returns the (attempt number, attempt) pairs in which `stage` actually ran."""
    return [
        (number, attempt)
        for number, attempt in enumerate(attempts, start=1)
        if attempt.stages.get(stage) and attempt.stages[stage].status in ("passed", "failed")
    ]


def _shot(uri: str, name: str, caption: str) -> str:
    """Renders one clickable evidence thumbnail with its short caption."""
    return (
        '<figure class="shot" tabindex="0" role="button" aria-label="Enlarge screenshot">'
        f'<img src="{uri}" alt="{esc(name)}"><figcaption class="mono">{esc(caption)}</figcaption>'
        "</figure>"
    )


def _flow_shots(attempt: Attempt, flow: QaFlow, cycle_id: str, budget: "Budget") -> tuple[str, int]:
    """Returns the thumbnails of what the QA captured in a flow, and how many did not fit."""
    pairs = [
        (s.screenshot, f"{s.width} px · {s.locale}" if s.locale else f"{s.width} px")
        for s in flow.steps
    ]
    shots, missing = [], 0
    for path, caption in pairs:
        name = attempt.thumbs.get(path)
        thumb = paths.thumbs_dir(cycle_id) / name if name and name != REJECTED else None
        uri = budget.take(thumb) if thumb and thumb.is_file() and thumb.suffix == ".jpg" else None
        if uri:
            shots.append(_shot(uri, Path(path).name, caption))
        else:
            missing += 1
    return "".join(shots), missing


def _flow_card(attempt: Attempt, flow: QaFlow, cycle_id: str, budget: "Budget") -> str:
    """Renders one QA flow as a finding-style card with its captures right below."""
    word, _, rest = flow.result.partition(":")
    tone = RESULT_TONE.get(word.strip(), "skip")
    name = flow.name.removeprefix("plan:").strip()
    full = (flow.summary or rest or flow.result).strip()
    first = full.split(". ")[0].rstrip(".")
    sentence = first if len(first) <= SENTENCE_LIMIT else first[:SENTENCE_LIMIT].rstrip() + "…"
    shots, missing = _flow_shots(attempt, flow, cycle_id, budget)
    gallery = f'<div class="shots flow-shots">{shots}</div>' if shots else ""
    note = f"{missing} screenshot(s) not included in the page" if missing else ""
    note = note or ("" if shots else "no screenshots of this flow")
    gone = f'<p class="meta">{note}</p>' if note else ""
    more = [t for t in (flow.summary, flow.result) if t.strip() and t.strip() != sentence]
    body = "".join(f"<p>{esc(t)}</p>" for t in more)
    detail = f"<details><summary>show detail</summary>{body}</details>" if body else ""
    return (
        '<article class="finding">'
        f'<div class="f-head">{_chip(word.strip() or "no result", tone)}'
        f'<span class="mono">{esc(name)}</span></div>'
        f'<p class="meta">{esc(sentence)}</p>{gallery}{gone}{detail}</article>'
    )


def _pruebas_qa(attempts: list[Attempt], cycle_id: str, budget: "Budget") -> str:
    """Renders the flows the QA stage walked, one card each with its screenshots right below."""
    ran = _ran(attempts, "qa")
    if not ran:
        stage = attempts[-1].stages.get("qa")
        reason = f" Reason: {stage.summary}." if stage and stage.summary else ""
        return f'<p class="empty">Not run in any attempt.{esc(reason)}</p>'
    number, attempt = ran[-1]
    if not attempt.qa_flows:
        return '<p class="empty">The QA did not record the flows it walked.</p>'
    cards = "".join(_flow_card(attempt, flow, cycle_id, budget) for flow in attempt.qa_flows)
    return f'<p class="meta">Manual QA of attempt {number}</p>{cards}'


def _pruebas(attempts: list[Attempt], cycle_id: str, budget: "Budget") -> str:
    """Renders what was exercised: the flows the manual QA pass walked."""
    return (
        f'<section id="pruebas"><h2>Tests</h2>{_pruebas_qa(attempts, cycle_id, budget)}</section>'
    )


class Budget:
    """Tracks how many bytes of embedded evidence images the page may still carry."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self, thumb: Path) -> str | None:
        """Returns the data URI of `thumb` when it fits in what is left, else None."""
        if thumb.stat().st_size * 4 // 3 + 32 > self.remaining:
            return None
        encoded = base64.b64encode(thumb.read_bytes()).decode("ascii")
        uri = f"data:image/jpeg;base64,{encoded}"
        self.remaining -= len(uri)
        return uri


def _evidence(item: Merged, cycle_id: str, budget: Budget) -> str:
    """Renders a finding's evidence: embedded thumbnails first, then the paths that did not fit."""
    shots, listed, logs = [], [], []
    for path in item.finding.evidence:
        name = item.thumbs.get(path)
        if name == REJECTED:
            listed.append(f'<li class="mono">rejected evidence path: {esc(path)}</li>')
            continue
        if name and name.startswith(TEXT_PREFIX):
            logs.append(
                f'<figure class="logx"><figcaption class="mono">{esc(Path(path).name)}</figcaption>'
                f'<pre class="log">{esc(name.removeprefix(TEXT_PREFIX))}</pre></figure>'
            )
            continue
        thumb = paths.thumbs_dir(cycle_id) / name if name else None
        uri = budget.take(thumb) if thumb and thumb.exists() else None
        if uri:
            shots.append(
                f'<figure class="shot" tabindex="0" role="button" aria-label="Enlarge screenshot">'
                f'<img src="{uri}" alt="{esc(Path(path).name)}"><figcaption class="mono">'
                f"{esc(Path(path).name)}</figcaption></figure>"
            )
        else:
            listed.append(f'<li class="mono">{esc(path)}</li>')
    out = ""
    if shots:
        out += f'<div class="shots">{"".join(shots)}</div>'
    out += "".join(logs)
    if listed:
        out += f'<p class="label">Evidence by path</p><ul class="paths">{"".join(listed)}</ul>'
    return out


def _closed_text(closed: report.Closure) -> str:
    """Spells out a P2 or P3 closed without a run: who, when, the commit and the note."""
    return (
        f"closed without a run by {closed.who} on {fmt_time(closed.when)} "
        f"(commit {closed.commit[:8]}): {closed.note}"
    )


def _status_text(item: Merged) -> str:
    """Spells out a finding's status; a decision of the operator names who, when and the note."""
    finding = item.finding
    if item.state == "fixed" and finding.closed:
        return _closed_text(finding.closed)
    if item.state == "fixed":
        return f"fixed in attempt {item.fixed_in}"
    if item.state == "dismissed" and finding.dismissed:
        d = finding.dismissed
        return f"dismissed by {d.who} on {fmt_time(d.when)}: {d.note}"
    if item.state == "acknowledged" and finding.acknowledged:
        a = finding.acknowledged
        return f"acknowledged by {a.who} on {fmt_time(a.when)}: {a.note}"
    if item.last > item.first:
        return f"Open since attempt {item.first}"
    return "open"


def _finding_tone(item: Merged) -> str:
    """Returns ok for a fixed finding, warn for an acknowledged one, skip when dismissed."""
    if item.state == "fixed":
        return "ok"
    if item.state == "dismissed":
        return "skip"
    if item.state == "acknowledged":
        return "warn"
    return "bad" if item.finding.severity in ("P0", "P1") else "skip"


def _finding(item: Merged, cycle_id: str, budget: Budget) -> str:
    """Renders one finding with its steps, expected and actual behaviour and evidence."""
    finding = item.finding
    tone = _finding_tone(item)
    steps = "".join(f"<li>{esc(step)}</li>" for step in finding.steps)
    blocks = []
    if steps:
        blocks.append(f'<div><p class="label">Steps</p><ol class="steps">{steps}</ol></div>')
    if finding.expected:
        blocks.append(f'<div><p class="label">Expected</p><p>{esc(finding.expected)}</p></div>')
    if finding.actual:
        blocks.append(f'<div><p class="label">Actual</p><p>{esc(finding.actual)}</p></div>')
    area = f'<span class="meta">{esc(finding.area)}</span>' if finding.area else ""
    seen = f"seen in attempt {item.first}" + (
        f", last {item.last}" if item.last != item.first else ""
    )
    notes = [report.verification_note(finding), *report.probe_notes(finding)]
    checked = "".join(f'<p class="meta">{esc(note)}</p>' for note in notes if note)
    return (
        '<article class="finding">'
        f'<div class="f-head"><span class="chip sev-{finding.severity}">{finding.severity}</span>'
        f'<span class="mono">{esc(finding.id)}</span>{area}'
        f"{_chip(FINDING_LABEL[item.state], tone)}</div>"
        f"<h3>{esc(finding.title)}</h3>"
        f'<p class="meta">{esc(_status_text(item))} · {esc(seen)}</p>{checked}'
        f'<div class="cols3">{"".join(blocks)}</div>{_evidence(item, cycle_id, budget)}</article>'
    )


def improved_rows(items: list[Merged], attempts: list[Attempt]) -> str:
    """Renders the earlier findings now fixed or acknowledged: what, who found it, what closed it."""
    rows = []
    for item in sorted(items, key=lambda m: (m.first, m.finding.id)):
        f = item.finding
        if item.state == "fixed" and f.closed:
            how = _closed_text(f.closed)
        elif item.state == "fixed":
            end = item.reached("fixed")
            number = end.attempt if end else item.fixed_in
            how = f"fixed in attempt {number} (commit {attempts[number - 1].sha[:8]})"
        else:
            end = item.reached("acknowledged")
            number = end.attempt if end else item.last
            ack = f.acknowledged
            who = f" by {ack.who} on {fmt_time(ack.when)}: {ack.note}" if ack else ""
            how = f"acknowledged in attempt {number} (commit {attempts[number - 1].sha[:8]}){who}"
        rows.append(
            '<li class="improved">'
            f'<span class="chip sev-{f.severity}">{esc(f.severity)}</span> '
            f'<span class="mono">{esc(f.id)}</span> {esc(f.title)} '
            f'<span class="meta">found in attempt {item.first} · {esc(how)}</span></li>'
        )
    return f'<ul class="paths">{"".join(rows)}</ul>'


def improved_section(merged: list[Merged], attempts: list[Attempt]) -> str:
    """Renders "Improved since attempt N", empty when no finding was fixed or acknowledged."""
    items = [m for m in merged if m.state in ("fixed", "acknowledged") and m.first < len(attempts)]
    if not items:
        return ""
    since = min(m.first for m in items)
    return (
        f'<section id="mejorado"><h2>Improved since attempt {since}</h2>'
        f"{improved_rows(items, attempts)}</section>"
    )


def _findings(merged: list[Merged], attempts: list[Attempt], cycle_id: str, budget: Budget) -> str:
    """Renders the P0 and P1 findings section; nothing when there are none."""
    serious = [m for m in merged if m.finding.severity in ("P0", "P1")]
    if not serious:
        return ""
    body = "".join(_finding(m, cycle_id, budget) for m in serious)
    return f'<section id="hallazgos"><h2>Findings</h2>{body}</section>'


def _mejoras(merged: list[Merged], cycle_id: str, budget: Budget) -> str:
    """Renders the P2 and P3 findings as improvement suggestions."""
    ideas = [m for m in merged if m.finding.severity in ("P2", "P3")]
    if ideas:
        body = "".join(_finding(m, cycle_id, budget) for m in ideas)
    else:
        body = '<p class="empty">No improvement suggestions (P2 and P3).</p>'
    return f'<section id="mejoras"><h2>Improvements</h2>{body}</section>'


def _logs(attempts: list[Attempt]) -> str:
    """Renders the report and log paths of every attempt, all folded."""

    def listing(attempt: Attempt) -> str:
        """Lists the files of one attempt, marking those retention already deleted."""
        items = []
        for path in attempt.files:
            gone = "" if Path(path).exists() else " (deleted by retention)"
            items.append(f'<li class="mono">{esc(path)}{gone}</li>')
        return f'<ul class="paths">{"".join(items)}</ul>'

    body = "".join(
        f'<details><summary>Attempt {number}: <span class="mono">{esc(a.run_id)}</span></summary>'
        f"{listing(a)}</details>"
        for number, a in enumerate(attempts, start=1)
    )
    return (
        f'<section id="registros"><details><summary><b>Logs</b></summary>{body}</details></section>'
    )


CSS = """
:root{
  --bg:#f1f3f6;--surface:#ffffff;--ink:#14202b;--ink-2:#40505e;--ink-3:#5b6977;
  --line:#d7dde4;--line-2:#b8c2cd;--accent:#2a45c2;
  --ok:#12703a;--ok-bg:#e1f2e7;--bad:#b0241b;--bad-bg:#fbe7e5;
  --warn:#8a5800;--warn-bg:#fcefd2;--skip:#52606d;--skip-bg:#e6eaef;
  --display:"IBM Plex Sans Condensed","Arial Narrow",Arial,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0e141a;--surface:#151d25;--ink:#e5eaef;--ink-2:#b0bbc6;--ink-3:#8896a3;
    --line:#26323d;--line-2:#394857;--accent:#8fa3ff;
    --ok:#63d48d;--ok-bg:#11301f;--bad:#ff8f86;--bad-bg:#3b1815;
    --warn:#ffc769;--warn-bg:#352608;--skip:#9ba8b4;--skip-bg:#202a33;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#0e141a;--surface:#151d25;--ink:#e5eaef;--ink-2:#b0bbc6;--ink-3:#8896a3;
  --line:#26323d;--line-2:#394857;--accent:#8fa3ff;
  --ok:#63d48d;--ok-bg:#11301f;--bad:#ff8f86;--bad-bg:#3b1815;
  --warn:#ffc769;--warn-bg:#352608;--skip:#9ba8b4;--skip-bg:#202a33;
  color-scheme:dark;
}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.55 var(--body)}
img{max-width:100%}
.page{max-width:72rem;margin-inline:auto;padding-inline:1rem;padding-block:1.5rem 3rem;
  display:grid;gap:2.2rem}
.page>*{min-width:0}
section{display:grid;gap:.9rem;align-content:start}
h1,h2,h3,h4,p,ul,ol,dl,dd,figure{margin:0}
h1{font:600 clamp(1.7rem,5vw,2.7rem)/1.08 var(--display);text-wrap:balance;overflow-wrap:anywhere}
h2{font:600 .95rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-2);padding-bottom:.45rem;border-bottom:1px solid var(--line-2)}
h3{font:600 1rem/1.35 var(--body);text-wrap:balance}
h4{font:600 .68rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);margin-top:.5rem}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.mono{font-family:var(--mono);font-size:.86em;overflow-wrap:anywhere}
.meta{color:var(--ink-3);font-size:.86rem;overflow-wrap:anywhere}
.label{font:600 .68rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);margin-bottom:.25rem}
.empty{color:var(--ink-2);padding:.9rem 1rem;background:var(--surface);
  border:1px dashed var(--line-2)}
.eyebrow{font:500 .78rem/1.3 var(--display);letter-spacing:.09em;text-transform:uppercase;
  color:var(--ink-3);margin-bottom:.45rem}
.eyebrow .mono{text-transform:none;letter-spacing:0}
.head{display:grid;gap:1rem}
.seals{display:flex;flex-wrap:wrap;gap:.6rem}
.seal{display:grid;gap:.2rem;padding:.55rem .85rem;border:1.5px solid currentColor;
  border-radius:2px;min-width:0;max-width:100%}
.seal .k{font:600 .68rem/1 var(--display);letter-spacing:.12em;text-transform:uppercase}
.seal .v{font:600 1.15rem/1.1 var(--mono);letter-spacing:.03em}
.seal .why{font-size:.8rem;line-height:1.35;overflow-wrap:anywhere}
.facts{display:flex;flex-wrap:wrap;gap:.6rem 2.2rem}
.facts>div{min-width:0}
.facts dt{font:600 .68rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3)}
.facts dd{font-size:.95rem;overflow-wrap:anywhere}
.ok{color:var(--ok);background:var(--ok-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}
.warn{color:var(--warn);background:var(--warn-bg)}
.skip{color:var(--skip);background:var(--skip-bg)}
.sev-P0{color:var(--bad);background:var(--bad-bg)}
.sev-P1{color:var(--warn);background:var(--warn-bg)}
.sev-P2,.sev-P3{color:var(--skip);background:var(--skip-bg)}
.bad-text{color:var(--bad)}
.chip{display:inline-block;font:600 .68rem/1 var(--mono);letter-spacing:.06em;
  text-transform:uppercase;padding:.3rem .42rem;border:1px solid currentColor;
  border-radius:2px;white-space:nowrap;vertical-align:middle}
.tiles{display:grid;gap:0;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  border-top:1px solid var(--line-2);border-left:1px solid var(--line-2)}
.tile{display:grid;gap:.1rem;align-content:start;background:var(--surface);padding:.75rem .9rem;
  min-width:0;border-right:1px solid var(--line-2);border-bottom:1px solid var(--line-2)}
.tl{font:600 .66rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3)}
.tv{font:500 1.9rem/1.1 var(--mono);font-variant-numeric:tabular-nums}
.ts{font-size:.8rem;color:var(--ink-2);overflow-wrap:anywhere}
.timeline{list-style:none;padding:0;display:grid;gap:1.3rem}
.attempt{display:grid;grid-template-columns:2.5rem minmax(0,1fr);gap:.9rem;position:relative}
.attempt::before{content:"";position:absolute;left:1.2rem;top:2.6rem;bottom:-1.3rem;width:2px;
  background:var(--line-2)}
.attempt:last-child::before{display:none}
.n{position:relative;display:grid;place-items:center;width:2.5rem;height:2.5rem;
  border:1.5px solid var(--line-2);background:var(--surface);font:600 1.1rem/1 var(--mono);
  font-variant-numeric:tabular-nums}
.body{display:grid;gap:.55rem;align-content:start;min-width:0;padding:.7rem .95rem;
  background:var(--surface);border:1px solid var(--line)}
.a-head{display:flex;flex-wrap:wrap;gap:.35rem .6rem;align-items:center}
.tests,.commits,.paths,.specs{list-style:none;padding:0;display:grid;gap:.2rem}
.tests li{color:var(--bad)}
.commits li,.paths li,.specs li{overflow-wrap:anywhere}
.paths li{color:var(--ink-2)}
.log{margin:.3rem 0 0;padding:.6rem;max-height:18rem;overflow:auto;background:var(--surface);
border:1px solid var(--line);font:12px/1.4 "IBM Plex Mono",ui-monospace,monospace;
white-space:pre-wrap;overflow-wrap:anywhere}
details>summary{cursor:pointer;color:var(--accent)}
details[open]>summary{margin-bottom:.35rem}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line)}
table{border-collapse:collapse;width:100%}
th,td{padding:.55rem .75rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
th{font:600 .66rem/1.2 var(--display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);white-space:nowrap}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td .mono,td.mono{white-space:nowrap;overflow-wrap:normal}
td:last-child{min-width:14rem;overflow-wrap:anywhere}
.grid td,.grid th{padding:.4rem .6rem;vertical-align:middle}
.grid td:last-child{min-width:12rem}
@media (max-width:600px){.grid .when,.grid .dur{display:none}.grid td:last-child{min-width:0}.grid td,.grid th{padding:.35rem .4rem;font-size:.78rem}
.grid .chip{font-size:.6rem;padding:.2rem .3rem}}
.flows td:first-child{min-width:13rem}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,20rem),1fr));gap:1.4rem}
.cols>div{min-width:0;display:grid;gap:.5rem;align-content:start}
.finding{display:grid;gap:.6rem;min-width:0;padding-top:.8rem;border-top:2px solid var(--line-2)}
.f-head{display:flex;flex-wrap:wrap;gap:.35rem .6rem;align-items:center}
.cols3{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,15rem),1fr));gap:.8rem}
.cols3>div{min-width:0}
.steps{padding-left:1.2rem}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(10.5rem,1fr));gap:.6rem}
.shot{display:grid;gap:.25rem;min-width:0;cursor:zoom-in}
.shot img{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top;
  border:1px solid var(--line-2)}
.shot figcaption{color:var(--ink-3);font-size:.72rem}
.flow-shots{grid-template-columns:repeat(auto-fill,minmax(8rem,10rem))}
.shot.open{grid-column:1/-1;cursor:zoom-out}
.shot.open img{aspect-ratio:auto;object-fit:contain}
footer{color:var(--ink-3);font-size:.8rem;overflow-wrap:anywhere}
"""

SCRIPT = """
document.addEventListener("click", function (event) {
  var shot = event.target.closest(".shot");
  if (shot) { shot.classList.toggle("open"); }
});
document.addEventListener("keydown", function (event) {
  var target = event.target;
  if ((event.key === "Enter" || event.key === " ") && target.classList.contains("shot")) {
    event.preventDefault();
    target.classList.toggle("open");
  }
});
"""

PAGE = Template(
    """<title>$title</title>
<link rel="stylesheet" href="$fonts">
<style>$css</style>
<main class="page">
$body
</main>
<script>$script</script>
"""
)


def request_configs(cycle: Cycle) -> list[dict]:
    """Returns the stored configuration of every run of the cycle that still has a request."""
    return [
        queue.load_request(run_id).config
        for run_id in cycle.runs
        if paths.request_file(run_id).exists()
    ]


def render(cycle: Cycle, attempts: list[Attempt]) -> str:
    """Builds the page of a cycle from its attempts: an artifact fragment starting at the title."""
    global SECRETS
    SECRETS = redact.secrets_of(request_configs(cycle))
    merged = merge_findings(attempts)
    budget = Budget(IMAGE_BUDGET)
    sections = [
        _header(cycle, attempts),
        _closure(cycle, attempts),
        _acs_section(cycle, attempts),
        _attempts_grid(cycle, attempts),
        _stage_line(cycle, attempts),
        _pruebas(attempts, cycle.id, budget),
        _findings(merged, attempts, cycle.id, budget),
        improved_section(merged, attempts),
        _mejoras(merged, cycle.id, budget),
        _logs(attempts),
        f'<footer>Generated by qg on {esc(fmt_time(now_iso()))} · cycle <span class="mono">'
        f"{esc(cycle.id)}</span> · {len(attempts)} attempt(s)</footer>",
    ]
    return PAGE.substitute(
        title=esc(cycle.feature), fonts=esc(FONTS), css=CSS, body="\n".join(sections), script=SCRIPT
    )


def write(cycle_id: str) -> Path:
    """Rebuilds the cycle's page.html and status from its attempts; returns the page path."""
    cycle = cycles.load(cycle_id)
    attempts = load_attempts(cycle)
    if not attempts:
        die(f"cycle {cycle_id} has no finished run yet; there is nothing to publish")
    atomic_write(paths.page_file(cycle_id), render(cycle, attempts))
    with queue.locked():
        cycles.set_acs(cycle_id, [item.id for item in phase_criteria(cycle, attempts)])
        active = any(
            queue.load(run_id).status in ("queued", "running")
            for run_id in cycle.runs
            if paths.state_file(run_id).exists()
        )
        cycles.set_status(cycle_id, "ready" if attempts[-1].ready and not active else "open")
    return paths.page_file(cycle_id)


def refresh(run_id: str) -> None:
    """Records the finished run as an attempt of its cycle, then rebuilds the cycle's page."""
    state = queue.load(run_id)
    record(state, queue.load_request(run_id))
    write(state.cycle)


def refresh_tree(run_id: str) -> None:
    """Rewrites the report and cycle page of every executed trusted run of the run's tree.

    Called when an operator's decision changes what the tree's verdict is judged on.
    """
    state = queue.load(run_id)
    for run in report.tree_runs(state.repo, state.tree):
        executed = run.status != "superseded" and any(s.started for s in run.stages.values())
        if executed and cycles.exists(run.cycle):
            report.refresh(run.id)
            refresh(run.id)
