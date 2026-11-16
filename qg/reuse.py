"""Stage reuse: a stage that passed on the same verified tree is not run again.

Every decision fails closed: anything the code cannot prove identical means the stage runs.
"""

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from qg import config, git, paths, phase_scope, queue, report, rules
from qg.config import COMMAND_STAGES, Config
from qg.queue import Request, RunState, StageState
from qg.sandbox import ENV_ALLOWED

SOURCE_STATUSES = ("passed", "failed", "cancelled", "interrupted", "error")


@dataclass(frozen=True)
class Source:
    """A finished run that may lend its passed stages, with its own request and configuration."""

    run: RunState
    request: Request
    config: Config


def fingerprint(
    cfg: Config,
    stage: str,
    tree: str,
    specs: tuple[str, ...] = (),
    selected: tuple[str, ...] = (),
    plan: str = "",
    scale: float = 1,
) -> str:
    """Returns the hash of everything that decides what a stage proves for a verified tree.

    The tree, the stage's commands from the protected configuration, the dependency and database
    configuration, the env allowlist, and for Playwright the specs and for the QA pass its whole
    configuration and the plan text, and for backend and frontend the affected-test mapping and
    the tests it selected, and for static its rules and their profile when verify.yml sets them.
    """
    extra: object = None
    if stage == "playwright":
        extra = {"run": cfg.playwright.run, "specs": list(specs)}
    elif stage in ("backend", "frontend"):
        extra = {"affected": asdict(cfg.affected), "selected": list(selected)}
    elif stage == "qa":
        extra = {"qa": asdict(cfg.qa_scaled(scale)), "plan": plan}
    elif stage == "static" and cfg.rules is not None:
        extra = {"rules": asdict(cfg.rules), "profile": rules.profile_text(cfg.rules.profile)}
    body = {
        "stage": stage,
        "tree": tree,
        "env": sorted(ENV_ALLOWED),
        "deps": asdict(cfg.deps),
        "postgres": asdict(cfg.postgres),
        "commands": list(cfg.commands(stage)) if stage in COMMAND_STAGES else [],
        "extra": extra,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def recorded(source: Source, stage: str) -> str:
    """Returns the fingerprint a source stage ran with; one that predates fingerprints is rebuilt.

    A rebuilt fingerprint is possible for the command stages and Playwright only: the source's
    own request holds its configuration and its state the verified tree and the specs it ran. The
    QA pass has none to rebuild, so an old QA result is never reused.
    """
    held = source.run.stages[stage]
    if held.fingerprint:
        return held.fingerprint
    if stage == "qa":
        return ""
    selected = tuple(held.specs) if stage in ("backend", "frontend") else ()
    specs = tuple(held.specs) if stage == "playwright" else ()
    return fingerprint(source.config, stage, source.run.tree, specs, selected)


def _source(run: RunState) -> Source | None:
    """Returns the run with its request and configuration, None when they cannot be read."""
    try:
        asked = queue.load_request(run.id)
        return Source(run, asked, config.parse(asked.config, f"request of run {run.id}"))
    except (OSError, ValueError, TypeError, SystemExit):
        return None


def sources(request: Request) -> list[Source]:
    """Returns the earlier finished runs of the same repo, env, requested tree and base.

    A run without the hardened runner's provenance is never a source.
    """
    found = []
    for run in sorted(queue.list_runs(), key=lambda r: r.id, reverse=True):
        same = (
            run.id < request.id
            and run.status in SOURCE_STATUSES
            and run.repo == request.repo
            and run.env == request.env
            and (run.mode == "nightly") == (request.mode == "nightly")
            and run.requested_tree == request.tree
            and report.trusted(run)
            and not report.reuse_problems(run)
        )
        source = _source(run) if same else None
        if source is not None and source.request.base == request.base:
            found.append(source)
    return found


@dataclass(frozen=True)
class Simplified:
    """What a reused S0 hands to the later stages: the stage as it ended and the verified commit."""

    source: str
    stage: StageState
    sha: str
    tree: str
    ref: str


def simplify(request: Request) -> Simplified | None:
    """Returns a passed S0 of the same requested tree and base whose verified commit still exists.

    A simplified commit counts only when its ref still points at the recorded sha, that commit's
    tree is the recorded one and its parent is the requested sha. With nothing simplified the
    verified commit must be the requested one.
    """
    if not request.base:
        return None
    for source in sources(request):
        run = source.run
        stage = run.stages.get("simplify")
        if stage is None or stage.status != "passed":
            continue
        if run.simplified_ref:
            valid = (
                run.simplified_ref.startswith(git.SIMPLIFIED_PREFIX)
                and run.requested_sha == request.sha
                and git.ref_sha(request.repo, run.simplified_ref) == run.sha
                and git.tree_of(request.repo, run.sha) == run.tree
                and git.parent_of(request.repo, run.sha) == request.sha
            )
        else:
            valid = run.sha == request.sha and run.tree == request.tree
        if valid:
            return Simplified(run.id, stage, run.sha, run.tree, run.simplified_ref)
    return None


def stage(request: Request, name: str, tree: str, wanted: str) -> tuple[str, StageState] | None:
    """Returns the source run id and stage of a pass of `name` with the `wanted` fingerprint.

    Static, backend, frontend and Playwright also lend a failure: the same tree fails the same way.
    A failure is lent only by a run that ended failed: in a cancelled or interrupted run the
    failure may be the kill itself.

    `tree` is the verified tree of the run now. The QA pass is also bound to the run's cycle, and
    needs its walked flows on disk.
    """
    for source in sources(request):
        run = source.run
        held = run.stages.get(name)
        if held is None or held.status not in report.reuse_statuses(name) or run.tree != tree:
            continue
        if held.status == "failed" and run.status != "failed":
            continue
        if recorded(source, name) != wanted:
            continue
        if name == "qa" and (run.cycle != request.cycle or not paths.qa_file(run.id).is_file()):
            continue
        return run.id, held
    return None


def _verdicts(request: Request) -> list[RunState]:
    """Returns the earlier finished runs of the request's cycle whose QA reached a verdict.

    Newest first; a phase or a plain verify of the same repository and environment counts.
    """
    found = []
    for run in sorted(queue.list_runs(), key=lambda r: r.id, reverse=True):
        held = run.stages.get("qa")
        if (
            run.id < request.id
            and run.mode in phase_scope.QA_MODES
            and run.cycle == request.cycle
            and run.repo == request.repo
            and run.env == request.env
            and run.status in queue.FINISHED
            and held is not None
            and held.status in ("passed", "failed")
        ):
            found.append(run)
    return found


def qa_problems(source: RunState, request: Request, cfg: Config, new_plan: str) -> list[str]:
    """Returns why the QA of run `source` does not stand for `request`; empty when it does.

    The one decision behind both the choice of a QA to carry and the report's check of one carried:
    the QA must be the newest verdict of the cycle, have passed with no open P0 or P1, stand on a
    trusted run, have its walked flows on disk, have run under the same QA configuration and the
    same non-empty plan with at least the time budget asked now, and the commits since must touch
    nothing of the phase (`phase_scope`).
    """
    held = source.stages.get("qa")
    if held is None or held.status != "passed":
        return ["its QA did not pass"]
    if any(run.id > source.id for run in _verdicts(request)):
        return ["a newer run of the cycle reached a QA verdict"]
    origin = _source(source)
    if origin is None:
        return ["its request cannot be read"]
    if not report.trusted(source) or report.reuse_problems(source):
        return ["it is not a trusted run"]
    if not paths.qa_file(source.id).is_file():
        return ["its walked flows are missing"]
    if report.blocking_open(report.load_findings(source.id)):
        return ["it left open P0/P1 findings"]
    if origin.request.qa_scale < request.qa_scale:
        return ["it ran with a smaller time budget"]
    held_plan = phase_scope.explicit_plan(source.id)
    if fingerprint(origin.config, "qa", "", plan=held_plan) != fingerprint(
        cfg, "qa", "", plan=new_plan
    ):
        return ["it ran under another QA configuration or plan"]
    return phase_scope.carried_problems(
        source, origin.request.base, request.cycle, request.sha, new_plan
    )


def phase_qa(request: Request, cfg: Config, new_plan: str) -> tuple[str, StageState] | None:
    """Returns the run and QA stage of the same cycle on an earlier commit that still stand.

    The newest earlier run of the cycle whose QA reached a verdict decides and no older run is
    consulted: `qa_problems` says whether it stands. `new_plan` is the plan the operator gives
    this run.
    """
    verdicts = _verdicts(request)
    if not verdicts or qa_problems(verdicts[0], request, cfg, new_plan):
        return None
    return verdicts[0].id, verdicts[0].stages["qa"]


def reused(stage_state: StageState, source: str, wanted: str, now: str) -> StageState:
    """Returns a copy of a source stage as the reusing run records it."""
    return replace(
        stage_state,
        started=now,
        ended=now,
        fingerprint=wanted,
        reused_from=source,
        failures=list(stage_state.failures),
        commands=list(stage_state.commands),
        specs=list(stage_state.specs),
        flows=list(stage_state.flows),
    )


def _inside(path: Path, root: Path) -> Path:
    """Returns `path` resolved, raising ValueError unless it is `root` itself or lies inside it."""
    resolved = path.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"{path} resolves outside {root}")
    return resolved


def _copy_evidence(origin: Path, target: Path) -> None:
    """Copies the regular files under `origin` into `target`, refusing symlinks and escapes.

    Every directory and file is resolved and must stay inside its run's evidence directory; a
    symlink anywhere in the tree fails the copy, so nothing outside the source run is read and
    nothing outside the target run is written.
    """
    root, base = origin.resolve(strict=True), target.resolve(strict=True)
    for current, dirs, files in os.walk(origin, followlinks=False):
        here = Path(current)
        for name in (*dirs, *files):
            if (here / name).is_symlink():
                raise ValueError(f"symlink in the QA evidence: {here / name}")
        relative = _inside(here, root).relative_to(root)
        (base / relative).mkdir(exist_ok=True)
        for name in files:
            source = _inside(here / name, root)
            destination = base / relative / name
            if destination.is_symlink() or base not in destination.resolve().parents:
                raise ValueError(f"unsafe evidence destination: {destination}")
            shutil.copyfile(source, destination, follow_symlinks=False)


def adopt_qa(source: str, run_id: str) -> None:
    """Copies the QA pass of run `source` into run `run_id`: walked flows, findings and evidence.

    The paths inside qa.json and findings.json are rewritten from the source's run directory to
    this run's, so the evidence stays inside the run that reports it. Symlinks are refused and
    every path is resolved inside its run directory (see `_copy_evidence`); a violation raises
    ValueError and the stage is not reused.
    """
    old, new = str(paths.run_dir(source)), str(paths.run_dir(run_id))
    run_root, source_root = paths.run_dir(run_id).resolve(), paths.run_dir(source).resolve()
    target = paths.evidence_dir(run_id)
    if target.is_symlink():
        raise ValueError(f"{target} is a symlink")
    target.mkdir(exist_ok=True)
    _inside(target, run_root)
    origin = paths.evidence_dir(source)
    if origin.is_symlink():
        raise ValueError(f"{origin} is a symlink")
    if origin.is_dir():
        _inside(origin, source_root)
        _copy_evidence(origin, target)
    for name in (paths.qa_file, paths.findings_file):
        origin_file, destination = name(source), name(run_id)
        if origin_file.is_symlink() or destination.is_symlink():
            raise ValueError(f"{origin_file} or {destination} is a symlink")
        if origin_file.is_file():
            _inside(origin_file, source_root)
            destination.write_text(origin_file.read_text().replace(old, new))


def discard_qa(run_id: str) -> None:
    """Removes whatever a refused `adopt_qa` left in run `run_id`: evidence, flows and findings."""
    target = paths.evidence_dir(run_id)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    target.mkdir(exist_ok=True)
    for name in (paths.qa_file, paths.findings_file):
        name(run_id).unlink(missing_ok=True)
