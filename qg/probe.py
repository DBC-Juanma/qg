"""The P0 probe and flip: confined agents write a pytest reproduction of each open P0 and the
minimal fix that turns it green; qg runs both.

A probe is red only when its one test failed cleanly against the run's commit. A P0 without a red
probe keeps blocking; the verifier then judges whether the probe and the flip are naive. A flip is
never merged: its patch is attached to the finding.
"""

import json
import re
import shlex
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from qg import (
    affected,
    config,
    git,
    jail,
    paths,
    proc,
    qa,
    report,
    settings,
    simplify,
    stages,
    verifier,
)
from qg import agent as agents
from qg.config import Config
from qg.report import (
    CONTRADICTS,
    FLIP_FAILED,
    GREEN,
    NO_PROBE,
    PATCHED,
    PROBE_ERROR,
    RED,
    SPECIAL_CASE,
    Finding,
    Reproduction,
)
from qg.sandbox import Sandbox, base_env
from qg.util import atomic_write, fill, now_iso, read_tail

PROMPT_FILE = Path(__file__).with_name("probe_prompt.md")
FLIP_PROMPT_FILE = Path(__file__).with_name("flip_prompt.md")
ATTEMPTS = 3
MAX_TURNS = 60
AGENT_MINUTES = 12
RUN_MINUTES = 5
RUNS = 3
MIN_AGENT_SECONDS = 120
MIN_COST_USD = 0.25
OUTPUT_TAIL = 8000
MAX_CONTRADICTED = 5
NO_ATTEMPT = "no attempt was made"
SLUG = re.compile(r"[^a-z0-9]+")


class ProbeFailure(Exception):
    """A reason one finding's probe cannot go on; its text becomes the probe's reason."""


@dataclass(frozen=True)
class Verdict:
    """How one run of a probe ended: red, or error with why."""

    status: str
    reason: str


def probe_path(cfg: config.Probe, finding_id: str) -> str:
    """Returns where the agent writes the probe of `finding_id`, relative to the repository."""
    return f"{cfg.dir}test_qg_{SLUG.sub('_', finding_id.lower()).strip('_')}.py"


def variant_path(path: str) -> str:
    """Returns where the held-out variant of the probe at `path` is written."""
    return f"{path.removesuffix('.py')}_variant.py"


def _command(cfg: Config, path: str) -> str:
    """Returns the command that runs the probe at `path`: affected.backend's, on that file only."""
    assert cfg.affected.backend is not None
    return cfg.affected.backend.command.replace("{tests}", shlex.quote(path))


def _run(box: Sandbox, cfg: Config, src: Path, path: str, log: Path) -> stages.StageResult:
    """Runs the probe at `path` of the checkout `src` on the QA lane's ports and Postgres."""
    return stages.run_commands(
        replace(box.for_qa(), src=src),
        cfg,
        "backend",
        (_command(cfg, path),),
        [],
        [],
        log,
        limit=RUN_MINUTES,
    )


def passed(result: stages.StageResult) -> bool:
    """Tells whether the probe passed: exit 0, its one test passed and none failed."""
    return (
        result.status == "passed"
        and bool(result.commands)
        and result.commands[-1].passed == 1
        and not result.failures
    )


def _node(reason: str) -> str:
    """Returns the test a red verdict's FAILED line names, without pytest's message."""
    return reason.split(" - ", 1)[0]


def _outcome(result: stages.StageResult, path: str) -> str:
    """Returns how one run of the probe ended: green, the failing test when red, else empty."""
    if passed(result):
        return GREEN
    verdict = classify(result, path)
    return _node(verdict.reason) if verdict.status == RED else ""


def classify(result: stages.StageResult, path: str) -> Verdict:
    """Returns red when the probe's one test failed cleanly, else error with the reason.

    Red needs pytest's exit code 1, exactly one FAILED line naming a test of `path`, no ERROR
    line and no passed test; a timeout, a crash, a collection or fixture error never counts.
    """
    if not result.commands:
        return Verdict(PROBE_ERROR, f"the probe did not run: {result.summary}")
    run = result.commands[-1]
    failed = [line for line in result.failures if line.startswith("FAILED ")]
    errors = [line for line in result.failures if line.startswith("ERROR ")]
    if "timed out" in result.summary or "out of memory" in result.summary:
        return Verdict(PROBE_ERROR, f"the probe did not finish: {result.summary}")
    if run.code == 0:
        return Verdict(
            PROBE_ERROR, "the probe passed on this commit: it does not reproduce the finding"
        )
    if errors:
        return Verdict(PROBE_ERROR, f"the probe errored instead of failing: {errors[0]}")
    if run.code != 1 or len(failed) != 1 or run.passed:
        return Verdict(
            PROBE_ERROR,
            f"the probe must fail exactly one test with exit 1 "
            f"(exit {run.code}, {len(failed)} failed, {run.passed} passed)",
        )
    if not failed[0].startswith(f"FAILED {path}::"):
        return Verdict(PROBE_ERROR, f"the failing test is not the probe's: {failed[0]}")
    return Verdict(RED, failed[0])


def check_written(changed: set[str], path: str) -> None:
    """Raises ProbeFailure unless the agent's only change is the new, untracked probe file."""
    expected = {f"?? {path}"}
    if changed != expected:
        listed = ", ".join(sorted(changed)[:10]) or "nothing"
        raise ProbeFailure(f"the probe agent must add only {path}; it changed {listed}")


def check_flip(src: Path, changed: set[str], path: str, source_dirs: tuple[str, ...]) -> list[str]:
    """Returns the product files the flip changed; raises ProbeFailure on any other change.

    Every changed path but the probe must be a source file under `source_dirs` that is no test,
    fixture, registry or tooling file, nor a symlink, nor reached through one.
    """
    if f"?? {path}" not in changed:
        raise ProbeFailure(f"the flip agent removed or committed the probe {path}")
    names = sorted(entry[3:] for entry in changed if entry != f"?? {path}")
    if not names:
        raise ProbeFailure("the flip agent changed no product file")
    root = src.resolve()
    refused = [
        name
        for name in names
        if not name.startswith(source_dirs)
        or simplify.protected(name)
        or (src / name).is_symlink()
        or (src / name).parent.resolve() != (root / Path(name).parent).resolve()
    ]
    if refused:
        listed = ", ".join(refused[:10]) + (f" (+{len(refused) - 10})" if len(refused) > 10 else "")
        raise ProbeFailure(
            f"the flip agent changed {len(refused)} path(s) it may not touch: {listed}"
        )
    return names


@contextmanager
def scratch(repo: str, sha: str, target: Path) -> Iterator[Path]:
    """Yields a detached worktree of `sha` at `target`, hooks off, removed again afterwards."""
    shutil.rmtree(target, ignore_errors=True)
    added = proc.run(
        [*simplify.GIT, "-C", repo, "worktree", "add", "--detach", str(target), sha],
        timeout=600,
    )
    if added.returncode != 0:
        raise ProbeFailure(f"git worktree add failed: {added.stderr.strip()}")
    try:
        yield target
    finally:
        proc.run(
            [*simplify.GIT, "-C", repo, "worktree", "remove", "--force", str(target)], timeout=600
        )
        shutil.rmtree(target, ignore_errors=True)
        git.remove_worktree_entry(repo, str(target))


def write_card(finding: Finding, run_id: str, plan: str, card: Path) -> None:
    """Writes what the agent reads of the finding: finding.json, plan.md and the cited evidence."""
    shutil.rmtree(card, ignore_errors=True)
    evidence = card / "evidence"
    evidence.mkdir(parents=True)
    body = verifier.card(finding, run_id, evidence)
    atomic_write(card / "finding.json", json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    atomic_write(card / "plan.md", plan or "No plan file was given to this run.\n")


HELD_OUT = (
    "\nA first probe of this finding already exists: `first-probe.py` in {card_dir}.\n"
    "Write a second, independent test of the same defect with other data and other values\n"
    "(and, where the product offers one, another entry point), so that a fix that only\n"
    "special-cases the first probe's data still fails yours. Do not copy its literals.\n"
)
PREVIOUS = {
    PROMPT_FILE: "what your previous attempt produced when qg ran it, and why it\n"
    "  is not a reproduction yet. Rewrite the probe so it fails because of the defect: make\n"
    "  it pass against what the product does today first, then invert only its final assertion.",
    FLIP_PROMPT_FILE: "why your previous flip is not green yet, with the output qg saw.\n"
    "  Your edits are still in the checkout: correct them.",
}


def render(
    template: Path,
    path: str,
    card: Path,
    command: str,
    previous: Path | None,
    source_dirs: tuple[str, ...] = (),
    held_out: bool = False,
) -> str:
    """Fills `template` (probe_prompt.md or flip_prompt.md); `previous` is what the last attempt
    left the agent, when there was one; `held_out` asks for the held-out variant of a probe."""
    told = f"- `{previous.name}`: {PREVIOUS[template]}\n" if previous else ""
    values = {"probe_path": path, "card_dir": str(card), "command": command, "previous": told}
    values["source_dirs"] = ", ".join(f"`{name}`" for name in source_dirs)
    values["held_out"] = fill(HELD_OUT, values) if held_out else ""
    return fill(template.read_text(), values)


def _feedback(card: Path, name: str, reason: str, log: Path) -> Path:
    """Writes why the last attempt failed, with the tail of its run log, for the next agent."""
    previous = card / name
    atomic_write(previous, f"{reason}\n\n{read_tail(log)[-OUTPUT_TAIL:]}")
    return previous


def _agent(
    who: str,
    attempt: int,
    run_id: str,
    finding_id: str,
    src: Path,
    card: Path,
    prompt: str,
    seconds: float,
    cost: float,
    wrap: Callable[[list[str]], list[str]],
) -> dict[str, Any] | None:
    """Runs one confined `who` agent (probe or flip) on `src`; returns its result.

    A crash raises ProbeFailure; running out of time, turns or money does not: what it wrote is
    still checked and run.
    """
    tools = settings.load()
    claude = jail.claude_binary()
    home = paths.probe_dir(run_id, finding_id) / "home"
    logs = paths.logs_dir(run_id)
    paths.NPM.mkdir(parents=True, exist_ok=True)
    output = card.parent / f"{who}-{attempt}-output.json"
    errors = logs / f"{who}-{finding_id}-agent.log"

    def renew(valid: float) -> None:
        """Renews the live login outside the jail until it covers `valid` seconds."""
        jail.ensure_login(claude, base_env(), valid)

    valid_for = seconds + jail.LOGIN_MARGIN_SECONDS
    renew(valid_for)
    jail.prepare_home(home, valid_for)
    command = wrap(
        jail.command(
            agents.claude_command(claude, prompt, MAX_TURNS, cost, card, simplify.TOOLS),
            workdir=src,
            writable=[],
            home=home,
            tools=tools,
            claude=claude,
            npm_cache=paths.NPM,
            readable=(card,),
        )
    )
    try:
        run = qa.retry_on_usage_limit(
            lambda left, _resumed: qa.run_agent(
                command, cwd=src, env=base_env(), out=output, err=errors, timeout=left
            ),
            home=home,
            budget=seconds,
            out=output,
            err=errors,
            renew=renew,
        )
    finally:
        jail.discard_home(home)
    result = qa.read_result(output)
    spent: list[str] = []
    problems = qa.agent_problems(
        run,
        result,
        max_turns=MAX_TURNS,
        max_cost_usd=cost,
        limit=seconds,
        err=errors,
        who=f"{who} agent",
        budget=spent,
    )
    if problems:
        raise ProbeFailure("; ".join(problems))
    return result


def _agent_in(
    who: str,
    attempt: int,
    box: Sandbox,
    finding: Finding,
    src: Path,
    card: Path,
    prompt: str,
    seconds: float,
    cost: float,
) -> float:
    """Runs one `who` agent on `src` and returns what it cost; raises ProbeFailure when it
    changed the .git file or an ignored file."""
    before = simplify.snapshot(src)
    result = _agent(
        who,
        attempt,
        box.run_id,
        finding.id,
        src,
        card,
        prompt,
        seconds,
        cost,
        lambda argv: box.wrap("qa_agent", argv),
    )
    if simplify.snapshot(src) != before:
        raise ProbeFailure(f"the {who} agent changed the .git file or an ignored file")
    return qa.cost_of(result) or 0.0


def _left(deadline: float, cap: float, spent: float) -> tuple[float, float, str]:
    """Returns the seconds and dollars the next attempt gets, and why there is none (else empty).

    The agent's seconds leave room before `deadline` for the RUNS runs of what it writes.
    """
    seconds = min(AGENT_MINUTES * 60.0, deadline - time.monotonic() - RUNS * RUN_MINUTES * 60)
    cost = cap - spent
    if seconds < MIN_AGENT_SECONDS:
        return seconds, cost, "time"
    if cost < MIN_COST_USD:
        return seconds, cost, "budget"
    return seconds, cost, ""


def _probe(
    box: Sandbox,
    cfg: Config,
    finding: Finding,
    src: Path,
    card: Path,
    deadline: float,
    variant: bool = False,
) -> Verdict:
    """Has up to ATTEMPTS agents write the probe, or its held-out `variant`, in `src` until it is
    red; returns the last verdict.

    The file and the output of its last run are kept in the finding's probe directory.
    """
    assert cfg.probe is not None
    path = probe_path(cfg.probe, finding.id)
    path, role, tag = (
        (variant_path(path), "variant", "variant-") if variant else (path, "probe", "")
    )
    home = paths.probe_dir(box.run_id, finding.id)
    command = _command(cfg, path)
    spent = 0.0
    verdict = Verdict(PROBE_ERROR, NO_ATTEMPT)
    previous: Path | None = None
    for attempt in range(1, ATTEMPTS + 1):
        seconds, cost, short = _left(deadline, cfg.probe.max_cost_usd, spent)
        if short:
            why = f"{verdict.reason}; out of {short} after {attempt - 1} attempt(s)"
            return Verdict(verdict.status, why)
        prompt = render(PROMPT_FILE, path, card, command, previous, held_out=variant)
        spent += _agent_in(role, attempt, box, finding, src, card, prompt, seconds, cost)
        check_written(simplify.porcelain(src), path)
        if (src / path).is_symlink():
            raise ProbeFailure(f"the {role} agent made {path} a symlink")
        log = home / f"{tag}run-{attempt}.log"
        verdict = classify(_run(box, cfg, src, path, log), path)
        shutil.copyfile(src / path, home / Path(path).name)
        shutil.copyfile(
            log, home / "variant.log" if variant else paths.probe_log(box.run_id, finding.id)
        )
        if verdict.status == RED:
            verdict, log = _confirm(box, cfg, src, path, home, attempt, verdict, deadline, tag)
        if verdict.status == RED:
            return verdict
        previous = _feedback(card, f"{tag}attempt-{attempt}.txt", verdict.reason, log)
    return verdict


def _confirm(
    box: Sandbox,
    cfg: Config,
    src: Path,
    path: str,
    home: Path,
    attempt: int,
    first: Verdict,
    deadline: float,
    tag: str = "",
) -> tuple[Verdict, Path]:
    """Runs a red probe until RUNS runs in all; returns it red only when every run failed the same
    test, else error with why, and the log of the last run (named after `tag`)."""
    log = home / f"{tag}run-{attempt}.log"
    for again in range(2, RUNS + 1):
        if deadline - time.monotonic() < RUN_MINUTES * 60:
            return Verdict(PROBE_ERROR, f"no time was left for red run {again} of {RUNS}"), log
        log = home / f"{tag}run-{attempt}-{again}.log"
        verdict = classify(_run(box, cfg, src, path, log), path)
        if verdict.status != RED or _node(verdict.reason) != _node(first.reason):
            why = f"run 1 {first.reason}; run {again} {verdict.reason}"
            return Verdict(PROBE_ERROR, f"the probe is not deterministic: {why}"), log
    return first, log


def _patch(src: Path, names: list[str]) -> str:
    """Returns the diff of `names` in `src` against HEAD, new files included."""
    for args in (["add", "--intent-to-add", "--", *names], ["diff", "HEAD", "--", *names]):
        done = proc.run([*simplify.GIT, "-C", str(src), *args], timeout=120)
        if done.returncode != 0:
            raise ProbeFailure(f"git {args[0]} of the flip failed: {done.stderr.strip()}")
    return done.stdout


def _green(
    box: Sandbox,
    cfg: Config,
    src: Path,
    path: str,
    names: list[str],
    log: Path,
    deadline: float,
) -> tuple[str, str]:
    """Runs the probe, then the tests affected by `names`, in `src`, both within `deadline`;
    returns the flip status and reason: green, contradicts (the probe passes and only existing
    tests fail, listed in the reason) or failed."""
    ran = _run(box, cfg, src, path, log)
    if not passed(ran):
        found = classify(ran, path)
        why = f"the probe still fails: {found.reason}" if found.status == RED else found.reason
        return FLIP_FAILED, why
    selection = affected.backend(cfg.affected, names, lambda name: (src / name).exists())
    assert selection is not None
    if selection.problems:
        return FLIP_FAILED, "; ".join(selection.problems)
    minutes = int((deadline - time.monotonic()) // 60)
    if minutes < 1:
        return FLIP_FAILED, "the probe passes; no time was left to run the affected tests"
    after = stages.run_commands(
        replace(box.for_qa(), src=src),
        cfg,
        "backend",
        tuple(selection.commands),
        [],
        [],
        log,
        limit=min(cfg.timeouts["backend"], minutes),
    )
    if after.status == "passed":
        return GREEN, f"{len(names)} file(s) changed; the probe and the affected tests pass"
    failing = [line for line in after.failures if line.startswith("FAILED ")]
    if failing and len(failing) == len(after.failures) and not after.summary.startswith("timed"):
        listed = "; ".join(failing[:MAX_CONTRADICTED])
        return CONTRADICTS, (
            f"the probe passes but {len(failing)} existing test(s) fail with the flip: {listed}"
        )
    failing_text = "; ".join(after.failures[:3])
    return FLIP_FAILED, f"the affected tests did not run cleanly: {after.summary}" + (
        f" ({failing_text})" if failing_text else ""
    )


def _flip(
    box: Sandbox,
    cfg: Config,
    finding: Finding,
    src: Path,
    plan: str,
    red: Verdict,
    deadline: float,
) -> tuple[str, str]:
    """Has up to ATTEMPTS agents fix the product in `src` until the red probe and the affected
    tests pass; returns the flip status and reason.

    A green flip's patch is saved as flip.patch. A flip that turns the probe green while existing
    tests fail is kept too, as contradicts, when no attempt reaches green: those tests may assert
    the defect itself, which the verifier judges.
    """
    assert cfg.probe is not None
    path = probe_path(cfg.probe, finding.id)
    home = paths.probe_dir(box.run_id, finding.id)
    card = home / "flip-card"
    made = Reproduction(RED, red.reason, box.run_id, path)
    write_card(replace(finding, probe=made), box.run_id, plan, card)
    command = _command(cfg, path)
    source_dirs = cfg.simplify.paths
    probe_text = (src / path).read_bytes()
    spent = 0.0
    why = NO_ATTEMPT
    contradicted = kept = ""
    previous: Path | None = None
    for attempt in range(1, ATTEMPTS + 1):
        seconds, cost, short = _left(deadline, cfg.probe.max_cost_usd, spent)
        if short:
            why = f"{why}; out of {short} after {attempt - 1} attempt(s)"
            break
        prompt = render(FLIP_PROMPT_FILE, path, card, command, previous, source_dirs)
        spent += _agent_in("flip", attempt, box, finding, src, card, prompt, seconds, cost)
        if (src / path).read_bytes() != probe_text:
            raise ProbeFailure(f"the flip agent changed the probe {path}")
        names = check_flip(src, simplify.porcelain(src), path, source_dirs)
        log = home / f"flip-{attempt}.log"
        status, why = _green(box, cfg, src, path, names, log, deadline)
        patch = _patch(src, names) if status in (GREEN, CONTRADICTS) else ""
        if patch:
            atomic_write(paths.flip_patch(box.run_id, finding.id), patch)
        if status == GREEN:
            return GREEN, why
        if status == CONTRADICTS and patch == kept:
            return CONTRADICTS, why
        if status == CONTRADICTS:
            contradicted, kept = why, patch
        previous = _feedback(card, f"flip-attempt-{attempt}.txt", why, log)
    if contradicted:
        return CONTRADICTS, contradicted
    return FLIP_FAILED, why


def _apply(src: Path, patch: Path) -> None:
    """Applies `patch` to the checkout `src` and drops the bytecode of the files it changes: their
    red runs may have cached it within the same second, which Python would still trust."""
    listed = proc.run(
        [*simplify.GIT, "-C", str(src), "apply", "--numstat", str(patch)], timeout=120
    )
    applied = proc.run([*simplify.GIT, "-C", str(src), "apply", str(patch)], timeout=120)
    if listed.returncode != 0 or applied.returncode != 0:
        raise ProbeFailure(
            f"git apply of the flip failed: {(listed.stderr + applied.stderr).strip()}"
        )
    for line in listed.stdout.splitlines():
        shutil.rmtree(src / Path(line.split("\t")[-1]).parent / "__pycache__", ignore_errors=True)


def _held_out(
    box: Sandbox, cfg: Config, finding: Finding, plan: str, flip: tuple[str, str], deadline: float
) -> tuple[tuple[str, str], Verdict]:
    """Has an agent write a held-out variant of the red probe on a fresh checkout and, when it is
    red, runs it with the flip's saved patch applied; returns the flip, special-case when the
    variant still fails, and the variant's verdict."""
    assert cfg.probe is not None
    home = paths.probe_dir(box.run_id, finding.id)
    path = variant_path(probe_path(cfg.probe, finding.id))
    card = home / "variant-card"
    write_card(finding, box.run_id, plan, card)
    shutil.copyfile(home / Path(probe_path(cfg.probe, finding.id)).name, card / "first-probe.py")
    with scratch(box.repo, box.sha, home / "variant-src") as src:
        verdict = _probe(box, cfg, finding, src, card, deadline, variant=True)
        if verdict.status != RED:
            return (flip[0], f"{flip[1]}; no held-out variant: {verdict.reason}"), verdict
        patch = paths.flip_patch(box.run_id, finding.id)
        _apply(src, patch)
        if deadline - time.monotonic() < RUN_MINUTES * 60:
            return (flip[0], f"{flip[1]}; no time was left to run the held-out variant"), verdict
        ran = _run(box, cfg, src, path, home / "variant-flip.log")
    if passed(ran):
        return (flip[0], f"{flip[1]}; the held-out variant passes too"), verdict
    still = classify(ran, path).reason if ran.commands else ran.summary
    why = f"the probe passes with the flip but the held-out variant does not: {still}"
    return (SPECIAL_CASE, why), verdict


def reproduce(
    box: Sandbox, cfg: Config, finding: Finding, plan: str, deadline: float
) -> Reproduction:
    """Makes and runs the probe of one P0 on a scratch worktree of the run's commit and, when it
    is red, the flip that turns it green, within the probe's cost cap per agent and `deadline`
    (a time.monotonic() value)."""
    if cfg.probe is None or cfg.affected.backend is None:
        return Reproduction(NO_PROBE, "verify.yml configures no probe", box.run_id, when=now_iso())
    path = probe_path(cfg.probe, finding.id)
    home = paths.probe_dir(box.run_id, finding.id)
    card = home / "card"
    write_card(finding, box.run_id, plan, card)
    verdict = Verdict(PROBE_ERROR, NO_ATTEMPT)
    flip = ("", "")
    variant = Verdict("", "")
    try:
        with scratch(box.repo, box.sha, home / "src") as src:
            verdict = _probe(box, cfg, finding, src, card, deadline)
            if verdict.status == RED:
                try:
                    flip = _flip(box, cfg, finding, src, plan, verdict, deadline)
                except (
                    ProbeFailure,
                    jail.JailError,
                    proc.AccountFailure,
                    settings.SettingsError,
                ) as exc:
                    flip = (FLIP_FAILED, str(exc))
    except (ProbeFailure, jail.JailError, proc.AccountFailure, settings.SettingsError) as exc:
        verdict = Verdict(PROBE_ERROR, str(exc))
    if flip[0] in PATCHED:
        try:
            flip, variant = _held_out(box, cfg, finding, plan, flip, deadline)
        except (ProbeFailure, jail.JailError, proc.AccountFailure, settings.SettingsError) as exc:
            flip, variant = (
                (flip[0], f"{flip[1]}; no held-out variant: {exc}"),
                Verdict(PROBE_ERROR, str(exc)),
            )
    file = path if (home / Path(path).name).is_file() else ""
    return Reproduction(
        verdict.status,
        verdict.reason,
        box.run_id,
        file,
        when=now_iso(),
        flip=flip[0],
        flip_reason=flip[1],
        variant=variant.status,
        variant_reason=variant.reason,
    )


def candidates(findings: list[Finding]) -> list[Finding]:
    """Returns the open P0 that have no probe yet, in the order the run filed them."""
    return [f for f in findings if report.blocking(f) and f.severity == "P0" and f.probe is None]


def probe_run(box: Sandbox, cfg: Config, plan: str, deadline: float) -> str:
    """Probes every open P0 of the run that has none, up to the configured count, by `deadline`.

    Saves each finding's Reproduction as soon as it is known. Returns the note for the QA stage's
    summary, empty when verify.yml configures no probe or there is no P0 to probe.
    """
    if cfg.probe is None:
        return ""
    findings = report.load_findings(box.run_id)
    targets = candidates(findings)
    if not targets:
        return ""
    made: dict[str, Reproduction] = {}
    for index, finding in enumerate(targets):
        if index >= cfg.probe.max_p0:
            why = f"only {cfg.probe.max_p0} P0 are probed per run"
            made[finding.id] = Reproduction(NO_PROBE, why, box.run_id, when=now_iso())
        elif deadline - time.monotonic() < MIN_AGENT_SECONDS:
            why = "the probe pass ran out of time"
            made[finding.id] = Reproduction(NO_PROBE, why, box.run_id, when=now_iso())
        else:
            made[finding.id] = reproduce(box, cfg, finding, plan, deadline)
        findings = report.with_probe(findings, finding.id, made[finding.id])
        report.save_findings(box.run_id, findings)
    red = sum(1 for item in made.values() if item.status == RED)
    green = sum(1 for item in made.values() if item.flip == GREEN)
    clash = sum(1 for item in made.values() if item.flip == CONTRADICTS)
    return (
        f"probes: {red} red of {len(made)} P0; flips: {green} green, "
        f"{clash} contradicting existing tests"
    )


def _rerun(box: Sandbox, cfg: Config, finding: Finding, deadline: float) -> str:
    """Runs a reproduced finding's probe RUNS times on the run's commit: fixed when every run is
    green, open when every run fails the same test.

    Empty when it can tell neither: the runs disagree (a flaky probe never decides), no time is
    left before `deadline` for the next run, the probe file is gone, the checkout fails, or the
    probe no longer runs cleanly.
    """
    made = finding.probe
    assert made is not None
    source = paths.probe_dir(made.run_id, finding.id) / Path(made.path).name
    if not source.is_file():
        return ""
    home = paths.probe_dir(box.run_id, finding.id)
    home.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    try:
        with scratch(box.repo, box.sha, home / "retest-src") as src:
            target = src / made.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            for run in range(1, RUNS + 1):
                if deadline - time.monotonic() < RUN_MINUTES * 60:
                    return ""
                ran = _run(box, cfg, src, made.path, home / f"retest-{run}.log")
                seen.add(_outcome(ran, made.path))
                if len(seen) > 1 or "" in seen:
                    return ""
    except ProbeFailure:
        return ""
    return "fixed" if seen == {GREEN} else "open"


def recheck(box: Sandbox, cfg: Config, deadline: float) -> str:
    """Re-runs on the run's commit the accepted probe of every P0 an earlier run reproduced.

    Green closes the finding (`fixed`) though the QA agent never saw it again; red keeps it open
    whatever the agent said. A probe that no longer runs cleanly leaves the finding as the QA
    stage recorded it, and so does running out of `deadline`. Returns the note for the QA stage's
    summary, empty with nothing to rerun.
    """
    if cfg.probe is None or cfg.affected.backend is None:
        return ""
    findings = report.load_findings(box.run_id)
    targets = [
        f
        for f in findings
        if f.dismissed is None
        and report.reproduced(f)
        and f.probe is not None
        and f.probe.run_id != box.run_id
    ]
    if not targets:
        return ""
    decided = {f.id: _rerun(box, cfg, f, deadline) for f in targets}
    report.save_findings(
        box.run_id,
        [replace(f, status=decided[f.id]) if decided.get(f.id) else f for f in findings],
    )
    green = sum(1 for status in decided.values() if status == "fixed")
    red = sum(1 for status in decided.values() if status == "open")
    left = len(decided) - green - red
    return f"probe retest: {green} green (fixed), {red} red (open), {left} undecided"


def probe_pass(box: Sandbox, cfg: Config, plan: str) -> str:
    """Runs the retest of earlier probes, then the probes of new P0, within one shared time
    limit (`probe.time_limit_minutes`, part of the run's deadline); returns the stage note."""
    if cfg.probe is None:
        return ""
    deadline = time.monotonic() + cfg.probe.time_limit_minutes * 60
    notes = [recheck(box, cfg, deadline), probe_run(box, cfg, plan, deadline)]
    return "; ".join(note for note in notes if note)
