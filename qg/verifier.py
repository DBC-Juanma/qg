"""The P0 verifier: an independent, read-only agent that tries to refute every open P0 of a run.

A P0 it refutes is re-graded to the severity it gives, with the category and the evidence kept in
the finding's `verification`; a P0 it confirms, or cannot judge, stays a P0 and keeps blocking.
"""

import json
import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from qg import config, git, jail, paths, proc, qa, report, settings
from qg.report import (
    ACCEPTED,
    CONFIRMED,
    PATCHED,
    RED,
    REFUTED,
    REJECTED,
    UNJUDGED,
    Finding,
    Verification,
)
from qg.sandbox import base_env
from qg.util import atomic_write, now_iso, read_tail

PROMPT_FILE = Path(__file__).with_name("verify_prompt.md")
TOOLS = "Read,Grep,Glob"
MAX_TURNS = 80
MAX_COST_USD = 5.0
CATEGORIES = ("harness", "designed", "duplicate", "unreachable", "minor", "unsupported")
REGRADES = ("P1", "P2", "P3")
PROBE_TAIL = 8000
EXAMPLES = 3

Wrap = Callable[[list[str]], list[str]]


class VerifyError(Exception):
    """The verifier's answer cannot be read; the text says why."""


@dataclass(frozen=True)
class Answer:
    """The verifier's judgement of one finding: verdict, evidence and, when refuted, the regrade.

    `probe` is its review of the finding's red probe, accepted or rejected with `probe_reason`,
    empty when the finding has no red probe or the verifier left it unreviewed; `flip` and
    `flip_reason` are the same for its green flip.
    """

    verdict: str
    reason: str
    category: str = ""
    severity: str = "P0"
    probe: str = ""
    probe_reason: str = ""
    flip: str = ""
    flip_reason: str = ""


def _unreviewed(finding: Finding) -> bool:
    """Tells whether a blocking P0 has a red probe or a patched flip no verifier reviewed."""
    probe = finding.probe
    if probe is None or probe.status != RED or not report.blocking(finding):
        return False
    return not probe.review or (probe.flip in PATCHED and not probe.flip_review)


def candidates(findings: list[Finding]) -> list[Finding]:
    """Returns the open P0 findings no verifier has judged, or that one could not judge, and
    those judged before their probe was made, so it gets reviewed."""
    return [f for f in findings if f.severity == "P0" and (report.unverified(f) or _unreviewed(f))]


def render_prompt(src: Path) -> str:
    """Returns the verifier's prompt for a checkout of the verified commit at `src`."""
    return PROMPT_FILE.read_text().format(src=src)


def _evidence_file(name: str, run_id: str) -> Path | None:
    """Returns the real file an evidence name names when it lies in the run's evidence directory.

    Anything else (another host file, another run's files, a link out, a directory) is None and is
    never shown to the verifier.
    """
    path = Path(name) if Path(name).is_absolute() else paths.run_dir(run_id) / name
    real = Path(os.path.realpath(path))
    base = Path(os.path.realpath(paths.evidence_dir(run_id)))
    return real if real.is_file() and real.is_relative_to(base) else None


def _probe_card(finding: Finding, evidence: Path) -> dict[str, Any] | None:
    """Returns what the verifier reads of a finding's red probe, copying its file and output.

    None when the finding has no red probe, or its files are gone from the probe's run.
    """
    probe = finding.probe
    if probe is None or probe.status != RED or not probe.path:
        return None
    home = paths.probe_dir(probe.run_id, finding.id)
    test, output = home / Path(probe.path).name, paths.probe_log(probe.run_id, finding.id)
    if not test.is_file() or not output.is_file():
        return None
    shutil.copyfile(test, evidence / f"{finding.id}-probe.py")
    atomic_write(evidence / f"{finding.id}-probe-run.log", read_tail(output)[-PROBE_TAIL:])
    body = {
        "path": probe.path,
        "file": f"evidence/{finding.id}-probe.py",
        "output": f"evidence/{finding.id}-probe-run.log",
        "failed": probe.reason,
    }
    patch = paths.flip_patch(probe.run_id, finding.id)
    if probe.flip not in PATCHED or not patch.is_file():
        return body
    shutil.copyfile(patch, evidence / f"{finding.id}-flip.patch")
    flip = {"patch": f"evidence/{finding.id}-flip.patch", "status": probe.flip}
    return {**body, "flip": {**flip, "result": probe.flip_reason}}


def card(finding: Finding, run_id: str, evidence: Path) -> dict[str, Any]:
    """Returns what an agent reads of a finding, copying the files it cites into `evidence`.

    A finding with a red probe also carries `probe`: the test file, its output and the failure.
    """
    cited = []
    for name in finding.evidence:
        path = _evidence_file(name, run_id)
        if path is not None:
            target = evidence / f"{finding.id}-{path.name}"
            shutil.copyfile(path, target)
            cited.append(f"evidence/{target.name}")
    body = {
        "id": finding.id,
        "title": finding.title,
        "area": finding.area,
        "flow": finding.flow,
        "steps": finding.steps,
        "expected": finding.expected,
        "actual": finding.actual,
        "evidence": cited,
    }
    probe = _probe_card(finding, evidence)
    return {**body, "probe": probe} if probe else body


def record_operator(run_id: str, finding: Finding, probe: str, flip: str, why: str) -> Path:
    """Keeps the operator's review of a P0's red probe and its flip, with their files, as an
    example for later verifiers, and applies it to the finding over the verifier's own review.

    `probe` and `flip` are accepted, rejected or empty (not judged). Returns the example's
    directory.
    """
    made = finding.probe
    assert made is not None and made.status == RED
    target = paths.calibration_dir(run_id, finding.id)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    _probe_card(finding, target)
    review = {
        "finding": {"id": finding.id, "title": finding.title, "actual": finding.actual},
        "probe": {
            "failed": made.reason,
            "verifier": made.review,
            "verifier_reason": made.review_reason,
        },
        "flip": {
            "status": made.flip,
            "result": made.flip_reason,
            "verifier": made.flip_review,
            "verifier_reason": made.flip_review_reason,
        },
        "operator": {"probe": probe, "flip": flip, "why": why, "when": now_iso()},
    }
    atomic_write(target / "review.json", json.dumps(review, indent=2, ensure_ascii=False) + "\n")
    note = f"operador: {why}"
    if probe:
        made = replace(made, review=probe, review_reason=note)
    if flip:
        made = replace(made, flip_review=flip, flip_review_reason=note)
    findings = report.load_findings(run_id)
    report.save_findings(run_id, report.with_probe(findings, finding.id, made))
    return target


def examples() -> list[Path]:
    """Returns up to EXAMPLES operator reviews for the verifier, those that overruled a verifier
    first, then the newest."""

    def rank(review: dict[str, Any]) -> tuple[bool, str]:
        said = review["operator"]
        overruled = (said["probe"] and said["probe"] != review["probe"]["verifier"]) or (
            said["flip"] and said["flip"] != review["flip"]["verifier"]
        )
        return bool(overruled), said["when"]

    if not paths.CALIBRATION.is_dir():
        return []
    found = [
        (rank(json.loads((entry / "review.json").read_text())), entry)
        for entry in paths.CALIBRATION.iterdir()
        if (entry / "review.json").is_file()
    ]
    return [entry for _, entry in sorted(found, reverse=True)[:EXAMPLES]]


def workspace(run_id: str, targets: list[Finding], plan: str) -> Path:
    """Writes the verifier's working directory: p0.json, plan.md, charter.md and the evidence."""
    root = paths.run_dir(run_id) / "verify"
    shutil.rmtree(root, ignore_errors=True)
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    cards = [card(f, run_id, evidence) for f in targets]
    atomic_write(root / "p0.json", json.dumps(cards, indent=2, ensure_ascii=False) + "\n")
    atomic_write(root / "plan.md", plan or "No plan file was given to this run.\n")
    charter = paths.qa_dir(run_id) / "charter.md"
    text = charter.read_text(errors="replace") if charter.is_file() else "No charter was written.\n"
    atomic_write(root / "charter.md", text)
    atomic_write(root / "mcp.json", '{"mcpServers": {}}\n')
    if any("probe" in item for item in cards):
        for entry in examples():
            shutil.copytree(entry, root / "examples" / entry.name)
    return root


def _review(entry: dict[str, Any], name: str) -> tuple[str, str]:
    """Returns one entry's review `name` (probe or flip): accepted or rejected with its reason,
    else empty."""
    review = entry.get(name)
    reason = str(entry.get(f"{name}_reason", "")).strip()
    if review in (ACCEPTED, REJECTED) and reason:
        return str(review), reason
    return "", ""


def _answer(entry: dict[str, Any], probed: bool) -> Answer | str:
    """Returns the Answer one entry of the verifier's array gives, or why it cannot be used.

    For a finding with a red probe (`probed`) the probe review is read too; a missing or invalid
    one leaves the probe unreviewed, never the verdict unusable.
    """
    reason = str(entry.get("reason", "")).strip()
    refs = entry.get("refs")
    cited = [str(r).strip() for r in refs if str(r).strip()] if isinstance(refs, list) else []
    if not reason:
        return "the verifier gave no reason"
    text = reason + (f" ({', '.join(cited)})" if cited else "")
    review, review_reason = _review(entry, "probe") if probed else ("", "")
    flip, flip_reason = _review(entry, "flip") if probed else ("", "")
    verdict = entry.get("verdict")
    if verdict == CONFIRMED:
        return Answer(CONFIRMED, text, "", "P0", review, review_reason, flip, flip_reason)
    if verdict != REFUTED:
        return f"the verifier's verdict {verdict!r} is neither confirmed nor refuted"
    category = entry.get("category")
    if category not in CATEGORIES:
        return f"the verifier's category {category!r} is not one of {', '.join(CATEGORIES)}"
    severity = entry.get("severity")
    if severity not in REGRADES:
        return f"the verifier's severity {severity!r} is not one of {', '.join(REGRADES)}"
    return Answer(
        REFUTED, text, str(category), str(severity), review, review_reason, flip, flip_reason
    )


def parse_answer(
    text: str, ids: list[str], probed: frozenset[str] = frozenset()
) -> dict[str, Answer | str]:
    """Returns, for every id, its Answer or why it has none; raises VerifyError on no JSON array.

    `probed` names the findings with a red probe, whose entries also review it.

    The array is read from the first `[` to the last `]` of the verifier's final message, so a
    code fence around it does not matter.
    """
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise VerifyError("the verifier's final message holds no JSON array")
    try:
        raw = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise VerifyError(f"the verifier's final message is not valid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise VerifyError("the verifier's final message is not a JSON array")
    found: dict[str, Answer | str] = {}
    for entry in raw:
        if isinstance(entry, dict) and entry.get("id") in ids and entry["id"] not in found:
            found[entry["id"]] = _answer(entry, entry["id"] in probed)
    return {fid: found.get(fid, "the verifier gave no answer for it") for fid in ids}


def _reviewed(finding: Finding, answer: Answer) -> Finding:
    """Returns the finding with the verifier's review of its probe and flip, when it gave one."""
    probe = finding.probe
    if probe is None:
        return finding
    if answer.probe:
        probe = replace(probe, review=answer.probe, review_reason=answer.probe_reason)
    if answer.flip and probe.flip in PATCHED:
        probe = replace(probe, flip_review=answer.flip, flip_review_reason=answer.flip_reason)
    return replace(finding, probe=probe)


def apply(findings: list[Finding], answers: dict[str, Answer | str], when: str) -> list[Finding]:
    """Returns the findings with every answered P0 confirmed, re-graded, or marked unjudged,
    and every reviewed probe accepted or rejected.

    A P0 judged before its probe was made keeps its verdict and severity: only the review of its
    probe and flip is taken from the answer.
    """
    judged = []
    for finding in findings:
        answer = answers.get(finding.id)
        if answer is None:
            judged.append(finding)
        elif not report.unverified(finding):
            judged.append(_reviewed(finding, answer) if isinstance(answer, Answer) else finding)
        elif isinstance(answer, str):
            seal = Verification(UNJUDGED, answer, "", finding.severity, when)
            judged.append(replace(finding, verification=seal))
        elif answer.verdict == CONFIRMED:
            seal = Verification(CONFIRMED, answer.reason, "", finding.severity, when)
            judged.append(replace(_reviewed(finding, answer), verification=seal))
        else:
            seal = Verification(REFUTED, answer.reason, answer.category, finding.severity, when)
            regraded = replace(finding, severity=answer.severity, verification=seal)
            judged.append(_reviewed(regraded, answer))
    return judged


def tally(answers: dict[str, Answer | str], reviews: dict[str, Answer | str]) -> str:
    """Returns the stage-summary note: how many P0 the verifier confirmed, refuted and left in
    `answers`, and how many probes and flips it accepted and rejected in `reviews`."""
    reviewed = [a for a in reviews.values() if isinstance(a, Answer)]
    confirmed = sum(1 for a in answers.values() if isinstance(a, Answer) and a.verdict == CONFIRMED)
    refuted = Counter(
        a.category for a in answers.values() if isinstance(a, Answer) and a.verdict == REFUTED
    )
    left = sum(1 for a in answers.values() if isinstance(a, str))
    parts = [f"{confirmed} P0 confirmed"]
    for name in ("probe", "flip"):
        counts = Counter(getattr(a, name) for a in reviewed if getattr(a, name))
        if counts:
            parts.append(f"{name}s accepted {counts[ACCEPTED]}, rejected {counts[REJECTED]}")
    if refuted:
        kinds = ", ".join(f"{name} {count}" for name, count in sorted(refuted.items()))
        parts.append(f"{sum(refuted.values())} refuted ({kinds})")
    if left:
        parts.append(f"{left} without a verdict, still blocking")
    return "verifier: " + "; ".join(parts)


def _command(claude: Path, prompt: str, mcp: Path, src: Path) -> list[str]:
    """Returns the headless Claude Code command of the verifier: read and search tools only.

    `src` is added as a working directory: Claude Code reads nothing outside its own otherwise.
    """
    return [
        str(claude),
        "-p",
        prompt,
        "--add-dir",
        str(src),
        "--model",
        "opus",
        "--max-turns",
        str(MAX_TURNS),
        "--max-budget-usd",
        f"{MAX_COST_USD:g}",
        "--output-format",
        "json",
        "--restricted",
        "--strict-mcp-config",
        f"--mcp-config={mcp}",
        f"--tools={TOOLS}",
        f"--allowedTools={TOOLS}",
    ]


def _ask(
    run_id: str, src: Path, root: Path, ids: list[str], probed: frozenset[str], wrap: Wrap
) -> dict[str, Answer | str]:
    """Runs the confined verifier on `root` with `src` readable; returns its answer for each id."""
    tools = settings.load()
    claude = jail.claude_binary()
    home = paths.run_dir(run_id) / "verify-home"
    logs = paths.logs_dir(run_id)
    logs.mkdir(exist_ok=True)
    paths.NPM.mkdir(parents=True, exist_ok=True)
    output = logs / "verify-output.json"
    errors = logs / "verify-agent.log"
    budget = config.VERIFY_MINUTES * 60.0

    def renew(seconds: float) -> None:
        """Renews the live login outside the jail until it covers `seconds`."""
        jail.ensure_login(claude, base_env(), seconds)

    valid_for = budget + jail.LOGIN_MARGIN_SECONDS
    renew(valid_for)
    jail.prepare_home(home, valid_for)
    command = wrap(
        jail.command(
            _command(claude, render_prompt(src), root / "mcp.json", src),
            workdir=root,
            writable=[],
            workdir_writable=False,
            home=home,
            tools=tools,
            claude=claude,
            npm_cache=paths.NPM,
            readable=(src,),
        )
    )
    try:
        run = qa.retry_on_usage_limit(
            lambda seconds, _resumed: qa.run_agent(
                command, cwd=root, env=base_env(), out=output, err=errors, timeout=seconds
            ),
            home=home,
            budget=budget,
            out=output,
            err=errors,
            renew=renew,
        )
    finally:
        jail.discard_home(home)
    result = qa.read_result(output)
    if run.timed_out:
        why = f"the verifier timed out after {config.VERIFY_MINUTES} min"
    elif result is None:
        why = f"the verifier left no readable result (exit {run.code})"
    elif run.code != 0 or result.get("is_error"):
        said = str(result.get("result", "")).strip().splitlines()
        subtype = result.get("subtype", "")
        why = f"the verifier failed (exit {run.code}, {subtype})" + (f": {said[0]}" if said else "")
    else:
        try:
            return parse_answer(str(result.get("result", "")), ids, probed)
        except VerifyError as exc:
            why = str(exc)
    return {fid: why for fid in ids}


def verify_run(run_id: str, src: Path, plan: str, wrap: Wrap) -> str:
    """Judges every open P0 of the run no verifier has judged and saves the findings.

    `src` is a checkout of the run's commit, `plan` the plan's text. Returns the note for the QA
    stage's summary, empty when there was no P0 to judge. A verifier that cannot start leaves
    every P0 unjudged, still blocking, with the reason.
    """
    findings = report.load_findings(run_id)
    targets = candidates(findings)
    if not targets:
        return ""
    ids = [f.id for f in targets]
    root = workspace(run_id, targets, plan)
    cards = json.loads((root / "p0.json").read_text())
    probed = frozenset(item["id"] for item in cards if "probe" in item)
    try:
        answers = _ask(run_id, src, root, ids, probed, wrap)
    except (jail.JailError, proc.AccountFailure, settings.SettingsError) as exc:
        answers = {fid: f"the verifier could not start: {exc}" for fid in ids}
    report.save_findings(run_id, apply(findings, answers, now_iso()))
    fresh = {f.id for f in targets if report.unverified(f)}
    atomic_write(
        root / "answers.json",
        json.dumps(
            {k: asdict(v) if isinstance(v, Answer) else v for k, v in answers.items()},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    return tally({fid: answer for fid, answer in answers.items() if fid in fresh}, answers)


@contextmanager
def checkout(repo: str, sha: str, target: Path) -> Iterator[Path]:
    """Yields a detached worktree of `sha` at `target`, removed again afterwards."""
    shutil.rmtree(target, ignore_errors=True)
    git.out(repo, "worktree", "add", "--detach", str(target), sha)
    try:
        yield target
    finally:
        git.out(repo, "worktree", "remove", "--force", str(target))
