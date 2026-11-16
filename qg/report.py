"""Findings, verdicts, report.json and report.md of a run, and the static board of every branch."""

import html
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from qg import config, cycles, paths, phase_scope, queue, redact, release
from qg.config import Policy
from qg.queue import Request, RunState, StageState
from qg.util import atomic_write, die, elapsed, human, now_iso

SEVERITIES = ("P0", "P1", "P2", "P3")
FINDING_STATUSES = ("open", "fixed")
CONFIRMED = "confirmed"
REFUTED = "refuted"
UNJUDGED = "error"
VERDICTS = (CONFIRMED, REFUTED, UNJUDGED)
RED = "red"
PROBE_ERROR = "error"
NO_PROBE = "none"
PROBE_STATUSES = (RED, PROBE_ERROR, NO_PROBE)
ACCEPTED = "accepted"
REJECTED = "rejected"
REVIEWS = ("", ACCEPTED, REJECTED)
GREEN = "green"
CONTRADICTS = "contradicts"
FLIP_FAILED = "failed"
SPECIAL_CASE = "special-case"
FLIP_STATUSES = ("", GREEN, CONTRADICTS, FLIP_FAILED, SPECIAL_CASE)
VARIANT_STATUSES = ("", RED, PROBE_ERROR)
PATCHED = (GREEN, CONTRADICTS)
READY = "READY"
NOT_READY = "NOT READY"
SHOWN_BLOCKERS = 5
FINDINGS_RULE = (
    "P0 and P1 block: fix them and they are re-verified by the next run of the cycle "
    "(qg retest {run}). P2 and P3 never block and need no run: fix them in the working tree, "
    "commit, and close each with qg fixed {run} <finding> --commit <sha> --note <text>; "
    "no later QA pass re-checks them."
)
UNREVIEWED_SHOWN = 40


@dataclass
class Ack:
    """The operator's acknowledgement of a P1 finding: a note, it gates nothing."""

    who: str
    when: str
    note: str


@dataclass
class Verification:
    """The independent verifier's judgement of a P0: confirmed, refuted (why, and the severity it
    was given instead of `filed`) or error (the verifier could not judge it)."""

    verdict: str
    reason: str
    category: str = ""
    filed: str = "P0"
    when: str = ""


@dataclass
class Reproduction:
    """The pytest probe of a P0: a test the probe agent wrote and qg ran against the run's commit.

    `status` is red (exactly one test failed, nothing errored), error (it never failed cleanly)
    or none (no probe was made); `reason` says what happened. `run_id` is the run whose
    probes/<finding id>/ directory holds the file and its output, `path` where the file sits
    in the repository. `review` is the verifier's judgement of the probe: accepted, rejected,
    or empty while it has not judged it.

    `flip` is the minimal fix of a red probe: green (the probe and the affected tests passed with
    it), contradicts (the probe passed and only existing tests failed, which may assert the defect
    itself), failed (`flip_reason` says why) or empty when none was tried. A green or contradicts
    flip's patch is flip.patch in the probe's directory; `flip_review` is the verifier's
    judgement of it. A flip that turned the probe green but not the held-out variant, a second
    probe of the same defect the flip agent never saw, is special-case: it fixes the probe's
    data rather than the defect.

    `variant` is that held-out probe, written once a flip was green: red, error (no clean red
    variant, `variant_reason` says why) or empty when none was tried.
    """

    status: str
    reason: str
    run_id: str
    path: str = ""
    review: str = ""
    review_reason: str = ""
    when: str = ""
    flip: str = ""
    flip_reason: str = ""
    flip_review: str = ""
    flip_review_reason: str = ""
    variant: str = ""
    variant_reason: str = ""


@dataclass
class Dismissal:
    """The operator's decision that a finding of any severity is a false positive."""

    who: str
    when: str
    note: str


@dataclass
class Closure:
    """The working session's record that a P2 or P3 finding was fixed without a run: the commit
    that fixed it and a note."""

    who: str
    when: str
    commit: str
    note: str


@dataclass
class Finding:
    """A problem the QA stage found; an open P0 blocks every deploy, and so does an open P1
    until the operator acknowledges it (`qg ack`).

    A P0 the verifier refuted is re-graded (its `verification` keeps what was filed and why).
    A P2 or P3 never blocks: the session fixes it and closes it without a run (`qg fixed`),
    which sets `status` fixed and `closed`.
    """

    id: str
    severity: str
    title: str
    area: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    evidence: list[str] = field(default_factory=list)
    status: str = "open"
    acknowledged: Ack | None = None
    dismissed: Dismissal | None = None
    flow: str = ""
    verification: Verification | None = None
    probe: Reproduction | None = None
    closed: Closure | None = None


@dataclass(frozen=True)
class Located:
    """A finding together with the run whose findings.json holds it and that run's cycle."""

    run_id: str
    cycle: str
    finding: Finding


def finding_from_dict(raw: dict[str, Any]) -> Finding:
    """Rebuilds a Finding from its JSON form, exiting on an unknown severity or status."""
    ack = raw.get("acknowledged")
    dismissal = raw.get("dismissed")
    verification = raw.get("verification")
    probe = raw.get("probe")
    closed = raw.get("closed")
    finding = Finding(
        **{
            **raw,
            "acknowledged": Ack(**ack) if ack else None,
            "dismissed": Dismissal(**dismissal) if dismissal else None,
            "verification": Verification(**verification) if verification else None,
            "probe": Reproduction(**probe) if probe else None,
            "closed": Closure(**closed) if closed else None,
        }
    )
    if finding.verification and finding.verification.verdict not in VERDICTS:
        verdict = finding.verification.verdict
        die(f"finding {finding.id}: verification {verdict!r} is not one of {VERDICTS}")
    made = finding.probe
    checks = (
        [
            ("probe", made.status, PROBE_STATUSES),
            ("probe review", made.review, REVIEWS),
            ("flip", made.flip, FLIP_STATUSES),
            ("flip review", made.flip_review, REVIEWS),
            ("variant", made.variant, VARIANT_STATUSES),
        ]
        if made
        else []
    )
    for label, value, allowed in checks:
        if value not in allowed:
            die(f"finding {finding.id}: {label} {value!r} is not one of {allowed}")
    if finding.severity not in SEVERITIES:
        die(f"finding {finding.id}: severity {finding.severity!r} is not one of {SEVERITIES}")
    if finding.status not in FINDING_STATUSES:
        die(f"finding {finding.id}: status {finding.status!r} is not one of {FINDING_STATUSES}")
    return finding


def load_findings(run_id: str) -> list[Finding]:
    """Reads the run's findings.json; a run without the file has no findings."""
    path = paths.findings_file(run_id)
    if not path.exists():
        return []
    return [finding_from_dict(item) for item in json.loads(path.read_text())["findings"]]


def with_probe(findings: list[Finding], finding_id: str, made: Reproduction) -> list[Finding]:
    """Returns `findings` with `made` as the probe of finding `finding_id`."""
    return [replace(f, probe=made) if f.id == finding_id else f for f in findings]


def save_findings(run_id: str, findings: list[Finding]) -> None:
    """Writes the run's findings.json."""
    body = {"findings": [asdict(f) for f in findings]}
    atomic_write(paths.findings_file(run_id), json.dumps(body, indent=2) + "\n")


def blocking(finding: Finding) -> bool:
    """Tells whether a finding stops a deploy: an open, undismissed P0, or an open, undismissed P1
    the operator has not acknowledged (`qg ack`). A run with such a finding is never READY.

    A P0 the verifier refuted carries the severity the verifier gave it instead; a P0 it confirmed
    or could not judge is still a P0 and still blocks.
    """
    if finding.status != "open" or finding.dismissed is not None:
        return False
    return finding.severity == "P0" or (finding.severity == "P1" and finding.acknowledged is None)


def unverified(finding: Finding) -> bool:
    """Tells whether a finding is a blocking P0 that no verifier has confirmed yet."""
    return (
        blocking(finding)
        and finding.severity == "P0"
        and (finding.verification is None or finding.verification.verdict == UNJUDGED)
    )


def reproduced(finding: Finding) -> bool:
    """Tells whether a finding has a red probe the verifier accepted: a reproduction to rerun."""
    probe = finding.probe
    return probe is not None and probe.status == RED and probe.review == ACCEPTED


def open_findings(findings: list[Finding]) -> list[Finding]:
    """Returns the findings that block: every open, undismissed P0 and unacknowledged P1."""
    return [f for f in findings if blocking(f)]


def _live(findings: list[Located]) -> list[Located]:
    """Returns the open, undismissed findings, one record per finding, a finding being cycle and id.

    A run that carries an earlier finding forward holds a copy of it; the newest run's record
    decides, so a fix, a dismissal or a verification recorded later takes effect.
    """
    latest: dict[tuple[str, str], Located] = {}
    for item in sorted(findings, key=lambda item: item.run_id):
        latest[(item.cycle, item.finding.id)] = item
    return [
        item
        for item in latest.values()
        if item.finding.status == "open" and item.finding.dismissed is None
    ]


def _named(items: list[Located]) -> str:
    """Lists up to five findings as `id (run id)` and says how many more there are.

    A P0 no verifier confirmed reads `id (run id, unverified)`.
    """
    shown = ", ".join(
        f"{item.finding.id} (run {item.run_id}"
        + (", unverified)" if unverified(item.finding) else ")")
        for item in items[:SHOWN_BLOCKERS]
    )
    extra = len(items) - SHOWN_BLOCKERS
    return shown + (f" and {extra} more" if extra > 0 else "")


ADVISORY_QA = ("failed", "skipped")


def evaluate(policy: Policy, stages: dict[str, StageState], findings: list[Located]) -> list[str]:
    """Returns why a run misses `policy`, judged on stage statuses and findings; empty is READY.

    A QA stage that ended failed or skipped is advisory, a warning (see `warnings_of`); one that
    has not finished still blocks. `findings` is the union over every trusted run of the tree,
    the newest record of each finding deciding (see `_live`); an open P0, and an open P1 the
    operator has not acknowledged, block.
    """
    reasons = []
    for name in policy.stages:
        stage = stages.get(name)
        if stage is None:
            reasons.append(f"{name}: not part of this run")
        elif stage.status != "passed" and not (name == "qa" and stage.status in ADVISORY_QA):
            detail = f" ({stage.summary})" if stage.summary else ""
            reasons.append(f"{name}: {stage.status}{detail}")
    live = [i for i in _live(findings) if blocking(i.finding)]
    p0 = [i for i in live if i.finding.severity == "P0"]
    p1 = [i for i in live if i.finding.severity == "P1"]
    if len(p0) > policy.max_p0:
        allowed = f"at most {policy.max_p0} allowed"
        reasons.append(f"{len(p0)} open P0 finding(s), {allowed}: {_named(p0)}")
    if p1:
        reasons.append(
            f"{len(p1)} open P1 finding(s) not acknowledged by the operator: {_named(p1)}"
        )
    return reasons


SHA = re.compile(r"^[0-9a-f]{40}$")


def trusted(run: RunState) -> bool:
    """Tells whether a run has the hardened runner's provenance fields; legacy runs never do."""
    return bool(
        SHA.match(run.config_sha)
        and run.config_ref
        and SHA.match(run.tree)
        and SHA.match(run.sha)
        and os.path.isabs(run.repo)
        and os.path.realpath(run.repo) == run.repo
    )


def reuse_statuses(name: str) -> tuple[str, ...]:
    """Returns the results of a source stage that may be reused: static, tests and Playwright
    lend a failure as well as a pass (same tree, same result); S0 and QA lend a pass only."""
    return ("passed",) if name in ("simplify", "qa") else ("passed", "failed")


def _reuse_problem(state: RunState, name: str, source_id: str) -> list[str]:
    """Returns why stage `name`, reused from `source_id`, does not stand; empty when it does."""
    where = f"{name}: reused from run {source_id}"
    if source_id >= state.id or not paths.state_file(source_id).exists():
        return [f"{where}, which is not an earlier run on disk"]
    try:
        source = queue.load(source_id)
    except (ValueError, KeyError, TypeError):
        return [f"{where}, whose state cannot be read"]
    held = source.stages.get(name)
    if source.status not in queue.FINISHED or not trusted(source) or source.repo != state.repo:
        return [f"{where}, which is not a trusted finished run of this repository"]
    if held is None or held.status not in reuse_statuses(name):
        return [f"{where}, whose own {name} did not finish"]
    if held.status == "failed" and source.status != "failed":
        return [f"{where}, a failure of a run that was {source.status}"]
    if name == "simplify":
        same = (
            source.requested_tree == state.requested_tree
            and source.tree == state.tree
            and source.sha == state.sha
            and source.simplified_ref == state.simplified_ref
        )
    else:
        same = source.tree == state.tree
    if not same and name == "qa" and state.mode == "phase":
        return [f"{where}, {why}" for why in _carried_problems(state, source)]
    if not same:
        return [f"{where}, which verified another tree"]
    return reuse_problems(source)


def blocking_open(findings: list[Finding]) -> list[Finding]:
    """Returns the open, undismissed P0 and P1 findings, acknowledged or not."""
    return [
        f
        for f in findings
        if f.status == "open" and f.dismissed is None and f.severity in ("P0", "P1")
    ]


def _carried_problems(state: RunState, source: RunState) -> list[str]:
    """Returns why the QA of an earlier commit's phase run does not stand for this phase run."""
    from qg import reuse

    try:
        request = queue.load_request(state.id)
        cfg = config.parse(request.config, f"request of run {state.id}")
    except (OSError, ValueError, TypeError, SystemExit):
        return ["its request cannot be read"]
    return reuse.qa_problems(source, request, cfg, phase_scope.explicit_plan(state.id))


def reuse_problems(state: RunState) -> list[str]:
    """Returns why the reused stages of a run do not stand: every source must be the same tree.

    A READY verdict built from reused stages needs each source to still exist, to be a trusted
    finished run of the repository, to have passed that very stage itself (or by a reuse that
    stands) and to have verified exactly the tree this run verified.
    """
    problems: list[str] = []
    for name, held in state.stages.items():
        if held.reused_from:
            problems += _reuse_problem(state, name, held.reused_from)
    return problems


def phase_problems(kid: RunState) -> list[str]:
    """Returns why a phase run's provenance fails: untrusted, or reused stages that do not stand."""
    found = [] if trusted(kid) else ["not a trusted run"]
    return [*found, *reuse_problems(kid)]


def judge(state: RunState, policy: Policy, findings: list[Located]) -> list[str]:
    """Returns why a run misses `policy`: its stages, its findings and the stages it reused.

    A release run is judged with the QA of all its phases and with whether its phase runs may
    stand for it (see `release.integrity`: a phase whose QA walked nothing blocks); any other run
    whose QA walked nothing is not READY either. A failed Playwright stage names who owns the
    failing tests (see `release.attribute`).
    """
    stages = release.effective_stages(state)
    reasons = release.attribute(state, stages, evaluate(policy, stages, findings))
    if state.mode == "release":
        reasons += release.integrity(state, release.children(state.id), phase_problems)
    elif "qa" in policy.stages and (why := release.unwalked(state)):
        reasons.append(f"qa: {why}")
    return [*reasons, *reuse_problems(state)]


def warnings_of(state: RunState) -> list[str]:
    """Returns what a finished run's verdict lets through but the operator must see.

    A QA stage that walked part of its plan and then failed (for a release, each such phase) is
    `QA incomplete`; one that walked nothing blocks instead (see `judge`). A Playwright test that
    failed and then passed every rerun is `Playwright flaky`. A verification that left out
    the QA its policy asks for says so first (`QA_LEFT_OUT`).
    """
    if state.status not in ("passed", "failed"):
        return []
    stages = release.effective_stages(state)
    found = [notice] if (notice := qa_notice(state)) else []
    if state.mode == "release":
        if incomplete := release.partial(state, release.children(state.id)):
            found.append(f"QA incomplete: {', '.join(incomplete)}")
    elif (qa := stages.get("qa")) is not None and qa.status in ADVISORY_QA:
        if release.walked(state):
            found.append(f"QA incomplete: {qa.summary or qa.status}")
    held = stages.get("playwright")
    flakes = held.flakes if held is not None else {}
    found += [
        f"Playwright flaky: {line.removeprefix('FAILED ')} ({label})"
        for line, label in flakes.items()
        if release.rerun_passed(label)
    ]
    return found


def tree_runs(repo: str, tree: str) -> list[RunState]:
    """Returns every trusted finished run of `tree`, newest first; the findings of all count."""
    found = [
        run
        for run in queue.list_runs()
        if run.repo == repo
        and run.tree == tree
        and run.mode != "nightly"
        and trusted(run)
        and run.status in queue.FINISHED
    ]
    return sorted(found, key=lambda run: run.id, reverse=True)


RUNNER_PREFIXES = ("L-", "G-")


def walked_a_flow(run_id: str) -> bool:
    """Tells whether the run's QA walked at least one flow; an aborted QA observed nothing."""
    path = paths.qa_file(run_id)
    return path.is_file() and bool(json.loads(path.read_text())["flows"])


def located(runs: list[RunState]) -> list[Located]:
    """Returns the findings of `runs`, oldest run first, with their run.

    A run whose QA walked no flow keeps only the runner's log and gap findings; the agent observed
    nothing.
    """
    ordered = sorted(runs, key=lambda run: run.id)
    return [
        Located(run.id, run.cycle, item)
        for run in ordered
        for item in load_findings(run.id)
        if item.id.startswith(RUNNER_PREFIXES) or walked_a_flow(run.id)
    ]


def tree_findings(state: RunState) -> list[Located]:
    """Returns every finding of every trusted run of the state's repo and tree, its own included."""
    runs = {run.id: run for run in tree_runs(state.repo, state.tree)}
    runs[state.id] = state
    return located(list(runs.values()))


def cycle_runs(state: RunState) -> list[RunState]:
    """Returns the trusted finished runs of the state's tree that belong to its cycle."""
    return [run for run in tree_runs(state.repo, state.tree) if run.cycle == state.cycle]


def qa_left_out(state: RunState, policy: Policy) -> bool:
    """Tells whether a verification run left out the QA stage that `policy` asks for: a run
    between phase closes, or one asked with --no-qa."""
    return state.mode == "verify" and "qa" in policy.stages and "qa" not in state.stage_set


QA_LEFT_OUT = (
    "QA not run: this run checked only its other stages ({stages}); the QA pass runs only at "
    "phase close (qg verify --prd <id> --phase <id>) or at release (--release). This verdict "
    "is not a deploy verdict: qg check needs a run with QA."
)


def qa_notice(state: RunState) -> str:
    """Returns the QA_LEFT_OUT notice of a run that left out the QA its env's policy asks for,
    empty for any other run."""
    if state.mode != "verify" or "qa" in state.stage_set:
        return ""
    cfg = config.parse(queue.load_request(state.id).config, f"request of run {state.id}")
    if not qa_left_out(state, cfg.policy(state.env)):
        return ""
    return QA_LEFT_OUT.format(stages=", ".join(state.stage_set))


def verdict_of(state: RunState, request: Request) -> tuple[str, list[str]]:
    """Returns READY or NOT READY for the run under its own env's policy, with the reasons.

    Stages are the run's own; findings are those of every trusted run of the same tree. A run
    that left QA out (`qa_left_out`) is judged on the stages it ran: it can be READY for them,
    its warnings say so, and `qg check` still judges it under the whole policy.
    """
    if state.status not in ("passed", "failed"):
        return NOT_READY, [f"run {state.status}" + (f": {state.error}" if state.error else "")]
    policy = config.stored_policy(request.config, state.env, f"request of run {state.id}")
    if state.mode == "nightly":
        policy = Policy(tuple(state.stage_set), policy.max_p0, policy.p1_acknowledged)
    elif qa_left_out(state, policy):
        kept = tuple(name for name in policy.stages if name != "qa")
        policy = Policy(kept, policy.max_p0, policy.p1_acknowledged)
    reasons = judge(state, policy, tree_findings(state))
    return (NOT_READY if reasons else READY), reasons


def display_verdict(state: RunState) -> str:
    """Returns the verdict of a finished run, or its status word when it has no verdict yet."""
    if state.status not in ("passed", "failed"):
        return state.status
    return verdict_of(state, queue.load_request(state.id))[0]


def finding_counts(state: RunState) -> tuple[int, int]:
    """Returns the open P0 count, which blocks, and the open P1 count, the to-do list, over the
    state's tree."""
    live = _live(tree_findings(state))
    return (
        sum(1 for i in live if i.finding.severity == "P0"),
        sum(1 for i in live if i.finding.severity == "P1"),
    )


def covering_run(repo: str, tree: str, stage_set: list[str], policy: Policy) -> RunState | None:
    """Returns the newest run of `tree` that passed every stage of `stage_set` within `policy`.

    The findings are those of every trusted run of the tree, not only the covering run's.
    """
    asked = Policy(tuple(stage_set), policy.max_p0, policy.p1_acknowledged)
    runs = tree_runs(repo, tree)
    known = located(runs)
    for run in runs:
        if (
            run.mode != "phase"
            and run.status in ("passed", "failed")
            and not judge(run, asked, known)
        ):
            return run
    return None


def best_run(repo: str, tree: str, policy: Policy) -> tuple[RunState | None, list[str]]:
    """Returns a run of `tree` satisfying `policy`, or the newest run with why it falls short.

    Stages are judged per run, findings over every trusted run of the tree.
    """
    runs = tree_runs(repo, tree)
    judged = [run for run in runs if run.mode != "phase" and run.status in ("passed", "failed")]
    if not judged:
        return None, [f"no finished verification run exists for tree {tree[:12]}"]
    known = located(runs)
    for run in judged:
        if not judge(run, policy, known):
            return run, []
    return judged[0], judge(judged[0], policy, known)


def status_word(finding: Finding) -> str:
    """Returns open, fixed, dismissed or acknowledged: the state the operator sees."""
    if finding.status == "fixed":
        return "fixed"
    if finding.dismissed is not None:
        return "dismissed"
    return "acknowledged" if finding.acknowledged is not None else "open"


def _disposal_note(finding: Finding) -> str:
    """Returns the note on a finding closed without a run, dismissed or acknowledged, with who
    and when."""
    if finding.closed is not None:
        c = finding.closed
        return f" (closed without a run by {c.who} at {c.when}, commit {c.commit[:12]}: {c.note})"
    if finding.dismissed is not None:
        d = finding.dismissed
        return f" (dismissed by {d.who} at {d.when}: {d.note})"
    if finding.acknowledged is not None:
        a = finding.acknowledged
        return f" (acknowledged by {a.who} at {a.when}: {a.note})"
    return ""


def verification_note(finding: Finding) -> str:
    """Returns what the verifier said of a finding, empty when it never judged it."""
    check = finding.verification
    if check is None:
        return ""
    if check.verdict == CONFIRMED:
        return f"verified: {check.reason}"
    if check.verdict == REFUTED:
        return f"verifier: {check.category} — {check.reason} (was {check.filed})"
    return f"unverified (the verifier failed: {check.reason})"


def probe_notes(finding: Finding) -> list[str]:
    """Returns what the probe and the flip of a finding showed, empty when no probe was tried."""
    probe = finding.probe
    if probe is None:
        return []
    if probe.status == NO_PROBE:
        return [f"not reproduced: {probe.reason}"]
    if probe.status == PROBE_ERROR:
        return [f"not reproduced, the probe did not fail cleanly: {probe.reason}"]
    if probe.review == ACCEPTED:
        notes = [f"reproduced by {probe.path}: {probe.review_reason}"]
    elif probe.review == REJECTED:
        notes = [f"not reproduced, the reviewer rejected the probe: {probe.review_reason}"]
    else:
        notes = [f"red probe not reviewed: {probe.path}"]
    if probe.flip == FLIP_FAILED:
        notes.append(f"no flip: {probe.flip_reason}")
    if probe.flip == SPECIAL_CASE:
        notes.append(f"flip that only fixes the probe's case: {probe.flip_reason}")
    if probe.flip not in PATCHED:
        return notes
    patch = paths.flip_patch(probe.run_id, finding.id)
    clash = f" ({probe.flip_reason})" if probe.flip == CONTRADICTS else ""
    lead = {
        ACCEPTED: f"flip proposed in {patch}: {probe.flip_review_reason}",
        REJECTED: f"flip rejected by the reviewer ({patch}): {probe.flip_review_reason}",
        "": f"flip not reviewed: {patch}",
    }
    return [*notes, lead[probe.flip_review] + clash]


def _cell(text: str) -> str:
    """Makes `text` safe inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _simplified(state: RunState) -> list[str]:
    """Returns report.md's lines on S0: each change with why, the diff, what it left alone."""
    sdir = paths.simplify_dir(state.id)
    said = sdir / "explained.json"
    explained = json.loads(said.read_text()) if said.is_file() else {}
    applied = sdir / "applied.diff"
    lines = []
    if state.simplified_ref and applied.is_file():
        lines += ["", "## What S0 simplify changed", ""]
        lines += [f"- {change}" for change in explained.get("changes", [])]
        lines += ["", f"Commit `{state.sha}` on top of `{state.requested_sha}`; full diff:", ""]
        lines += ["~~~~diff", applied.read_text().rstrip(), "~~~~"]
    left = explained.get("left_alone", [])
    if left:
        lines += ["", "## What S0 simplify left alone", ""] + [f"- {item}" for item in left]
        lines += ["", f"Decide them with `qg simplify-queue --repo {state.repo}`."]
    unread = explained.get("unreviewed", [])
    if unread:
        lines += ["", f"## What S0 simplify did not review ({len(unread)} file(s))", ""]
        lines += [f"- {name}" for name in unread[:UNREVIEWED_SHOWN]]
        if len(unread) > UNREVIEWED_SHOWN:
            lines.append(f"- … {len(unread) - UNREVIEWED_SHOWN} more in simplify/explained.json")
    return lines + _audited(state)


def _audited(state: RunState) -> list[str]:
    """Returns report.md's lines on the nightly audit: the commits of the night, then per lens the
    parts it read whole and those an earlier run had audited, what went to the queue, what the
    refuter disproved and what it did not judge."""
    said = paths.audit_dir(state.id) / "audit.json"
    if not said.is_file():
        return []
    audit = json.loads(said.read_text())
    lines = ["", "## What the nightly audit found", ""]
    lines.append(
        f"{audit['commits']} commit(s) in {audit['units']} part(s), {audit['files']} file(s), "
        f"{audit['hits']} tool hit(s) as leads, ${audit['cost_usd']:.2f}."
    )
    if audit["links"]:
        lines += ["", "Symbolic links, not followed:", ""] + [
            f"- {name}" for name in audit["links"]
        ]
    if audit["no_code"]:
        lines += ["", "Commits with no reviewed file (tests, documents, tooling):", ""]
        lines += [f"- {commit}" for commit in audit["no_code"]]
    for lens, found in audit["lenses"].items():
        lines += ["", f"### {lens}", ""]
        lines += [
            f"{found['read']} part(s) read one by one to their last line, {found['earlier']} "
            "skipped: an earlier run audited them.",
            "",
        ]
        if found["queued"]:
            lines += [f"Queued for you ({len(found['queued'])}):", ""]
            lines += [f"- {item}" for item in found["queued"]]
            lines += [
                "",
                f"Decide them with `qg simplify-queue --repo {state.repo} --lens {lens}`.",
            ]
        else:
            lines.append("Nothing new queued.")
        if found["known"]:
            lines += ["", f"{found['known']} finding(s) already in the queue, not judged again."]
        if found["refuted"]:
            lines += ["", "Refuted, not queued:", ""] + [f"- {item}" for item in found["refuted"]]
        if found["unverified"]:
            lines += ["", "Not judged by the refuter, not queued:", ""]
            lines += [f"- {item}" for item in found["unverified"]]
    return lines


def _markdown(
    state: RunState, verdict: str, reasons: list[str], warnings: list[str], findings: list[Finding]
) -> str:
    """Builds report.md."""
    name = Path(state.repo).name
    lines = [f"# {verdict}: {name} {state.branch} @ {state.sha[:8]}", ""]
    lines += [
        f"- run: `{state.id}` ({state.status})",
        f"- env: {state.env}",
        f"- requested commit: `{state.requested_sha or state.sha}`",
        f"- verified commit: `{state.sha}`",
        f"- verified tree: `{state.tree}`",
        f"- feature: {state.feature}",
        f"- cycle: `{state.cycle}`",
        f"- verify.yml: `{state.config_ref}` @ `{state.config_sha}` (protected ref)",
    ]
    if state.config_branch_differs:
        lines.append("- verify.yml changed in this branch: not used")
    if state.base:
        lines.append(f"- base: `{state.base}`")
    else:
        lines.append("- base: none, so S0 simplify and the generated plan were skipped")
    if state.plan:
        lines.append(f"- plan: {state.plan}")
    if state.simplified_ref:
        name = state.simplified_ref.removeprefix("refs/heads/")
        lines.append(f"- simplified: `{state.requested_sha}` became `{state.sha}` on `{name}`")
        lines.append(
            f"- to take it: `git cherry-pick {state.sha}`; this run's suite ran on it"
            if state.mode == "nightly"
            else f"- **merge `{name}` into `{state.branch}` (fast-forward) before deploying**: "
            "`qg check` passes only for the verified tree"
        )
    if state.retest_of:
        lines.append(f"- retest of: `{state.retest_of}`")
    duration = human(elapsed(state.started, state.ended))
    lines.append(f"- started: {state.started or '-'}, ended: {state.ended or '-'} ({duration})")
    if state.error:
        lines.append(f"- error: {state.error}")
    lines += ["", "## Verdict", "", f"**{verdict}** for env `{state.env}`"]
    if notice := qa_notice(state):
        lines.append(f"- {notice}")
    lines += [f"- {reason}" for reason in reasons]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in warnings]
    if state.mode == "release":
        lines += release.phases_table(state, display_verdict)
    lines += ["", "## Stages", "", "| stage | status | duration | summary |", "|---|---|---|---|"]
    for stage_name, stage in release.effective_stages(state).items():
        seconds = human(elapsed(stage.started, stage.ended))
        lines.append(
            f"| {stage_name} | {queue.stage_text(stage)} | {seconds} | {_cell(stage.summary)} |"
        )
    failing = [(n, s) for n, s in state.stages.items() if s.failures]
    if failing:
        lines += ["", "## Failing tests"]
        for stage_name, stage in failing:
            shown = release.owner_lines(stage)
            lines += ["", f"### {stage_name}", ""] + [f"- {_cell(line)}" for line in shown]
    lines += _simplified(state)
    rejected = paths.simplify_dir(state.id) / "rejected.diff"
    if rejected.is_file():
        lines += ["", "## What S0 simplify tried and was refused", ""]
        lines += ["~~~~diff", rejected.read_text().rstrip(), "~~~~"]
    lines += ["", "## Findings", ""]
    if not findings:
        lines.append("No findings recorded.")
    else:
        lines += [FINDINGS_RULE.format(run=state.id), ""]
    for severity in SEVERITIES:
        group = [f for f in findings if f.severity == severity]
        if group:
            lines += [f"### {severity}", ""]
            for f in group:
                lines.append(f"- `{f.id}` [{status_word(f)}] {f.title}{_disposal_note(f)}")
                if note := verification_note(f):
                    lines.append(f"  - {note}")
                lines += [f"  - {note}" for note in probe_notes(f)]
            lines.append("")
    lines += ["", "## Files", "", f"- state: {paths.state_file(state.id)}"]
    lines.append(f"- report: {paths.report_json(state.id)}")
    lines.append(f"- executor log: {paths.run_dir(state.id) / 'executor.log'}")
    for log in sorted(paths.logs_dir(state.id).glob("*.log")):
        lines.append(f"- {log.stem} log: {log}")
    if (paths.run_dir(state.id) / "qa" / "charter.md").exists():
        lines.append(f"- qa charter: {paths.run_dir(state.id) / 'qa' / 'charter.md'}")
        lines.append(f"- qa evidence: {paths.run_dir(state.id) / 'evidence'}")
    return "\n".join(lines) + "\n"


def write(state: RunState, request: Request) -> str:
    """Writes report.json and report.md of the run and returns its verdict."""
    verdict, reasons = verdict_of(state, request)
    warnings = warnings_of(state)
    findings = load_findings(state.id)
    body = {
        "id": state.id,
        "repo": state.repo,
        "branch": state.branch,
        "sha": state.sha,
        "tree": state.tree,
        "requested_sha": state.requested_sha or state.sha,
        "requested_tree": state.requested_tree or state.tree,
        "verified_sha": state.sha,
        "verified_tree": state.tree,
        "base": state.base,
        "simplified_ref": state.simplified_ref,
        "feature": state.feature,
        "env": state.env,
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        "status": state.status,
        "cycle": state.cycle,
        "config_ref": state.config_ref,
        "config_sha": state.config_sha,
        "config_branch_differs": state.config_branch_differs,
        "retest_of": state.retest_of,
        "started": state.started,
        "ended": state.ended,
        "error": state.error,
        "stages": {name: asdict(stage) for name, stage in release.effective_stages(state).items()},
        "findings": [asdict(f) for f in findings],
    }
    secrets = redact.run_secrets(config.parse(request.config, f"request of run {state.id}"))
    safe = redact.scrub_tree(body, secrets)
    atomic_write(paths.report_json(state.id), json.dumps(safe, indent=2) + "\n")
    markdown = _markdown(state, verdict, reasons, warnings, findings)
    atomic_write(paths.report_md(state.id), redact.clean(markdown, secrets))
    return verdict


def refresh(run_id: str) -> None:
    """Regenerates the report files of a finished run, for example after an acknowledgement."""
    state = queue.load(run_id)
    if state.status in queue.FINISHED:
        write(state, queue.load_request(run_id))


BOARD_CSS = """
:root{--bg:#f2f4f7;--surface:#fff;--ink:#15202b;--ink-2:#44515e;--line:#d8dee5;--accent:#2b46c4;
--ok:#14753a;--ok-bg:#e3f3e8;--bad:#b3261e;--bad-bg:#fbe9e7;--warn:#8f5b00;--warn-bg:#fdf0d5;
--skip:#56626f;--skip-bg:#e8ecf0;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f151b;--surface:#161e26;
--ink:#e6ebf0;--ink-2:#a5b1bd;--line:#26313c;--accent:#8da2ff;--ok:#5fd38a;--ok-bg:#12301f;
--bad:#ff8a80;--bad-bg:#3a1714;--warn:#ffc666;--warn-bg:#352607;--skip:#9aa7b3;--skip-bg:#212b35;
color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f151b;--surface:#161e26;--ink:#e6ebf0;--ink-2:#a5b1bd;--line:#26313c;
--accent:#8da2ff;--ok:#5fd38a;--ok-bg:#12301f;--bad:#ff8a80;--bad-bg:#3a1714;--warn:#ffc666;
--warn-bg:#352607;--skip:#9aa7b3;--skip-bg:#212b35;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
main{max-width:80rem;margin-inline:auto;padding:1.2rem 1rem 3rem}
h1{font-size:1.3rem;margin:0 0 .3rem}
p{color:var(--ink-2);margin:0 0 1rem}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line)}
table{border-collapse:collapse;width:100%;min-width:56rem}
th,td{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-2)}
td.num{font-variant-numeric:tabular-nums}
code{font:12px ui-monospace,Menlo,monospace}
a{color:var(--accent)}
.chip{display:inline-block;padding:.1rem .4rem;border:1px solid currentColor;border-radius:2px;
font:600 .72rem ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.05em}
.READY{color:var(--ok);background:var(--ok-bg)}
.NOT-READY,.error,.failed{color:var(--bad);background:var(--bad-bg)}
.running,.queued{color:var(--warn);background:var(--warn-bg)}
.cancelled,.interrupted,.superseded,.passed{color:var(--skip);background:var(--skip-bg)}
"""


def _stage_times(state: RunState) -> str:
    """Formats the durations of the stages that ran, e.g. 'static 12s, backend 4m07s'."""
    parts = [
        f"{name} {human(elapsed(stage.started, stage.ended))}"
        for name, stage in state.stages.items()
        if stage.started
    ]
    return ", ".join(parts) or "-"


def render_board(runs: list[RunState]) -> str:
    """Builds the self-contained HTML board with the last run of every repo+branch."""
    rows = []
    for run in queue.latest_per_branch(runs):
        verdict = display_verdict(run)
        p0, p1 = finding_counts(run) if run.status in queue.FINISHED else (0, 0)
        cls = verdict.replace(" ", "-")
        links = [
            f'<a href="{html.escape(paths.report_md(run.id).as_uri())}">report</a>',
            f'<a href="{html.escape(paths.run_dir(run.id).as_uri())}">run</a>',
        ]
        cycle_links = ""
        if cycles.exists(run.cycle):
            cycle = cycles.load(run.cycle)
            if paths.page_file(cycle.id).exists():
                cycle_links += (
                    f'<a href="{html.escape(paths.page_file(cycle.id).as_uri())}">page</a> '
                )
            if cycle.artifact_url:
                cycle_links += f'<a href="{html.escape(cycle.artifact_url)}">artifact</a>'
        rows.append(
            "<tr>"
            f"<td>{html.escape(Path(run.repo).name)}<br><code>{html.escape(run.branch)}</code></td>"
            f"<td>{html.escape(run.feature)}</td>"
            f"<td><code>{run.sha[:8]}</code><br>{html.escape(run.env)}</td>"
            f'<td><span class="chip {html.escape(cls)}">{html.escape(verdict)}</span></td>'
            f'<td class="num">{p0} / {p1}</td>'
            f"<td>{html.escape(_stage_times(run))}</td>"
            f"<td>{' '.join(links)}</td>"
            f"<td>{cycle_links}</td>"
            f"<td><code>{html.escape(run.id)}</code>"
            f"<br>{html.escape(run.ended or run.created)}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="9">No runs yet.</td></tr>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>qg board</title><style>{BOARD_CSS}</style></head><body><main>"
        "<h1>qg board</h1>"
        f"<p>Last run of every repo and branch. Generated {html.escape(now_iso())}. "
        "P0 / P1: open P0 findings block a deploy; an open P1 blocks until the operator "
        "acknowledges it.</p>"
        '<div class="scroll"><table><thead><tr><th>repo / branch</th><th>feature</th>'
        "<th>commit / env</th><th>verdict</th><th>P0 / P1</th><th>stages</th><th>files</th>"
        "<th>cycle</th><th>run</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></main></body></html>\n"
    )
