"""The stage executor: builds the sandbox, walks the stages, tears down, writes the outcome."""

import os
import signal
import sys
import threading
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType

from qg import (
    admission,
    config,
    git,
    inplace,
    mutants,
    notify,
    ownership,
    paths,
    peaks,
    phase_scope,
    probe,
    proc,
    qa,
    queue,
    release,
    report,
    reuse,
    rules,
    sandbox,
    simplify,
    stages,
    topics,
    verifier,
)
from qg.config import Config
from qg.queue import Request, RunState, StageState
from qg.stages import StageResult
from qg.util import now_iso


def _terminate(signum: int, frame: FrameType | None) -> None:
    """SIGTERM and SIGINT handler: later signals are ignored and the first one raises Cancelled."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise proc.Cancelled


def _begin(run_id: str, name: str) -> None:
    """Marks stage `name` running; raises Cancelled when the operator asked to cancel the run."""

    def apply(state: RunState) -> None:
        if state.cancel_requested:
            raise proc.Cancelled
        state.stages[name].status = "running"
        state.stages[name].started = now_iso()
        state.stage = ", ".join(n for n, s in state.stages.items() if s.status == "running")

    queue.update(run_id, apply)


def _end(
    run_id: str,
    name: str,
    status: str,
    summary: str,
    result: StageResult | None,
    wanted: str = "",
) -> None:
    """Stores the outcome of stage `name`, with what its commands did when `result` is given.

    `wanted` is the fingerprint the stage ran with; a later run reuses a pass only on a match.
    """

    def apply(state: RunState) -> None:
        stage = state.stages[name]
        stage.fingerprint = wanted
        stage.status = status
        stage.ended = now_iso()
        stage.summary = summary
        if result is not None:
            stage.failures = result.failures
            stage.commands = result.commands
            stage.specs = result.specs
            stage.flows = result.flows
            stage.flakes = result.flakes

    _fail_fast(queue.update(run_id, apply), name)


def _fail_fast(state: RunState, name: str) -> None:
    """Cancels a release's queued phase runs once its stage `name` has ended failed.

    Covers the shared stages and S0, whether they ran or were reused. It is best-effort: a
    failure here is printed and never replaces the stage's own verdict. A release whose setup
    errors before any stage is not covered; its phase runs are left to the operator.
    """
    if state.mode != "release" or state.stages[name].status != "failed":
        return
    if name not in (*release.SHARED, "simplify"):
        return
    try:
        release.fail_fast(state, name)
    except Exception:
        traceback.print_exc()


def _end_qa(run_id: str, outcome: qa.QaOutcome, wanted: str) -> None:
    """Stores the outcome of the QA stage with its cost, turns and the flows it walked."""

    def apply(state: RunState) -> None:
        stage = state.stages["qa"]
        stage.fingerprint = wanted
        stage.status = outcome.status
        stage.ended = now_iso()
        stage.summary = outcome.summary
        stage.cost_usd = outcome.cost_usd
        stage.turns = outcome.turns
        stage.flows = outcome.flows

    queue.update(run_id, apply)


def _end_simplify(run_id: str, outcome: simplify.SimplifyOutcome) -> None:
    """Stores S0's outcome; a commit makes its sha and tree the ones every later stage verifies."""

    def apply(state: RunState) -> None:
        stage = state.stages["simplify"]
        stage.status = outcome.status
        stage.ended = now_iso()
        stage.summary = outcome.summary
        stage.cost_usd = outcome.cost_usd
        stage.turns = outcome.turns
        if outcome.sha and outcome.tree and outcome.ref:
            state.sha = outcome.sha
            state.tree = outcome.tree
            state.simplified_ref = outcome.ref

    _fail_fast(queue.update(run_id, apply), "simplify")


def _end_reused(
    run_id: str,
    name: str,
    source: str,
    held: StageState,
    wanted: str,
    simplified: reuse.Simplified | None = None,
) -> None:
    """Records stage `name` as passed by reuse of run `source`; S0 also carries its commit."""

    def apply(state: RunState) -> None:
        if state.cancel_requested:
            raise proc.Cancelled
        state.stages[name] = reuse.reused(held, source, wanted, now_iso())
        if simplified is not None and simplified.ref:
            state.sha = simplified.sha
            state.tree = simplified.tree
            state.simplified_ref = simplified.ref

    _fail_fast(queue.update(run_id, apply), name)


def _checkout(box: sandbox.Sandbox, sha: str) -> bool:
    """Moves the sandbox's clean checkout to commit `sha`; False when git refuses."""
    done = proc.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(box.src), "checkout", "--detach", sha]
    )
    return done.returncode == 0


def _skip(run_id: str, name: str, summary: str) -> None:
    """Marks stage `name` skipped with the reason."""

    def apply(state: RunState) -> None:
        state.stages[name].status = "skipped"
        state.stages[name].summary = summary
        state.stages[name].ended = now_iso()

    queue.update(run_id, apply)


def _assert_verified_tree(run_id: str, box: sandbox.Sandbox) -> None:
    """Raises when the sandbox's HEAD tree is not the tree the run's state says it verified."""
    head = proc.run(["git", "-C", str(box.src), "rev-parse", "HEAD^{tree}"]).stdout.strip()
    verified = queue.load(run_id).tree
    if head != verified:
        raise RuntimeError(f"the stages ran on tree {head} but the run verified {verified}")


@dataclass
class Ctx:
    """What every lane of one run shares: the request as asked, the one verified, the sandbox."""

    run_id: str
    asked: Request
    request: Request
    cfg: Config
    box: sandbox.Sandbox
    changed: list[str]
    nightly: bool


def _simplify(ctx: Ctx) -> tuple[Ctx, bool]:
    """Runs or reuses the optional S0; returns the context for the lanes and whether it failed."""
    run_id, cfg = ctx.run_id, ctx.cfg
    found = reuse.simplify(ctx.asked) if ctx.asked.reuse else None
    if found is not None and (not found.ref or _checkout(ctx.box, found.sha)):
        _end_reused(run_id, "simplify", found.source, found.stage, "", found)
        if not found.ref:
            return ctx, False
        request = replace(ctx.request, sha=found.sha, tree=found.tree)
        return replace(ctx, request=request, box=replace(ctx.box, sha=found.sha)), False
    _begin(run_id, "simplify")
    outcome = simplify.run_simplify(ctx.box, cfg, ctx.request)
    _end_simplify(run_id, outcome)
    if outcome.status == "failed":
        return ctx, True
    if outcome.sha and outcome.tree:
        request = replace(ctx.request, sha=outcome.sha, tree=outcome.tree)
        return replace(ctx, request=request, box=replace(ctx.box, sha=outcome.sha)), False
    return ctx, False


def _reusable(ctx: Ctx, name: str, wanted: str) -> tuple[str, StageState] | None:
    """Returns the source run and stage that pass `name` with the `wanted` fingerprint, if any."""
    if not ctx.asked.reuse:
        return None
    return reuse.stage(ctx.asked, name, ctx.request.tree, wanted)


def _carried_qa(ctx: Ctx) -> tuple[str, StageState] | None:
    """Returns the QA of this cycle that stands for a plain verification of exactly this tree."""
    asked = ctx.asked
    if not asked.reuse or asked.mode != "verify" or ctx.request.sha != asked.sha:
        return None
    found = reuse.phase_qa(asked, ctx.cfg, phase_scope.explicit_plan(ctx.run_id))
    if found is None or queue.load(found[0]).tree != ctx.request.tree:
        return None
    return found


def _limit(ctx: Ctx, name: str) -> str:
    """Returns the MemoryMax of the lane stage `name` runs in."""
    return ctx.cfg.resources.lanes[admission.STAGE_LANES[name]][1]


def _qa_stage(ctx: Ctx) -> None:
    """Runs the QA stage on its own ports and Postgres, or adopts a passed one it can prove."""
    plan = qa.plan_text(ctx.box, ctx.request.plan)
    scale = ctx.request.qa_scale
    wanted = reuse.fingerprint(ctx.cfg, "qa", ctx.request.tree, plan=plan, scale=scale)
    found = _reusable(ctx, "qa", wanted) or _carried_qa(ctx)
    if found is not None:
        try:
            reuse.adopt_qa(found[0], ctx.run_id)
        except ValueError as exc:
            print(f"qg: QA of run {found[0]} not reused: {exc}", file=sys.stderr)
            reuse.discard_qa(ctx.run_id)
        else:
            _end_reused(ctx.run_id, "qa", found[0], found[1], wanted)
            return
    admission.wait_for_memory(ctx.run_id, "qa", _limit(ctx, "qa"))
    sandbox.hold_services(ctx.run_id, ctx.cfg.services)
    _begin(ctx.run_id, "qa")
    outcome = qa.run_qa(ctx.box.for_qa(), ctx.cfg, ctx.request)
    probed = probe.probe_pass(ctx.box, ctx.cfg, plan)
    said = verifier.verify_run(
        ctx.run_id, ctx.box.src, plan, lambda argv: ctx.box.wrap("qa_agent", argv)
    )
    notes = [note for note in (probed, said) if note]
    if notes:
        outcome = replace(outcome, summary="; ".join([outcome.summary, *notes]))
    _end_qa(ctx.run_id, outcome, wanted)


def _command_stage(ctx: Ctx, name: str) -> None:
    """Runs one of static, backend, frontend or playwright, or reuses a pass it can prove."""
    cfg, box = ctx.cfg, ctx.box
    selection = None
    if name == "playwright":
        selection = stages.select_specs(
            cfg,
            ctx.request.repo,
            ctx.request.sha,
            lambda spec: (box.src / spec).is_file(),
            ctx.nightly,
        )
    elif name in ("backend", "frontend"):
        selection = stages.select_tests(
            cfg, name, ctx.changed, lambda path: (box.src / path).exists(), ctx.nightly
        )
    labels = tuple(selection.specs) if selection else ()
    specs = labels if name == "playwright" else ()
    selected = labels if name in ("backend", "frontend") else ()
    wanted = reuse.fingerprint(cfg, name, ctx.request.tree, specs=specs, selected=selected)
    found = _reusable(ctx, name, wanted)
    if found is not None:
        _end_reused(ctx.run_id, name, found[0], found[1], wanted)
        return
    admission.wait_for_memory(ctx.run_id, name, _limit(ctx, name))
    if name == "playwright":
        sandbox.hold_services(ctx.run_id, cfg.services)
        with admission.playwright_turn(ctx.run_id):
            _run_command_stage(ctx, name, selection, wanted)
    else:
        _run_command_stage(ctx, name, selection, wanted)


def _run_command_stage(
    ctx: Ctx, name: str, selection: stages.Selection | None, wanted: str
) -> None:
    """Marks the stage running, runs its commands and stores the outcome."""
    _begin(ctx.run_id, name)
    if name == "playwright" and ctx.request.stress > 1:
        result = stages.run_stress(ctx.box, ctx.cfg, selection, ctx.request.stress)
    else:
        result = stages.run_stage(ctx.box, ctx.cfg, name, selection)
    if name == "static" and ctx.cfg.rules is not None:
        seconds = ctx.cfg.timeouts["static"] * 60
        result = rules.in_stage(ctx.box, ctx.cfg.rules, ctx.request.base, seconds, result)
    hunt = name == "playwright" and ctx.request.mode == "release" and ctx.request.stress == 1
    if hunt and result.status == "failed" and result.failures:
        result = stages.flake_hunt(ctx.box, ctx.cfg, result)
    if name == "backend" and result.status == "passed":
        note = _mutant_note(ctx)
        result = replace(result, summary=f"{result.summary}; {note}") if note else result
    _end(ctx.run_id, name, result.status, result.summary, result, wanted)


def _mutant_note(ctx: Ctx) -> str:
    """Returns the mutant hunt's note for a passing backend stage. The hunt only informs, so a
    crash of it becomes the note and never the stage's verdict."""
    try:
        return mutants.hunt(ctx.box, ctx.cfg)
    except proc.Cancelled:
        raise
    except (Exception, SystemExit) as exc:
        return f"mutants: could not be tried ({type(exc).__name__}: {exc})"


def _attribute(ctx: Ctx) -> None:
    """Records, on a release run's failed Playwright stage, which phase owns each failing test."""
    held = queue.load(ctx.run_id).stages["playwright"]
    if ctx.request.mode != "release" or held.status != "failed" or not held.failures:
        return
    phases = [release.split_phase(text) for text in ctx.request.phases]
    scenarios = ownership.scenario_tags(ctx.request.repo, ctx.request.sha)
    owners = ownership.assign(held.failures, scenarios, phases)
    queue.update(ctx.run_id, lambda state: setattr(state.stages["playwright"], "owners", owners))


def _run_stage(ctx: Ctx, name: str) -> None:
    """Runs one stage, recording a crash of the stage's own machinery as that stage failing.

    A cancellation and an infrastructure failure (qg.proc) pass through; any other error (a setup
    step that exits the process, a missing file) fails the stage with its message, so the lane's
    other stages still run.
    """
    try:
        if name == "qa":
            _qa_stage(ctx)
        else:
            _command_stage(ctx, name)
            if name == "playwright":
                _attribute(ctx)
    except (proc.Cancelled, proc.Infrastructure):
        raise
    except (Exception, SystemExit) as exc:
        _end(ctx.run_id, name, "failed", f"stage crashed: {exc or repr(exc)}", None)


def _await_lane(run_id: str, event: threading.Event) -> None:
    """Blocks until `event` is set; raises Cancelled on a cancelled run or a stopping executor."""
    key = "qa lane"
    while not event.wait(admission.POLL_SECONDS):
        if admission.STOP.is_set() or queue.load(run_id).cancel_requested:
            queue.set_waiting(run_id, key, None)
            raise proc.Cancelled
        queue.set_waiting(run_id, key, "waiting for the tests lane: one stack at a time")
    queue.set_waiting(run_id, key, None)


def _lane(
    ctx: Ctx,
    names: list[str],
    ended: list[BaseException],
    after: threading.Event | None = None,
    done: threading.Event | None = None,
) -> None:
    """Runs a lane's stages in order on its own thread; a cancellation or an infrastructure
    failure stops the lane and is handed to the caller in `ended`.

    A lane given `after` starts once that event is set; a lane given `done` sets it when it ends,
    however it ends.
    """
    try:
        if after is not None:
            _await_lane(ctx.run_id, after)
        for name in names:
            _run_stage(ctx, name)
    except (proc.Cancelled, proc.Infrastructure) as exc:
        ended.append(exc)
    finally:
        if done is not None:
            done.set()


def _run_lanes(ctx: Ctx, lanes: dict[str, list[str]]) -> None:
    """Runs the lanes, each stage in its lane's cgroup, and waits for all of them.

    Static and the tests lane start together; the tests lane runs backend, frontend and
    Playwright one after another, and the QA lane starts when the tests lane has ended, so a run
    never holds the Playwright stack and the QA stack (two app stacks, two browsers) at once. A
    failed stage never stops another lane or another stage of its lane, so one run reports
    everything that is wrong. A cancellation stops the lane scopes, waits for the threads and is
    raised again; an infrastructure failure that ended a lane is raised once every lane ended.
    """
    ended: list[BaseException] = []
    tests_done = threading.Event()
    if "tests" not in lanes:
        tests_done.set()
    threads = [
        threading.Thread(
            target=_lane,
            args=(ctx, names, ended),
            kwargs={
                **({"done": tests_done} if lane == "tests" else {}),
                **({"after": tests_done} if lane == "qa" else {}),
            },
            name=f"lane-{lane}",
        )
        for lane, names in lanes.items()
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
    except BaseException:
        admission.STOP.set()
        proc.stop_lanes(ctx.run_id)
        for thread in threads:
            thread.join(60)
        raise
    if ended:
        raise next((exc for exc in ended if isinstance(exc, proc.Cancelled)), ended[0])


def adopt_phase(run_id: str, request: Request, cfg: Config, found: tuple[str, StageState]) -> bool:
    """Records phase run `run_id` as passed by the QA of run `found[0]`, with no sandbox.

    Returns whether the QA was adopted; one that cannot be is discarded and nothing is recorded.
    """
    plan = Path(request.plan).read_text(errors="replace") if Path(request.plan).is_file() else ""
    wanted = reuse.fingerprint(cfg, "qa", request.tree, plan=plan, scale=request.qa_scale)
    try:
        reuse.adopt_qa(found[0], run_id)
    except ValueError as exc:
        print(f"qg: QA of run {found[0]} not reused: {exc}", file=sys.stderr)
        reuse.discard_qa(run_id)
        return False
    _end(run_id, "setup", "passed", f"no sandbox: QA reused from run {found[0]}", None)
    held = replace(found[1], summary=f"QA reused from run {found[0]}")
    _end_reused(run_id, "qa", found[0], held, wanted)
    return True


def settle_phase(run_id: str, request: Request, cfg: Config, found: tuple[str, StageState]) -> bool:
    """Finishes a freshly enqueued phase run whose QA stands, so it never waits in the queue."""
    if not adopt_phase(run_id, request, cfg, found):
        return False
    queue.update(run_id, lambda state: setattr(state, "started", now_iso()))
    finalize(run_id, None, True)
    return True


def _reuse_phase(run_id: str, request: Request, cfg: Config) -> bool:
    """Ends a phase run at once when its QA on an earlier commit still stands.

    Returns whether the run is complete; a QA that cannot be adopted is discarded and the run
    goes on as if nothing had been found.
    """
    if request.mode != "phase" or not request.reuse:
        return False
    found = reuse.phase_qa(request, cfg, phase_scope.explicit_plan(request.id))
    return found is not None and adopt_phase(run_id, request, cfg, found)


def _walk(run_id: str, request: Request, cfg: Config) -> None:
    """Creates the sandbox, then runs the optional S0 alone and the lanes (QA after the tests).

    A stage an earlier run of the same requested tree and base passed is reused, never run
    again, when `request.reuse` allows it and `reuse` can prove it identical (qg/reuse.py).
    """
    if _reuse_phase(run_id, request, cfg):
        return
    _begin(run_id, "setup")
    box = sandbox.create(request, cfg)
    _end(run_id, "setup", "passed", "; ".join(box.notes), None)
    ctx = Ctx(
        run_id,
        request,
        request,
        cfg,
        box,
        git.change_paths(request.repo, request.base, request.sha),
        request.mode == "nightly",
    )
    names = [name for name in request.stage_set if name != "simplify"]
    lanes = cfg.lane_stages(names)
    if "simplify" in request.stage_set:
        ctx, failed = _simplify(ctx)
        if failed:
            for name in names:
                _skip(run_id, name, "not run: simplify failed")
            lanes = {}
    _run_lanes(ctx, lanes)
    _assert_verified_tree(run_id, ctx.box)
    inplace.verify_unchanged(run_id)


def _outcome(state: RunState, exc: BaseException | None) -> tuple[str, str | None]:
    """Returns the final run status and error text for the exception that ended the run, if any.

    An infrastructure failure (qg.proc) ends the run `error` naming its cause, or `failed` when a
    stage had already failed on the code.
    """
    if exc is None:
        broken = any(stage.status == "failed" for stage in state.stages.values())
        return ("failed" if broken else "passed"), None
    if isinstance(exc, proc.Cancelled):
        if state.cancel_requested:
            return "cancelled", "cancelled by the operator"
        if state.deadline_exceeded:
            return "interrupted", f"run exceeded its deadline ({state.deadline})"
        return "interrupted", "terminated by a signal"
    if isinstance(exc, proc.Infrastructure):
        broken = any(stage.status == "failed" for stage in state.stages.values())
        return ("failed" if broken else "error"), f"infrastructure, not the code: {exc}"
    if isinstance(exc, inplace.TreeChanged):
        return "failed", str(exc)
    if isinstance(exc, proc.Interrupted):
        return "interrupted", str(exc)
    return "error", str(exc) or repr(exc)


def retry(state: RunState, request: Request, status: str, exc: BaseException | None) -> str | None:
    """Queues the one automatic retry of a verification a stack failure ended `error`; returns its
    id, None when there is none.

    Only a first attempt (no `retest_of`) is retried, and only while no newer run of its line
    (repo, branch, cycle) exists: enqueuing would supersede it. The retry asks the same as a
    retest of `state`, so it starts at once and reuses what passed (qg/reuse.py). A phase run
    has its own retry (`release.retry`); an account failure is never retried.
    """
    if status != "error" or not isinstance(exc, proc.StackFailure):
        return None
    if request.mode != "verify" or request.retest_of:
        return None
    with queue.locked():
        newer = any(
            run.id > state.id
            and (run.repo, run.branch, run.cycle) == (state.repo, state.branch, state.cycle)
            for run in queue.list_runs()
        )
        if newer:
            return None
        run_id = paths.new_run_id(state.id)
        plan = paths.plan_file(state.id)
        own = request.plan == str(plan)
        again = replace(
            request,
            id=run_id,
            created=now_iso(),
            retest_of=state.id,
            plan=str(paths.plan_file(run_id)) if own else request.plan,
        )
        queue.enqueue(again, plan.read_text() if plan.is_file() else None)
    return run_id


def finalize(run_id: str, exc: BaseException | None, clean: bool) -> None:
    """Writes the run's final status, report and cycle page; `exc` is what ended the run, if any.

    A verification a stack failure ended `error` is queued again once (see `retry`); its error
    names the retry and only the retry's end is announced. A phase run whose QA walked nothing,
    unless the Claude account stopped it, queues its one automatic retry (see `release.retry`)
    before its release is judged again, so the release waits for it.
    """
    request = queue.load_request(run_id)
    with queue.locked():
        state = queue.load(run_id)
        status, error = _outcome(state, exc)
        again = retry(state, request, status, exc)
        if again:
            error = f"{error}; queued again as run {again}"
        now = now_iso()
        broken = next((n for n, s in state.stages.items() if s.status == "failed"), None)
        for stage in state.stages.values():
            if stage.status == "running":
                stage.status = "failed"
                stage.ended = now
                stage.summary = error or status
            elif stage.status == "pending":
                stage.status = "skipped"
                stage.summary = f"not run: {broken} failed" if broken else f"not run: run {status}"
        state.status = status
        state.error = error
        state.ended = now
        state.stage = None
        state.slot = None
        state.waiting = {}
        state.torn_down = clean
        queue.save(state)
        report.write(state, request)
    if not isinstance(exc, proc.AccountFailure) and (retried := release.retry(state, request)):
        print(f"qg: phase run {run_id} walked nothing: queued its retry {retried}", file=sys.stderr)
    for other in release.sync(run_id):
        report.refresh(other)
    topics.refresh(run_id)
    if again:
        print(f"qg: run {run_id} hit a stack failure: queued again as {again}", file=sys.stderr)
    else:
        notify.finished(run_id)


def abandon(run_id: str, reason: str) -> None:
    """Finishes a run whose executor is gone: tears its sandbox down and writes its final state."""
    state = queue.load(run_id)
    if state.cancel_requested:
        exc: BaseException = proc.Cancelled()
    elif state.deadline_exceeded:
        exc = proc.Interrupted(f"run exceeded its deadline ({state.deadline})")
    else:
        exc = proc.Interrupted(reason)
    clean = sandbox.teardown(run_id, state.repo) if state.sandbox_created else True
    finalize(run_id, exc, clean)


def _record_pid(state: RunState) -> None:
    """Stores this executor's pid so the runner can tell whether it is still alive."""
    state.executor_pid = os.getpid()


def execute(run_id: str) -> int:
    """Runs the whole verification of `run_id`; the outcome lands in its state, the exit is 0."""
    queue.update(run_id, _record_pid)
    request = queue.load_request(run_id)
    cfg = config.parse(request.config, f"request of run {run_id}")
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    stop_peaks = threading.Event()
    watcher = peaks.watch(run_id, stop_peaks)
    try:
        _walk(run_id, request, cfg)
    finally:
        exc = sys.exception()
        stop_peaks.set()
        watcher.join()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        clean = True
        try:
            if queue.load(run_id).sandbox_created:
                clean = sandbox.teardown(run_id, request.repo)
        finally:
            finalize(run_id, exc, clean)
    return 0
