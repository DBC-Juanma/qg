"""Command line of qg."""

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from qg import (
    admission,
    audit,
    backlog,
    config,
    cycle_page,
    cycles,
    deployed,
    estimate,
    executor,
    git,
    init,
    inplace,
    lite,
    notify,
    paths,
    phase_scope,
    plan,
    proc,
    prune,
    qa,
    queue,
    release,
    report,
    rules,
    runner,
    sandbox,
    settings,
    shadow_report,
    shim,
    simplify,
    stages,
    topics,
    verifier,
)
from qg import agent as agents
from qg.queue import Request, RunState
from qg.reuse import phase_qa
from qg.util import ago, atomic_write, die, elapsed, fill, human, now_iso

POLL_SECONDS = 5
MIN_FREE_PORTS = 30
EXAMPLE_PORTS = {
    "backend": 21001,
    "frontend": 21002,
    "pg": 21003,
    "qa_backend": 21004,
    "qa_frontend": 21005,
    "qa_pg": 21006,
}
CANCEL_WAIT_SECONDS = 120


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Formats rows as a plain left-aligned text table."""
    widths = [max([len(h), *(len(row[i]) for row in rows)]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _plan_path(plan: str | None) -> str:
    """Returns the absolute path of the --plan file, exiting when it does not exist."""
    if not plan:
        return ""
    resolved = Path(plan).expanduser().resolve()
    if not resolved.is_file():
        die(f"--plan {plan!r} is not a file")
    return str(resolved)


def _plan_text(repo: str, sha: str, plan_path: str) -> str:
    """Reads the plan as QA does: the committed copy when inside the repository, else the file."""
    path = Path(plan_path)
    if path.is_relative_to(repo):
        committed = git.show(repo, sha, str(path.relative_to(repo)))
        if committed is not None:
            return committed
    return path.read_text(errors="replace")


def _refuse_unwalkable(plan_text: str, source: str, allow_manual: bool) -> None:
    """Exits when the plan has topics the QA agent cannot walk, unless `allow_manual`."""
    found = [] if allow_manual else plan.unwalkable_topics(plan_text)
    if found:
        listed = "\n".join(f"  - {title}" for title in found)
        die(
            f"{source} has topics the QA agent cannot walk (manual, operator or real account); "
            "they would end the run NOT READY. Rewrite them as walkable (a marker word in a title "
            "or a bold span counts, even in a walkable topic) or pass "
            f"--allow-manual:\n{listed}"
        )


def _eta(ahead: int, hold: int = 0) -> str:
    """Estimates the end of a run with `ahead` queued runs in front that cannot start for `hold`
    seconds (see `queue.hold_left`), from the last five runs."""
    runs = queue.list_runs()
    finished = [r for r in runs if r.status in ("passed", "failed")][-5:]
    durations = [d for d in (elapsed(r.started, r.ended) for r in finished) if d]
    if not durations:
        return "unknown (no finished runs yet)"
    mean = sum(durations) / len(durations)
    running = [r for r in runs if r.status == "running"]
    left = sum(max(mean - (elapsed(r.started, None) or 0), 0) for r in running)
    total = human(max(left + ahead * mean, hold) + mean)
    return f"about {total} (mean of the last {len(durations)} runs: {human(mean)})"


def _example_sandbox(repo: str, sha: str, cfg: config.Config) -> sandbox.Sandbox:
    """Builds a sandbox of example values so a dry run can show filled commands."""
    example = "<run-id>"
    return sandbox.Sandbox(
        run_id=example,
        repo=repo,
        sha=sha,
        src=paths.src_dir(example),
        run_dir=paths.run_dir(example),
        venv=paths.VENVS / "<uv.lock hash>",
        db=cfg.postgres.db,
        ports=EXAMPLE_PORTS,
    )


def _print_qa_plan(box: sandbox.Sandbox, cfg: config.Config) -> None:
    """Prints how S5 would boot the stack and run the QA agent, with example values."""
    q = cfg.qa
    assert q is not None
    limits = f"agent {q.time_limit_minutes} min, {q.max_turns} turns, ${q.max_cost_usd:g} cap"
    print(f"\nqa (stage timeout {cfg.timeouts['qa']} min, grown with the plan; {limits})")
    marker = f"{q.ready_marker} and " if q.ready_marker else ""
    print(f"  boot (ready when {marker}{fill(q.ready_url, box.values)} answers, 10 min):")
    print(f"    {fill(q.boot, box.values)}")
    keys = ", ".join(q.secrets_keys) or "none"
    print(f"  secrets read from {q.secrets_env_file or 'nothing'}: {keys}")
    print(f"  app {fill(q.app_url, box.values)}   api {fill(q.api_url, box.values)}")
    print(f"  widths {', '.join(map(str, qa.WIDTHS))}   locales {', '.join(qa.LOCALES)}")
    qa_dir = box.run_dir / "qa"
    print("  mcp config (qa/mcp.json):")
    origins = qa.allowed_origins(q, box.values)
    mcp = qa.mcp_config(box.run_dir / "evidence", "npx", origins)
    for line in json.dumps(mcp, indent=2).splitlines():
        print(f"    {line}")
    prompt = "<qg/qa_prompt.md with app, api and turn cap filled>"
    command = qa.claude_command(
        Path("claude"), prompt, qa_dir / "mcp.json", q.max_turns, q.max_cost_usd
    )
    print(f"  agent (cwd {qa_dir}, SIGTERM then SIGKILL after 30 s):")
    print(f"    {shlex.join(command)}")
    print("  oracles: tracebacks, ERROR lines and HTTP 5xx in qa-stack.log during the agent run")


def _print_simplify_plan(box: sandbox.Sandbox, cfg: config.Config) -> None:
    """Prints how S0 would run the /simplify agent and what it does with the result."""
    s = cfg.simplify
    limits = f"agent {s.time_limit_minutes} min, {s.max_turns} turns, ${s.max_cost_usd:g} cap"
    print(f"\nsimplify (stage timeout {cfg.timeouts['simplify']} min; {limits})")
    print("  runs only with a base; without one the stage is skipped and the run cannot be READY")
    print(f"  diff {box.sha[:12]} against the base is written to {box.run_dir}/simplify/diff.patch")
    prompt = "<qg/simplify_prompt.md with the base, sha, diffstat and files filled>"
    command = agents.claude_command(
        Path("claude"),
        prompt,
        s.max_turns,
        s.max_cost_usd,
        box.run_dir / "simplify",
        simplify.TOOLS,
    )
    print(f"  agent (cwd {box.src}, jailed like S5, no shell, no browser):")
    print(f"    {shlex.join(command)}")
    print("  no changes: the stage passes with 'nothing to simplify', sha and tree unchanged")
    print(
        f"  changes: one commit 'simplify: ...' on top of the sha, "
        f"created as {git.SIMPLIFIED_PREFIX}<run-id>"
    )
    print("  every later stage then runs on that commit; qg check passes only after a fast-forward")


def _print_stage_plan(
    name: str,
    box: sandbox.Sandbox,
    cfg: config.Config,
    repo: str,
    sha: str,
    changed: list[str],
    nightly: bool,
) -> None:
    """Prints the commands of one stage with example values, and the specs S4 would run."""
    if name == "qa":
        _print_qa_plan(box.for_qa(), cfg)
        return
    if name == "simplify":
        _print_simplify_plan(box, cfg)
        return
    print(f"\n{name} (timeout {cfg.timeouts[name]} min)")
    specs = ""
    if name == "playwright":
        selection = stages.select_specs(
            cfg, repo, sha, lambda spec: git.exists_in_tree(repo, sha, spec)
        )
        specs = " ".join(selection.specs)
        print(f"  specs ({len(selection.specs)}): {specs or 'none'}")
        for flow in selection.flows:
            print(f"  flow: {flow}")
        if not selection.specs:
            print("  no specs: the stage would be skipped")
    templates = (cfg.playwright.run,) if name == "playwright" else cfg.commands(name)
    chosen = stages.select_tests(
        cfg, name, changed, lambda path: git.exists_in_tree(repo, sha, path), nightly
    )
    if chosen is not None:
        print(f"  affected: {'; '.join(chosen.specs) or 'nothing selected'}")
        for note in chosen.notes:
            print(f"  note: {note}")
        templates = (*templates, *chosen.commands)
    for index, template in enumerate(templates, start=1):
        print(f"  {index}. {fill(template, {**box.values, 'specs': specs})}")


def _print_plan(
    *,
    repo: str,
    branch: str,
    sha: str,
    tree: str,
    feature: str,
    cycle: str,
    env: str,
    stage_set: list[str],
    cfg: config.Config,
    base: str,
    base_source: str,
    plan_text: str | None,
    changed: list[str],
    found: estimate.Estimate,
    nightly: bool,
) -> None:
    """Prints what a verification would do with example values; creates nothing."""
    box = _example_sandbox(repo, sha, cfg)
    known = cycles.load(cycle) if cycles.exists(cycle) else None
    print("dry run: nothing is queued and nothing is created")
    print(f"repo      {repo}")
    print(f"branch    {branch}")
    print(f"commit    {sha} (tree {tree})")
    print(f"env       {env}: {', '.join(stage_set)}")
    if base:
        print(f"base      {base} ({base_source})")
    else:
        print(
            "base      none: the generated plan is skipped; "
            "prod refuses to run without a base or a plan"
        )
    print(f"feature   {feature}")
    print(f"cycle     {cycle} ({f'existing, {len(known.runs)} run(s) so far' if known else 'new'})")
    for line in estimate.describe(found, stage_set):
        print(line)
    print("graph     setup, then at the same time:")
    for lane, names in cfg.lane_stages(stage_set).items():
        print(f"            {lane}: {' -> '.join(names)}")
    limits = ", ".join(f"{k} {c}/{m}" for k, (c, m) in cfg.resources.lanes.items())
    executor_limit = f"{cfg.resources.cpu_quota}/{cfg.resources.memory_max}"
    print(f"limits    executor {executor_limit}; cgroups {limits}")
    print("timeouts  " + ", ".join(f"{k} {v} min" for k, v in cfg.timeouts.items()))
    if plan_text is not None:
        print("\nplan (written to runs/<run-id>/plan.md and handed to the S5 charter)")
        for line in plan_text.splitlines():
            print(f"  {line}")
    print("\nexample placeholder values")
    for name, value in box.values.items():
        print(f"  {{{name}}} = {value}")
    print("\nsetup")
    print(f"  git worktree add --detach {box.src} {sha}")
    print(f"  python  {fill(cfg.deps.python_sync, box.values)}")
    for package in cfg.deps.node_packages:
        copy = f"<temporary copy of {package}>"
        print(f"  node    {package}: {fill(cfg.deps.node_install, {**box.values, 'pkg': copy})}")
    print(
        f"  docker run -d --name qg-<run-id>-pg --tmpfs /var/lib/postgresql/data "
        f"{cfg.postgres.image}  (the QA lane starts its own, qg-<run-id>-qapg, while tests run)"
    )
    for name in stage_set:
        _print_stage_plan(name, box, cfg, repo, sha, changed, nightly)
    if cfg.teardown:
        print("\nteardown (after the stacks stop, before the Postgres container is removed)")
        for index, template in enumerate(cfg.teardown, start=1):
            print(f"  {index}. {fill(template, box.values)}")


def _qa_scale(cfg: config.Config, plan_text: str | None) -> float:
    """Returns the QA budget scale of a plan; 1 when the configuration has no QA stage."""
    return plan.scale(plan_text or "", cfg.qa.time_limit_minutes) if cfg.qa else 1.0


def _resolve_base(repo: str, env: str, base_arg: str | None, sha: str) -> tuple[str, str]:
    """Returns the base sha and its source: --base, else the last-deploy record, else none.

    A `--base` must be an ancestor of the commit and may not be newer than the last deploy to
    `env` (when that deploy is in the commit's history): a newer base shrinks the plan and the
    tests the change is judged by.
    """
    known = deployed.get(repo, env)
    if base_arg:
        base = git.resolve_commit(repo, base_arg)
        if not git.is_ancestor(repo, base, sha):
            die(f"--base {base[:12]} is not an ancestor of {sha[:12]}")
        last = git.resolve_commit(repo, known["sha"]) if known else ""
        if last and git.is_ancestor(repo, last, sha) and not git.is_ancestor(repo, base, last):
            die(f"--base {base[:12]} is newer than the last deploy to {env} ({last[:12]})")
        return base, "--base"
    if known:
        return git.resolve_commit(repo, known["sha"]), f"the last deploy to {env} ({known['at']})"
    return "", ""


def _submit(
    *,
    repo: str,
    sha: str,
    branch: str,
    feature: str,
    plan_path: str,
    env: str,
    qa: bool,
    force: bool,
    dry_run: bool,
    retest_of: str | None,
    base_arg: str | None = None,
    reuse: bool = True,
    plan_override: str | None = None,
    simplify_stage: bool = False,
    nightly: bool = False,
    reopen: str | None = None,
    prd: str = "",
    phase: str = "",
    given_cycle: str | None = None,
    run_mode: str = "",
    release: str = "",
    phases: list[str] | None = None,
    only_stages: list[str] | None = None,
    out: list[str] | None = None,
    stress: int = 1,
    worktree: str = "",
    allow_manual: bool = False,
) -> int:
    """Resolves a verification, then prints its plan, reports it already holds, or queues it.

    `nightly` queues the whole suite of the commit (every test, no QA, no base): it never counts
    for a deploy, never supersedes a verification and is cancelled when one waits. A release
    run (`run_mode` release) has the shared stages and lists its `phases`; a phase run
    (`run_mode` phase, `only_stages` ["qa"]) is the QA of one phase of release `release`. The
    id of the queued run is appended to `out`.
    """
    feature = " ".join(feature.split())
    if not feature:
        die("--feature must name the feature being verified")
    tree = git.tree_of(repo, sha)
    cycle_id, prd, phase = _resolve_cycle(repo, branch, feature, prd, phase, given_cycle)
    if not nightly:
        _guard_closed(cycle_id, feature, reopen, dry_run)
    loaded = config.load(repo, sha)
    raw, cfg = loaded.raw, loaded.config
    policy = cfg.policy(env)
    stage_set = list(cfg.stage_set(env, qa and not nightly, simplify_stage))
    if only_stages is not None:
        stage_set = [name for name in stage_set if name in only_stages]
    if nightly:
        stage_set = [name for name in stage_set if name != "qa"]
        base, plan_path = base_arg or "", ""
        base_source = f"the nightly simplify since {base[:12]}" if base else ""
    else:
        base, base_source = _resolve_base(repo, env, base_arg, sha)
    if not nightly and env == "prod" and not base and not plan_path and "qa" in stage_set:
        die(
            f"prod has no last-deploy record for {repo}: pass --base <sha> or --plan PATH "
            "(qg never runs prod with an empty plan)"
        )
    explicit = _plan_text(repo, sha, plan_path) if plan_path else None
    if explicit is not None:
        _refuse_unwalkable(explicit, f"plan {plan_path}", allow_manual)
    plan_text = plan_override
    if plan_text is None:
        plan_text = plan.generate(cfg, repo, base, sha, explicit, base_source) if base else explicit
    covering = None if force or nightly else report.covering_run(repo, tree, stage_set, policy)
    changed = git.change_paths(repo, base, sha)
    found = estimate.estimate(cfg, stage_set, _qa_scale(cfg, plan_text), queue.list_runs())
    if dry_run:
        _print_plan(
            repo=repo,
            branch=branch,
            sha=sha,
            tree=tree,
            feature=feature,
            cycle=cycle_id,
            env=env,
            stage_set=stage_set,
            cfg=cfg,
            base=base,
            base_source=base_source,
            plan_text=plan_text,
            changed=changed,
            found=found,
            nightly=nightly,
        )
        print(f"config    {loaded.ref} @ {loaded.ref_sha[:12]} (protected ref)")
        if loaded.branch_differs:
            print(f"note: {config.VERIFY_PATH} changed in this branch: not used")
        if covering:
            print(f"\nnote: run {covering.id} already passed these stages for this tree")
        return 0
    if covering:
        stages_text = ", ".join(stage_set)
        print(f"already verified: run {covering.id} passed {stages_text} on tree {tree[:12]}")
        print(f"report: {paths.report_md(covering.id)}")
        return 0
    if "playwright" in stage_set:
        stages.select_specs(cfg, repo, sha, lambda spec: git.exists_in_tree(repo, sha, spec))
    run_id = paths.new_run_id(release)
    request = Request(
        id=run_id,
        repo=repo,
        branch=branch,
        sha=sha,
        tree=tree,
        feature=feature,
        plan=str(paths.plan_file(run_id)) if base else plan_path,
        env=env,
        qa="qa" in stage_set,
        retest_of=retest_of,
        stage_set=stage_set,
        config=raw,
        cycle=cycle_id,
        prd=prd,
        phase=phase,
        created=now_iso(),
        config_ref=loaded.ref,
        config_sha=loaded.ref_sha,
        config_branch_differs=loaded.branch_differs,
        base=base,
        qa_scale=_qa_scale(cfg, plan_text),
        explicit_plan=plan_path,
        reuse=reuse,
        mode=run_mode or ("nightly" if nightly else "verify"),
        release=release,
        phases=list(phases or []),
        stress=stress,
        worktree=worktree,
    )
    with queue.locked():
        state, superseded = queue.enqueue(
            request, plan_text if base else phase_scope.snapshot(explicit) if explicit else None
        )
        carried = None
        if run_mode == "phase" and reuse and "qa" in stage_set:
            carried = phase_qa(request, cfg, (explicit or "").strip())
        if carried and executor.settle_phase(run_id, request, cfg, carried):
            print(f"run {run_id}: QA reused from run {carried[0]}; not queued")
            if out is not None:
                out.append(run_id)
            return 0
    if out is not None:
        out.append(run_id)
    ahead, running = queue.position(state)
    hold = queue.hold_left(state, datetime.now(UTC))
    print(f"queued run {state.id}")
    print(f"  repo      {repo}")
    print(f"  branch    {branch}   commit {sha[:12]}   env {env}")
    print(f"  stages    {', '.join(stage_set)}")
    print(
        f"  base      {f'{base} ({base_source})' if base else 'none: S0 and the plan are skipped'}"
    )
    print(f"  config    {loaded.ref} @ {loaded.ref_sha[:12]}")
    if loaded.branch_differs:
        print(f"  note      {config.VERIFY_PATH} changed in this branch: not used")
    print(f"  cycle     {state.cycle}  (run {len(cycles.load(state.cycle).runs)} of this feature)")
    print(f"  position  {ahead + 1} in the queue ({ahead} ahead, {running} running)")
    if hold:
        print(f"  hold      {queue.hold_text(hold)}")
    for line in estimate.describe(found, stage_set):
        print(f"  {line}")
    print(f"  eta       {_eta(ahead, hold)}")
    for old in superseded:
        print(f"  superseded queued run {old}")
    if not paths.is_enabled():
        print("  note      paused: the operator has not activated qg (qg enable); the run waits")
    print(f"follow it with: qg wait {state.id}")
    return 0


def _resolve_cycle(
    repo: str,
    branch: str,
    feature: str,
    prd: str,
    phase: str,
    given: str | None,
) -> tuple[str, str, str]:
    """Returns the cycle id, PRD and phase a run belongs to.

    A phase cycle is a PRD phase of a repository, never a branch or a feature text. A run
    without --prd and --phase (a run between phase closes, or the nightly) joins the cycle of its
    feature name, opening it when there is none.
    """
    if given is not None:
        held = cycles.load(given)
        return held.id, held.prd, held.phase
    if bool(prd) != bool(phase):
        die("--prd and --phase go together")
    if prd:
        return cycles.identify_phase(repo, prd, phase), prd, phase
    legacy = cycles.identify(repo, branch, feature)
    if cycles.exists(legacy):
        held = cycles.load(legacy)
        return held.id, held.prd, held.phase
    return legacy, "", ""


def _guard_closed(ident: str, feature: str, reopen: str | None, dry: bool) -> None:
    """Refuses a verification of a feature whose cycle was closed by a deploy, unless reopened.

    A closed cycle is a finished phase: more work is a new phase under a new --feature name.
    --reopen "<reason>" is only for a deploy that was rolled back, and it is recorded.
    """
    if not cycles.exists(ident):
        return
    known = cycles.load(ident)
    if known.closed_at is None:
        if reopen:
            die(f"cycle {ident} is not closed; --reopen applies only to a closed cycle")
        return
    if not reopen:
        die(
            f"cycle {ident} was closed when {known.closed_sha[:12]} was deployed to "
            f"{known.closed_env}. Open a new phase with a new --feature name "
            f'(for example "{feature.split(" · ")[0]}.1 · ..."); if the deploy was rolled back, '
            'pass --reopen "<reason>"'
        )
    if not dry:
        cycles.reopen(ident, reopen)


def _phase_plans(plan_dir: str, phases: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Returns the plan file of every phase: the file of `plan_dir` named like `F01-P1`."""
    root = Path(plan_dir).expanduser().resolve()
    if not root.is_dir():
        die(f"--plan-dir {plan_dir!r} is not a directory")
    found: dict[tuple[str, str], str] = {}
    for prd, phase in phases:
        wanted = f"{prd}-{phase}".casefold()
        matches = [f for f in sorted(root.iterdir()) if f.is_file() and f.stem.casefold() == wanted]
        if not matches:
            die(f"--plan-dir {plan_dir!r} has no plan named {prd}-{phase}, e.g. {prd}-{phase}.md")
        found[(prd, phase)] = str(matches[0])
    return found


def _stress(text: str) -> int:
    """Parses --stress: a whole number of passes from 1 to 20."""
    if not text.isdigit() or not 1 <= int(text) <= 20:
        raise argparse.ArgumentTypeError("expected a number of passes from 1 to 20")
    return int(text)


def cmd_release(args: argparse.Namespace) -> int:
    """Queues a release: one run for the shared stages and one QA run per phase."""
    if not args.phases or not args.plan_dir:
        die("--release needs --phases F01:P1,F02:P1,... and --plan-dir <directory of plans>")
    try:
        phases = [release.split_phase(text) for text in args.phases.split(",")]
    except ValueError as exc:
        die(str(exc))
    if len(set(phases)) != len(phases):
        die("--phases names a phase twice")
    plans = _phase_plans(args.plan_dir, phases)
    sha = git.resolve_commit(args.repo, args.ref)
    branch = git.branch_of(args.repo, args.ref)
    repo = git.canonical_repo(args.repo)
    for path in plans.values():
        _refuse_unwalkable(_plan_text(repo, sha, path), f"plan {path}", args.allow_manual)
    titles = {}
    for prd, phase in phases:
        ident = cycles.identify_phase(repo, prd, phase)
        known = cycles.load(ident) if cycles.exists(ident) else None
        if known is not None and known.closed_at is not None:
            die(f"cycle {ident} ({prd} {phase}) was closed by a deploy; a release cannot reopen it")
        titles[(prd, phase)] = known.feature if known else f"{prd} {phase}"
    names = [f"{prd}:{phase}" for prd, phase in phases]
    common = dict(
        repo=repo,
        sha=sha,
        branch=branch,
        env=args.env,
        force=True,
        dry_run=args.dry_run,
        retest_of=None,
        base_arg=args.base,
        allow_manual=args.allow_manual,
    )
    queued: list[str] = []
    feature = args.feature or f"Release {sha[:12]}: {', '.join(names)}"
    _submit(
        **common,
        feature=feature,
        plan_path="",
        qa=False,
        reuse=not args.force and args.stress == 1,
        stress=args.stress,
        prd="RELEASE",
        phase=sha[:12],
        run_mode="release",
        phases=names,
        out=queued,
    )
    if args.dry_run:
        for prd, phase in phases:
            print(f"phase {prd}·{phase}: QA run with plan {plans[(prd, phase)]}")
        return 0
    kids: list[str] = []
    try:
        for prd, phase in phases:
            print(f"phase {prd}·{phase}:")
            before = len(kids)
            _submit(
                **common,
                feature=titles[(prd, phase)],
                plan_path=plans[(prd, phase)],
                qa=True,
                reuse=not args.force,
                prd=prd,
                phase=phase,
                run_mode="phase",
                release=queued[0],
                only_stages=["qa"],
                out=kids,
            )
            if len(kids) == before:
                die(f"phase {prd}·{phase} was not queued")
    except SystemExit:
        print(f"a phase could not be queued: cancelling release {queued[0]}", file=sys.stderr)
        for run_id in [*kids, queued[0]]:
            _cancel(run_id)
        raise
    print(f"release run {queued[0]}: follow it with: qg wait {queued[0]}")
    return 0


QA_ONLY_AT_CLOSE = (
    "QA left out: the QA pass runs only when a phase is closed (--prd <id> --phase <id>) or at "
    "release (--release); this run checks the other stages"
)


def cmd_verify(args: argparse.Namespace) -> int:
    """Queues a verification of one commit; its QA stage only with --prd and --phase (a phase
    close), never in a run between closes."""
    if args.release:
        return cmd_release(args)
    if not args.feature:
        die("--feature is required")
    sha = git.resolve_commit(args.repo, args.ref)
    branch = git.branch_of(args.repo, args.ref)
    repo = git.canonical_repo(args.repo)
    worktree = "" if args.simplify else (inplace.toplevel(args.repo) or "")
    if worktree and (why := inplace.refusal(worktree, sha)):
        die(why)
    phase_close = bool(args.prd and args.phase)
    if not args.no_qa and not phase_close:
        print(QA_ONLY_AT_CLOSE)
    return _submit(
        repo=repo,
        sha=sha,
        branch=branch,
        feature=args.feature,
        plan_path=_plan_path(args.plan),
        env=args.env,
        qa=phase_close and not args.no_qa,
        force=args.force or args.stress > 1,
        dry_run=args.dry_run,
        retest_of=None,
        base_arg=args.base,
        reuse=not args.force and args.stress == 1,
        simplify_stage=args.simplify,
        reopen=args.reopen,
        prd=args.prd or "",
        phase=args.phase or "",
        stress=args.stress,
        worktree=worktree,
        allow_manual=args.allow_manual,
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Proposes verify.yml for a repository from its protected ref; writes it with --write."""
    return init.run(args.repo, args.profile, args.write, not args.no_baseline)


def cmd_lite(args: argparse.Namespace) -> int:
    """Checks a commit in the foreground with the deterministic stages; 0 green, 1 red."""
    return lite.run(args.repo, args.ref)


GC_DAYS = 7


def _gc_since(repo: str, ref: str) -> str:
    """Returns where the next nightly simplify starts: the commit the last one of `repo` that
    passed was asked to review (never its simplified commit, which master does not hold), else
    the last commit of `ref` older than GC_DAYS days, else the root of `ref`. A failed night keeps
    the base; its audit skips the units the ledger records as finished."""
    done = [
        run
        for run in queue.list_runs()
        if run.repo == repo
        and run.mode == "nightly"
        and "simplify" in run.stages
        and run.stages["simplify"].status == "passed"
    ]
    if done:
        return done[-1].requested_sha or done[-1].sha
    older = git.out(repo, "rev-list", "-1", f"--before={GC_DAYS}.days.ago", ref)
    return older or git.out(repo, "rev-list", "--max-parents=0", ref).splitlines()[0]


def cmd_nightly(args: argparse.Namespace) -> int:
    """Queues the whole test suite of a commit (default: the tip of master); never a deploy gate.

    With `--simplify` the run also reviews what reached the commit since `--since` (default: the
    previous nightly simplify of the repository) and leaves its fixes on qg/simplified/<run>.
    """
    repo = git.canonical_repo(args.repo)
    ref = args.ref or config.protected_ref(repo)
    if args.since and not args.simplify:
        die("--since is where --simplify starts: give --simplify too")
    since = git.resolve_commit(repo, args.since or _gc_since(repo, ref)) if args.simplify else None
    return _submit(
        repo=repo,
        sha=git.resolve_commit(repo, ref),
        branch=git.branch_of(repo, ref),
        feature=args.feature,
        plan_path="",
        env="staging" if "staging" in config.load(repo, ref).config.deploy else "prod",
        qa=False,
        force=True,
        dry_run=args.dry_run,
        retest_of=None,
        reuse=False,
        nightly=True,
        simplify_stage=args.simplify,
        base_arg=since,
    )


def cmd_retest(args: argparse.Namespace) -> int:
    """Queues a new run of the same repo, branch and feature, flagged as a retest of `args.run`."""
    old = queue.load(queue.resolve(args.run))
    if old.mode == "nightly":
        die("a nightly run is queued again with `qg nightly`, not retested")
    asked = queue.load_request(old.id)
    tip = git.tip_of_branch(old.repo, old.branch) if old.branch != "detached" else None
    return _submit(
        repo=old.repo,
        sha=tip or old.requested_sha or old.sha,
        branch=old.branch,
        feature=old.feature,
        plan_path=asked.explicit_plan or ("" if asked.base else old.plan),
        env=old.env,
        qa=old.qa,
        force=True,
        dry_run=False,
        retest_of=old.id,
        base_arg=asked.base or None,
        reuse=False,
        given_cycle=old.cycle,
        allow_manual=args.allow_manual,
    )


RESUMABLE = ("cancelled", "failed", "interrupted", "error")


def cmd_resume(args: argparse.Namespace) -> int:
    """Re-queues a cancelled or failed run of the same requested commit, reusing what passed."""
    old = queue.load(queue.resolve(args.run))
    if old.mode == "nightly":
        die("a nightly run is queued again with `qg nightly`, not resumed")
    if old.status not in RESUMABLE:
        die(f"run {old.id} is {old.status}: only a {', '.join(RESUMABLE)} run is resumed")
    asked = queue.load_request(old.id)
    held = paths.plan_file(old.id)
    keep = held.read_text() if asked.base and not asked.explicit_plan and held.is_file() else None
    return _submit(
        repo=old.repo,
        sha=asked.sha,
        branch=old.branch,
        feature=old.feature,
        plan_path=asked.explicit_plan or ("" if asked.base else old.plan),
        env=old.env,
        qa=old.qa,
        force=True,
        dry_run=False,
        retest_of=old.id,
        base_arg=asked.base or None,
        reuse=True,
        plan_override=keep,
        simplify_stage="simplify" in asked.stage_set,
        given_cycle=old.cycle,
        allow_manual=args.allow_manual,
    )


def _stamp() -> str:
    """Returns the local time as HH:MM:SS for progress lines."""
    return time.strftime("%H:%M:%S")


def _finish_wait(state: RunState) -> int:
    """Prints the end of a run and returns the exit status of `qg wait`."""
    if state.status in ("passed", "failed"):
        verdict, reasons = report.verdict_of(state, queue.load_request(state.id))
        print(f"{_stamp()} {verdict}  report: {paths.report_md(state.id)}")
        for reason in reasons:
            print(f"  - {reason}")
        _print_warnings(state)
        if state.simplified_ref:
            name = state.simplified_ref.removeprefix("refs/heads/")
            print(
                f"  simplified: to take it, git cherry-pick {state.sha}"
                if state.mode == "nightly"
                else f"  simplified: merge {name} into {state.branch} (fast-forward) "
                "before deploying"
            )
        print(f"  topic pages: qg topics {state.id}")
        return 0 if verdict == report.READY else 1
    detail = f" by {state.superseded_by}" if state.status == "superseded" else ""
    print(f"{_stamp()} run {state.status}{detail}" + (f": {state.error}" if state.error else ""))
    if any(stage.started for stage in state.stages.values()):
        print(f"  topic pages: qg topics {state.id}")
    return 2


def _retried_as(state: RunState) -> str | None:
    """Returns the run that queued `state` again after it ended `error` (see `executor.retry`) or
    was preempted (see `executor.requeue`)."""
    preempted = state.status == "interrupted" and executor.preempted_for(state.id)
    if state.status != "error" and not preempted:
        return None
    return next((run.id for run in queue.list_runs() if run.retest_of == state.id), None)


def cmd_wait(args: argparse.Namespace) -> int:
    """Polls a run, following its automatic retry; exit 0 READY, 1 NOT READY, 2 aborted,
    3 timeout, 4 paused."""
    run_id = queue.resolve(args.run)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    seen: dict[str, str] = {}
    last = ""
    while True:
        state = queue.load(run_id)
        if state.status != last:
            last = state.status
            print(f"{_stamp()} run {run_id}: {state.status}", flush=True)
        for name, stage in state.stages.items():
            if stage.status != "pending" and seen.get(name) != stage.status:
                seen[name] = stage.status
                tail = f" ({stage.summary})" if stage.summary and stage.status != "running" else ""
                print(f"{_stamp()} {name}: {queue.stage_text(stage)}{tail}", flush=True)
        if state.status in queue.FINISHED and not release.awaiting_phases(state):
            again = _retried_as(state)
            if again is None:
                return _finish_wait(state)
            print(f"{_stamp()} run {run_id} {state.status}: {state.error}", flush=True)
            run_id, seen, last = again, {}, ""
            continue
        if state.status == "queued" and not paths.is_enabled():
            print(
                f"{_stamp()} qg is paused: the operator has not activated qg (qg enable); "
                f"run {run_id} waits in the queue"
            )
            return 4
        if deadline is not None and time.monotonic() >= deadline:
            print(f"{_stamp()} timeout after {args.timeout}s; run {run_id} is still {state.status}")
            return 3
        time.sleep(POLL_SECONDS)


def cmd_shadow_report(args: argparse.Namespace) -> int:
    """Prints the shadow window per project and whether the exit criterion holds; exit 0 only
    when every printed project meets the items the calculator can decide."""
    criterion = shadow_report.Criterion(k=args.k, r=args.r, m=args.m)
    grouped = shadow_report.by_project(shadow_report.load(paths.SHADOW))
    if args.project:
        grouped = {args.project: grouped.get(args.project, [])}
    if not grouped:
        print("no shadow lines")
        return 1
    ok = True
    for name, rows in grouped.items():
        found = shadow_report.window(rows)
        items = shadow_report.evaluate(found, criterion)
        print(shadow_report.render(name, found, items, criterion))
        ok = shadow_report.met(items) and ok
    return 0 if ok else 1


def _run_row(run: RunState) -> list[str]:
    """Returns the status-table cells of one run."""
    finished = run.status in queue.FINISHED
    p0, p1 = report.finding_counts(run) if finished else (0, 0)
    return [
        run.id,
        f"{Path(run.repo).name}/{run.branch}",
        run.sha[:8],
        run.env,
        report.display_verdict(run),
        f"{p0}/{p1}" if finished else "-",
        ago(run.ended or run.started or run.created),
        run.feature,
    ]


def _print_needs(runs: list[RunState]) -> None:
    """Prints the memory each lane needs to be admitted, per repo, from measured peaks."""
    latest: dict[str, RunState] = {}
    for run in sorted(runs, key=lambda run: run.id):
        latest[run.repo] = run
    print("\nmemory admission needs")
    for repo, run in sorted(latest.items()):
        lanes = config.parse(queue.load_request(run.id).config, f"request of run {run.id}")
        for lane in ("setup", "static", "tests", "qa_stack"):
            need = admission.lane_need(repo, lane, lanes.resources.lanes[lane][1])
            print(f"  {Path(repo).name:<12} {lane:<9} needs {admission.describe(need)}")


def cmd_status(args: argparse.Namespace) -> int:
    """Prints whether qg is enabled, what runs, what waits and the last run of every branch."""
    with queue.snapshot():
        return _status(args)


def _status(args: argparse.Namespace) -> int:
    """Renders `qg status` from one snapshot of the runs (it asks for them per row and per tree)."""
    runs = queue.list_runs()
    print("qg: enabled" if paths.is_enabled() else "paused: the operator has not activated qg")
    print(f"runner service: {proc.service_state()}")
    running = {run.slot: run for run in runs if run.status == "running"}
    memory = admission.gib(admission.available_memory())
    print(f"\nslots (memory available {memory}, disk free {admission.gib(admission.free_disk())})")
    for slot in queue.SLOTS:
        run = running.get(slot)
        if run is None:
            print(f"  {slot:<5} free")
        else:
            where = f"{Path(run.repo).name}/{run.branch}"
            print(
                f"  {slot:<5} {run.id}  {where}  {run.stage or 'starting'}  "
                f"{human(elapsed(run.started, None))}"
            )
            for reason in run.waiting.values():
                print(f"        {reason}")
    _print_needs(runs)
    waiting = [run for run in runs if run.status == "queued"]
    now = datetime.now(UTC)
    print(f"\nqueue ({len(waiting)})")
    for position, run in enumerate(waiting, start=1):
        where = f"{Path(run.repo).name}/{run.branch}"
        print(
            f"  {position}  {run.id}  {where}  {run.sha[:8]}  {run.env}  queued {ago(run.created)}"
        )
        if hold := queue.hold_left(run, now):
            print(f"     {queue.hold_text(hold)}")
        for reason in run.waiting.values():
            print(f"     {reason}")
    known = deployed.load()
    print("\nlast deployed")
    if known:
        for repo, envs in sorted(known.items()):
            for env, entry in sorted(envs.items()):
                print(
                    f"  {Path(repo).name:<12} {env:<8} {entry['sha'][:12]}  "
                    f"tree {entry['tree'][:12]}  {ago(entry['at'])}"
                )
    else:
        print("  nothing recorded (qg deployed <sha> --env <env>)")
    headers = ["id", "repo/branch", "sha", "env", "verdict", "P0/P1", "when", "feature"]
    if args.all:
        shown = sorted(runs, key=lambda r: r.id, reverse=True)
        print("\nall runs")
    else:
        shown = queue.latest_per_branch(runs)
        print("\nlast run per repo and branch")
    print(_table(headers, [_run_row(run) for run in shown]) if shown else "  none yet")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Prints the markdown report of a run."""
    run_id = queue.resolve(args.run)
    path = paths.report_md(run_id)
    if not path.exists():
        die(f"run {run_id} has no report yet: it is {queue.load(run_id).status}")
    print(path.read_text(), end="")
    return 0


def _log_override(args: argparse.Namespace, repo: str, sha: str, tree: str) -> None:
    """Appends the --unverified override to overrides.log and prints a loud warning."""
    runs = [r for r in queue.list_runs() if r.repo == repo and r.tree == tree]
    last = runs[-1] if runs else None
    blocking = report.open_findings(report.load_findings(last.id)) if last else []
    entry = {
        "when": now_iso(),
        "repo": repo,
        "sha": sha,
        "tree": tree,
        "env": args.env,
        "reason": args.unverified,
        "last_run_id": last.id if last else None,
        "open_findings": [{"id": f.id, "severity": f.severity, "title": f.title} for f in blocking],
    }
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with paths.OVERRIDES.open("a") as log:
        log.write(json.dumps(entry) + "\n")
    bar = "!" * 72
    print(bar, file=sys.stderr)
    print(f"UNVERIFIED {args.env.upper()} DEPLOY of {sha[:12]}: {args.unverified}", file=sys.stderr)
    print(f"No verification gate was honoured. Logged in {paths.OVERRIDES}", file=sys.stderr)
    print(bar, file=sys.stderr)


def _print_warnings(run: RunState) -> None:
    """Prints what the run's verdict lets through but the operator must see, one per line."""
    for warning in report.warnings_of(run):
        print(f"  warning: {warning}")


def cmd_check(args: argparse.Namespace) -> int:
    """Exits 0 only if the commit's tree is verified for the env; --unverified logs an override."""
    sha = git.resolve_commit(args.repo, args.sha)
    repo = git.canonical_repo(args.repo)
    tree = git.tree_of(repo, sha)
    if args.unverified:
        _log_override(args, repo, sha, tree)
        return 0
    cfg = config.load(repo, sha).config
    run, reasons = report.best_run(repo, tree, cfg.policy(args.env))
    if run is not None and not reasons:
        print(f"OK: run {run.id} satisfies the {args.env} policy for tree {tree[:12]}")
        _print_warnings(run)
        print(f"report: {paths.report_md(run.id)}")
        if args.dry_run:
            print("dry run: no deploy recorded")
            return 0
        deployed.record(repo, args.env, sha, tree)
        print(f"recorded {sha[:12]} as the last {args.env} deploy of {repo}")
        closing = [run, *release.children(run.id)] if run.mode == "release" else [run]
        for closed in closing:
            if cycles.exists(closed.cycle):
                cycles.close(closed.cycle, closed.id, sha, args.env)
                print(f"closed cycle {closed.cycle}")
        return 0
    print(f"NOT VERIFIED for {args.env}: tree {tree[:12]} ({sha[:12]})")
    if run is not None:
        print(f"newest run: {run.id} ({run.status})")
    for reason in reasons:
        print(f"  - {reason}")
    if run is not None:
        _print_warnings(run)
    simplified = [
        r
        for r in queue.list_runs()
        if r.repo == repo and r.requested_tree == tree and r.simplified_ref and report.trusted(r)
    ]
    if simplified:
        newest = simplified[-1]
        print(
            f"run {newest.id} verified a simplified tree: merge "
            f"{newest.simplified_ref.removeprefix('refs/heads/')} into {newest.branch} "
            "(fast-forward), then check the new tip"
        )
    print(f'verify it with: qg verify --repo {repo} --env {args.env} --feature "<feature>"')
    return 1


def cmd_deployed(args: argparse.Namespace) -> int:
    """Sets by hand the last-deployed record of a repo and env: the base of the next plan."""
    sha = git.resolve_commit(args.repo, args.sha)
    repo = git.canonical_repo(args.repo)
    deployed.record(repo, args.env, sha, git.tree_of(repo, sha))
    print(f"recorded {sha} as the last {args.env} deploy of {repo}")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    """Acknowledges a P1 finding of a finished run as the operator."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status not in queue.FINISHED:
        die(f"run {run_id} is still {state.status}; findings are acknowledged once it finished")
    findings = report.load_findings(run_id)
    match = [f for f in findings if f.id == args.finding]
    if not match:
        die(f"run {run_id} has no finding {args.finding!r}")
    finding = match[0]
    if finding.severity != "P1":
        die(f"{finding.id} is {finding.severity}: only P1 is acknowledged (P0 must be fixed)")
    finding.acknowledged = report.Ack(who="operator", when=now_iso(), note=args.note)
    report.save_findings(run_id, findings)
    report.refresh(run_id)
    topics.refresh(run_id)
    print(f"acknowledged {finding.id} of run {run_id}: {args.note}")
    return 0


def cmd_fixed(args: argparse.Namespace) -> int:
    """Closes a P2 or P3 finding the session fixed, without a run: status fixed, with the commit
    and the note. Refused for a P0 or P1, which a run of its cycle re-verifies, and on any run
    but the newest finished one of the cycle that holds the finding."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status not in queue.FINISHED:
        die(f"run {run_id} is still {state.status}; findings are closed once it finished")
    findings = report.load_findings(run_id)
    match = [f for f in findings if f.id == args.finding]
    if not match:
        die(f"run {run_id} has no finding {args.finding!r}")
    finding = match[0]
    if finding.severity in ("P0", "P1"):
        die(
            f"{finding.id} is {finding.severity}: a P0 or P1 is closed only by a run of its cycle "
            f"that re-verifies it (qg retest {run_id})"
        )
    if finding.status != "open" or finding.dismissed is not None:
        die(f"{finding.id} of run {run_id} is {report.status_word(finding)}, not open")
    later = [
        other
        for other in cycles.load(state.cycle).runs
        if other > run_id
        and paths.state_file(other).exists()
        and queue.load(other).status in queue.FINISHED
        and any(f.id == finding.id for f in report.load_findings(other))
    ]
    if later:
        die(f"run {later[-1]} of the same cycle also holds {finding.id}: close it there")
    commit = git.resolve_commit(state.repo, args.commit)
    finding.status = "fixed"
    finding.closed = report.Closure(who="session", when=now_iso(), commit=commit, note=args.note)
    report.save_findings(run_id, findings)
    report.refresh(run_id)
    topics.refresh(run_id)
    print(f"closed {finding.id} of run {run_id} as fixed in {commit[:12]}: {args.note}")
    return 0


def cmd_probe_review(args: argparse.Namespace) -> int:
    """Records the operator's review of a P0's red probe and flip; later verifiers learn from it."""
    if not args.probe and not args.flip:
        die("give --probe, --flip or both")
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status not in queue.FINISHED:
        die(f"run {run_id} is still {state.status}; a probe is reviewed once it finished")
    match = [f for f in report.load_findings(run_id) if f.id == args.finding]
    if not match:
        die(f"run {run_id} has no finding {args.finding!r}")
    made = match[0].probe
    if made is None or made.status != report.RED:
        die(f"{args.finding} has no red probe to review")
    if args.flip and made.flip not in report.PATCHED:
        die(f"{args.finding} has no flip patch to review (flip {made.flip or 'none'})")
    kept = verifier.record_operator(run_id, match[0], args.probe, args.flip, args.why)
    report.refresh(run_id)
    topics.refresh(run_id)
    print(f"reviewed the probe of {args.finding} of run {run_id}; kept as an example in {kept}")
    return 0


def cmd_simplify_queue(args: argparse.Namespace) -> int:
    """Lists the simplify queue of a repository: what simplify passes left alone and audit lenses
    confirmed, and the operator's say; `--lens` and `--severity` narrow it."""
    repo = git.canonical_repo(args.repo)
    open_ = (backlog.PENDING, backlog.ACCEPTED)
    shown = [
        item
        for item in backlog.load(repo)
        if (args.all or item.status in open_)
        and (not args.lens or item.lens == args.lens)
        and (not args.severity or item.severity in args.severity.split(","))
    ]
    if not shown:
        print(
            f"the simplify queue of {repo} is empty" + ("" if args.all else " (--all for decided)")
        )
        return 0
    for item in shown:
        said = f" — {item.why}" if item.why else ""
        print(f"{item.id}  {item.status:<8}  run {item.run_id}  {item.text}{said}")
        if item.evidence:
            print(f"    evidence: {item.evidence}")
        if item.fix:
            print(f"    fix: {item.fix}")
    return 0


def cmd_audit_rules(args: argparse.Namespace) -> int:
    """Downloads the semgrep registry packs the nightly audit runs, replacing the ones it has."""
    for path in audit.fetch_rules():
        print(f"{path} ({path.stat().st_size} bytes)")
    return 0


def _rules_of(repo: str, path: str | None, sha: str) -> config.Rules:
    """Returns the rules of the --config file, else of the protected verify.yml; exits on none."""
    if path:
        if not Path(path).is_file():
            die(f"--config {path} is not a file")
        return config.parse_rules(config.load_yaml(Path(path).read_text(), path), path)
    found = config.load(repo, sha).config.rules
    if found is None:
        die(f"{config.VERIFY_PATH} of {repo} has no rules: section; pass --config FILE")
    return found


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Prints the rule violations --head adds over --base; exit 0 when none, 1 otherwise."""
    base = git.resolve_commit(args.repo, args.base)
    head = git.resolve_commit(args.repo, args.head)
    verdict = rules.check(args.repo, _rules_of(args.repo, args.config, head), base, head)
    for hit in verdict.regressions:
        print(hit)
    print(verdict.summary)
    return 0 if verdict.passed else 1


def cmd_rules_baseline(args: argparse.Namespace) -> int:
    """Prints the violations of the whole tree at HEAD per rule, their change since the last
    snapshot and the files holding most; stores the snapshot."""
    head = git.resolve_commit(args.repo, "HEAD")
    found = _rules_of(args.repo, args.config, head)
    for line in rules.burn_down(args.repo, git.canonical_repo(args.repo), found, head):
        print(line)
    return 0


def cmd_simplify_decide(args: argparse.Namespace) -> int:
    """Records the operator's decision on a simplify queue item: the next pass fixes or skips it."""
    repo = git.canonical_repo(args.repo)
    item = backlog.decide(repo, args.item, args.decision, args.why)
    then = (
        "the next simplify pass fixes it"
        if item.status == backlog.ACCEPTED
        else "no pass proposes it again"
    )
    print(f"{item.id} {item.status}: {then}")
    return 0


def cmd_remerge(args: argparse.Namespace) -> int:
    """Recomputes a finished run's findings from its own QA output with the current merge."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status not in queue.FINISHED:
        die(f"run {run_id} is still {state.status}; findings are merged again once it finished")
    if "qa" not in state.stages:
        die(f"run {run_id} has no QA stage; a release run is judged on the findings of its phases")
    request = queue.load_request(run_id)
    before = report.load_findings(run_id)
    after, notes = qa.remerge(run_id, request)
    stamp = now_iso()
    run_dir = paths.run_dir(run_id)
    backup = run_dir / f"findings.before-remerge-{stamp.replace(':', '')}.json"
    shutil.copy2(paths.findings_file(run_id), backup)
    report.save_findings(run_id, after)
    changed = {
        f.id: [old.status, f.status]
        for f in after
        for old in before
        if old.id == f.id and old.status != f.status
    }
    entry = {
        "when": stamp,
        "who": os.environ.get("USER", "operator"),
        "reason": args.reason,
        "backup": backup.name,
        "changed": changed,
        "notes": notes,
    }
    with (run_dir / "remerge.log").open("a") as log:
        log.write(json.dumps(entry) + "\n")
    report.refresh(run_id)
    topics.refresh(run_id)
    print(f"remerged run {run_id}: {len(changed)} finding(s) changed status; backup {backup.name}")
    for note in notes:
        print(f"  {note}")
    return 0


def cmd_verify_p0(args: argparse.Namespace) -> int:
    """Judges a finished run's unverified open P0 findings on a checkout of its commit."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status not in queue.FINISHED:
        die(f"run {run_id} is still {state.status}; its P0 are verified once it finished")
    request = queue.load_request(run_id)
    plan = ""
    if request.plan and Path(request.plan).is_file():
        plan = Path(request.plan).read_text(errors="replace")
    with verifier.checkout(request.repo, state.sha, paths.run_dir(run_id) / "verify-src") as src:
        said = verifier.verify_run(run_id, src, plan, lambda argv: argv)
    report.refresh(run_id)
    topics.refresh(run_id)
    if state.release and paths.state_file(state.release).exists():
        report.refresh(state.release)
    print(said or f"run {run_id} has no unverified P0")
    return 0


def _drop_queued(current: RunState) -> None:
    """Cancels the run when it is still queued."""
    if current.status == "queued":
        current.status = "cancelled"
        current.ended = now_iso()


def _request_cancel(current: RunState) -> None:
    """Records that the operator asked to cancel the run."""
    current.cancel_requested = True


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancels a run; a release run takes the QA runs of its phases with it."""
    run_id = queue.resolve(args.run)
    status = _cancel(run_id)
    for kid in release.children(run_id):
        if kid.status in ("queued", "running"):
            _cancel(kid.id)
    return status


def _cancel(run_id: str) -> int:
    """Cancels a queued run at once, or stops a running one and tears its sandbox down."""
    state = queue.load(run_id)
    if state.status == "queued":
        if queue.update(run_id, _drop_queued).status == "cancelled":
            notify.finished(run_id)
            print(f"cancelled queued run {run_id}")
            return 0
    elif state.status != "running":
        print(f"run {run_id} is already {state.status}")
        return 1
    queue.update(run_id, _request_cancel)
    proc.stop_scope(run_id)
    deadline = time.monotonic() + CANCEL_WAIT_SECONDS
    while queue.load(run_id).status == "running" and time.monotonic() < deadline:
        time.sleep(1)
    if queue.load(run_id).status == "running":
        executor.abandon(run_id, "cancelled; the executor did not finish by itself")
    print(f"run {run_id}: {queue.load(run_id).status}")
    return 0


def _check_docker() -> tuple[str, str]:
    """Checks the docker daemon answers."""
    if not shutil.which("docker"):
        return "fail", "docker not found on PATH"
    result = proc.run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if result.returncode != 0:
        return "fail", (result.stderr.strip().splitlines() or ["docker info failed"])[0]
    return "ok", f"server {result.stdout.strip()}"


def _check_systemd() -> tuple[str, str]:
    """Checks user scopes with a CPU quota can be created, and that nice and ionice exist."""
    missing = [tool for tool in ("systemd-run", "nice", "ionice") if not shutil.which(tool)]
    if missing:
        return "fail", f"{', '.join(missing)} not found on PATH"
    probe = ["systemd-run", "--user", "--scope", "--quiet", "-p", "CPUQuota=100%", "true"]
    result = proc.run(probe)
    if result.returncode != 0:
        return "fail", (result.stderr.strip().splitlines() or ["systemd-run failed"])[0]
    return "ok", "user scopes with CPUQuota work; nice and ionice found"


def _check_tool(name: str) -> tuple[str, str]:
    """Checks `name --version` runs and returns its first line."""
    if not shutil.which(name):
        return "fail", f"{name} not found on PATH"
    result = proc.run([name, "--version"])
    line = (result.stdout.strip().splitlines() or [result.stderr.strip()])[0]
    return ("ok" if result.returncode == 0 else "fail"), line


def _check_root() -> tuple[str, str]:
    """Checks the state root exists or can be created, and is writable."""
    ancestor = paths.STATE_ROOT
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return "fail", f"{ancestor} is not writable"
    paths.ensure()
    return "ok", f"{paths.STATE_ROOT} is writable"


def _check_disk() -> tuple[str, str]:
    """Checks the filesystem of the state root has room for worktrees and caches."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    free = admission.free_disk()
    reason = admission.disk_reason(free)
    if reason:
        return "fail", reason
    need = admission.gib(admission.MIN_FREE_DISK_BYTES)
    return "ok", f"{admission.gib(free)} free on {paths.STATE_ROOT} (a run starts at {need})"


def _check_ports() -> tuple[str, str]:
    """Checks enough ports of 21000-21999 are free."""
    free = sum(1 for port in paths.PORT_RANGE if sandbox.port_free(port))
    return (
        "ok" if free >= MIN_FREE_PORTS else "fail"
    ), f"{free} of {len(paths.PORT_RANGE)} ports free"


def _check_images() -> tuple[str, str]:
    """Reports the tool that turns evidence screenshots into thumbnails; optional."""
    tool = cycle_page.image_tool()
    if tool:
        return "ok", f"{tool} makes the evidence thumbnails of the cycle page"
    return "warn", "no ffmpeg, magick or convert: evidence screenshots are listed by path only"


def cmd_prune(args: argparse.Namespace) -> int:
    """Removes what finished runs no longer need (see `qg.prune`); --dry-run only prints it."""
    free = admission.free_disk()
    plan = prune.prune(dry_run=args.dry_run, pressure=prune.due(free) == "pressure")
    print(f"{'would free' if args.dry_run else 'freed'}:")
    print(prune.describe(plan))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prints ok, warn or fail for every dependency of the runner; exits 1 when any fails."""
    checks: list[tuple[str, Callable[[], tuple[str, str]]]] = [
        ("docker", _check_docker),
        ("systemd-run", _check_systemd),
        ("uv", lambda: _check_tool("uv")),
        ("git", lambda: _check_tool("git")),
        ("claude", lambda: _check_tool("claude")),
        ("curl", lambda: _check_tool("curl")),
        ("state root", _check_root),
        ("disk", _check_disk),
        ("ports", _check_ports),
        ("images", _check_images),
    ]
    failed = 0
    for name, check in checks:
        status, detail = check()
        failed += status == "fail"
        print(f"{status:<4}  {name:<12} {detail}")
    activated = "yes" if paths.is_enabled() else "no (qg enable)"
    print(f"\nactivated: {activated}   runner service: {proc.service_state()}")
    return 1 if failed else 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Activates qg: the runner starts executing queued runs."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    paths.ENABLED.touch()
    print(f"qg enabled ({paths.ENABLED}); the runner service must be running to pick up runs")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Deactivates qg: the runner stops starting runs; one in flight finishes."""
    paths.ENABLED.unlink(missing_ok=True)
    print("qg disabled: the runner executes nothing new")
    return 0


def cmd_runner(args: argparse.Namespace) -> int:
    """Runs the daemon in the foreground."""
    return runner.serve()


def cmd_board(args: argparse.Namespace) -> int:
    """Writes the static HTML board of every branch's last verdict and prints its path."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.BOARD, report.render_board(queue.list_runs()))
    print(paths.BOARD)
    return 0


def cmd_cycle_page(args: argparse.Namespace) -> int:
    """Rebuilds the page of the run's cycle and prints its path and the stored artifact URL."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status in ("queued", "running"):
        die(f"run {run_id} is still {state.status}; wait for it with: qg wait {run_id}")
    if state.status == "superseded" or not any(s.started for s in state.stages.values()):
        die(f"run {run_id} never executed ({state.status}); use a run that did")
    topics.refresh(run_id)
    cycle = cycles.load(state.cycle)
    print(f"cycle: {cycle.id}")
    print(f"feature: {cycle.feature}")
    print(f"page: {paths.page_file(cycle.id)}")
    print(f"artifact: {cycle.artifact_url or 'none'}")
    return 0


def cmd_cycle_artifact(args: argparse.Namespace) -> int:
    """Stores the claude.ai artifact URL of a cycle; one cycle keeps one URL."""
    cycle_id = cycles.resolve(args.cycle)
    with queue.locked():
        cycle = cycles.set_artifact(cycle_id, args.url)
    print(f"cycle {cycle.id}: artifact {cycle.artifact_url}")
    return 0


def cmd_prd_artifact(args: argparse.Namespace) -> int:
    """Stores the claude.ai artifact URL of a PRD on every phase cycle of it."""
    with queue.locked():
        found = cycles.set_prd_artifact(args.prd, args.url)
    print(f"PRD {args.prd}: artifact {args.url} on {len(found)} phase(s)")
    return 0


def cmd_cycle_assign(args: argparse.Namespace) -> int:
    """Maps an existing cycle to a PRD phase (one-off for cycles that predate --prd/--phase)."""
    ident = cycles.resolve(args.cycle)
    with queue.locked():
        cycle = cycles.assign(ident, args.prd, args.phase)
    print(f"cycle {cycle.id}: PRD {cycle.prd} phase {cycle.phase}")
    return 0


def cmd_prd(args: argparse.Namespace) -> int:
    """Prints every phase of a PRD with its state, last run, cycle artifact and deployed commit."""
    found = cycles.of_prd(args.prd)
    if not found:
        die(f"no cycle belongs to PRD {args.prd!r}")
    print(f"PRD {args.prd}  artifact: {found[0].prd_artifact_url or 'none'}")
    for cycle in found:
        last = None
        for run_id in reversed(cycle.runs):
            if paths.state_file(run_id).exists():
                last = queue.load(run_id)
                break
        state = f"Closed · deployed {cycle.closed_sha[:8]}" if cycle.closed_at else "Open"
        run = f"{last.id} ({last.status})" if last else "none"
        print(f"  {cycle.phase:<6} {state}")
        print(f"         title    {cycle.feature}")
        print(f"         last run {run}")
        print(f"         cycle    {cycle.id}  artifact: {cycle.artifact_url or 'none'}")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    """Rebuilds the topic pages of the run's cycle and prints each topic's page and artifact URL."""
    run_id = queue.resolve(args.run)
    state = queue.load(run_id)
    if state.status in ("queued", "running"):
        die(f"run {run_id} is still {state.status}; wait for it with: qg wait {run_id}")
    topics.refresh(run_id)
    found = topics.list_topics(state.cycle)
    print(f"cycle: {state.cycle}")
    if not found:
        print("topics: none (no topic has been walked by S5 yet, so no page exists)")
    for topic in found:
        print(f"topic: {topic.slug}")
        print(f"name: {topic.name}")
        print(f"verdict: {topic.verdict}")
        print(f"page: {paths.topic_page(state.cycle, topic.slug)}")
        print(f"artifact: {topic.artifact_url or 'none'}")
    return 0


def cmd_topic_artifact(args: argparse.Namespace) -> int:
    """Stores the claude.ai artifact URL of one topic of a cycle; one topic keeps one URL."""
    cycle_id = cycles.resolve(args.cycle)
    with queue.locked():
        topic = topics.set_artifact(cycle_id, args.slug, args.url)
    print(f"cycle {cycle_id} topic {topic.slug}: artifact {topic.artifact_url}")
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    """Copies the systemd user unit to ~/.config/systemd/user and reloads; enables nothing."""
    source = Path(__file__).resolve().parent.parent / "systemd" / paths.SERVICE_UNIT
    if not source.exists():
        die(f"{source} not found: install qg editable from its repository")
    target_dir = Path.home() / ".config" / "systemd" / "user"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target_dir / paths.SERVICE_UNIT)
    reload = proc.run(["systemctl", "--user", "daemon-reload"])
    if reload.returncode != 0:
        die(f"systemctl --user daemon-reload failed: {reload.stderr.strip()}")
    tools = settings.resolve(os.environ["PATH"])
    settings.save(tools)
    print(f"installed {target_dir / paths.SERVICE_UNIT} and reloaded systemd")
    print(f"QA browser server: node {tools.node}, npx {tools.npx}")
    print(f"not enabled, not started: systemctl --user enable --now {paths.SERVICE_UNIT}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Builds the argument parser with every command."""
    parser = argparse.ArgumentParser(prog="qg", description="Feature verification runner.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def command(
        name: str, handler: Callable[[argparse.Namespace], int], summary: str
    ) -> argparse.ArgumentParser:
        """Registers a command whose handler receives the parsed namespace."""
        added = sub.add_parser(name, help=summary, description=summary)
        added.set_defaults(handler=handler)
        return added

    p = command("verify", cmd_verify, "queue a verification of one commit (one run of a cycle)")
    p.add_argument(
        "--repo",
        default=".",
        help="worktree path (default: current directory); the run works on it in place when "
        "it is clean at --ref, and refuses it when it is dirty (commit or stash first)",
    )
    p.add_argument("--ref", default="HEAD", help="commit to verify (default: HEAD)")
    p.add_argument("--feature", help="title of the cycle on its page (required, but for --release)")
    p.add_argument(
        "--release",
        action="store_true",
        help="verify a release candidate as one release: the shared stages once, one QA run per "
        "phase of --phases, one verdict; a failing test is owned by the phase whose tag it carries",
    )
    p.add_argument("--phases", help="with --release: the phases, PRD:PHASE separated by commas")
    p.add_argument(
        "--plan-dir",
        help="with --release: a directory holding one plan per phase, named like F01-P1.md",
    )
    p.add_argument("--prd", help="PRD id, for example F01; with --phase it is the cycle's key")
    p.add_argument("--phase", help="phase of the PRD, for example P1")
    p.add_argument("--plan", help="path of the plan document of the feature")
    p.add_argument(
        "--allow-manual",
        action="store_true",
        help="queue a plan whose topics are marked manual, operator or real account anyway",
    )
    p.add_argument(
        "--base",
        help="commit the change is measured from (default: the last deploy to --env); "
        "S0 simplify and the generated plan cover base..ref",
    )
    p.add_argument(
        "--env",
        choices=config.ENVS,
        default="prod",
        help="deploy policy to verify for (default: prod, which needs S5)",
    )
    p.add_argument("--no-qa", action="store_true", help="leave S5 (manual QA) out of the run")
    p.add_argument(
        "--simplify",
        action="store_true",
        help="also run the optional S0 /simplify stage first (not part of the gate)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="queue even when the tree is verified; every stage runs, none is reused",
    )
    p.add_argument(
        "--reopen",
        metavar="REASON",
        help="reopen a cycle closed by a deploy that was rolled back; the reason is recorded",
    )
    p.add_argument(
        "--stress",
        type=_stress,
        default=1,
        metavar="N",
        help="run the whole Playwright stage N times on this tree (no reuse) and report every test "
        "that failed at least once, labelled always fails or flaky k/N",
    )
    p.add_argument("--dry-run", action="store_true", help="print the plan; create nothing")

    p = command(
        "init",
        cmd_init,
        "onboard or reform a repository: detect its layout at the protected ref, propose "
        "verify.yml (a minimal file, or the optional sections it lacks), count the rules",
    )
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")
    p.add_argument("--profile", default="python-layered", help="architecture rules profile")
    p.add_argument(
        "--write",
        action="store_true",
        help="write .claude/verify.yml into the working tree (default: print the proposal)",
    )
    p.add_argument(
        "--no-baseline", action="store_true", help="skip the whole-tree count of the rules"
    )

    p = command(
        "lite",
        cmd_lite,
        "check a commit now, in the foreground: static, rules, the affected tests that need no "
        "database, mutants; no Postgres, stack or agent, and no deploy verdict",
    )
    p.add_argument("--repo", default=".", help="worktree to check (default: current directory)")
    p.add_argument("--ref", default="HEAD", help="commit to check (default: HEAD)")

    p = command(
        "nightly",
        cmd_nightly,
        "queue the whole suite of a commit (default: master); never blocks or satisfies a deploy",
    )
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")
    p.add_argument("--ref", help="commit to run (default: the protected master)")
    p.add_argument("--feature", default="nightly", help="name of the run's cycle")
    p.add_argument(
        "--simplify",
        action="store_true",
        help="also run /simplify over what reached the commit since --since; fixes go to a branch",
    )
    p.add_argument(
        "--since",
        help="where --simplify starts (default: the previous nightly simplify, or 7 days)",
    )
    p.add_argument("--dry-run", action="store_true", help="print the plan; create nothing")

    p = command(
        "wait", cmd_wait, "poll a run: exit 0 READY, 1 NOT READY, 2 aborted, 3 timeout, 4 paused"
    )
    p.add_argument("run")
    p.add_argument("--timeout", type=int, help="give up after this many seconds")

    p = command(
        "status",
        cmd_status,
        "show the slots, what waits and why, and the last run per branch. At most 4 runs run at "
        "once. Queue order is FIFO; a run (or its next stage) starts only when MemAvailable is at "
        "least its measured lane peak x1.25 + 1 GiB (with no history min(memory_max, 4 GiB) + "
        "1.5 GiB), except that a run alone on the machine always starts, the "
        "oldest running run's stages never wait, and when the head of the queue does not fit, a "
        "later run whose setup fits may start before it. Two runs never run their Playwright "
        "stage at the same time.",
    )
    p.add_argument("--all", action="store_true", help="list every run")

    p = command("report", cmd_report, "print the report of a run")
    p.add_argument("run")

    p = command("retest", cmd_retest, "queue a new run of the same feature, every stage run again")
    p.add_argument("run")
    p.add_argument(
        "--allow-manual",
        action="store_true",
        help="queue a plan whose topics are marked manual, operator or real account anyway",
    )

    p = command(
        "resume",
        cmd_resume,
        "re-queue a cancelled or failed run of the same commit, reusing the stages that passed",
    )
    p.add_argument("run")
    p.add_argument(
        "--allow-manual",
        action="store_true",
        help="queue a plan whose topics are marked manual, operator or real account anyway",
    )

    p = command(
        "check",
        cmd_check,
        "exit 0 only when the commit's tree is verified for the env; records it as that env's "
        "last deploy (the deploy scripts' guard) unless --dry-run",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="only report the verdict; record no deploy"
    )
    p.add_argument("sha")
    p.add_argument("--env", choices=config.ENVS, required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--unverified", metavar="REASON", help="skip the check and log an override")

    p = command("deployed", cmd_deployed, "set the last-deployed record of a repo and env by hand")
    p.add_argument("sha")
    p.add_argument("--env", choices=config.ENVS, required=True)
    p.add_argument("--repo", default=".")

    p = command(
        "ack", cmd_ack, "acknowledge a P1 finding of a run: an open P1 blocks until acknowledged"
    )
    p.add_argument("run")
    p.add_argument("finding")
    p.add_argument("--note", required=True)

    p = command(
        "fixed",
        cmd_fixed,
        "close a P2 or P3 finding the session fixed, without a run (a P0 or P1 needs a run)",
    )
    p.add_argument("run")
    p.add_argument("finding")
    p.add_argument("--commit", required=True, help="the commit that fixed it")
    p.add_argument("--note", required=True, help="what was fixed")

    p = command(
        "probe-review",
        cmd_probe_review,
        "review a P0's red probe and flip as the operator: overrides the verifier, kept as example",
    )
    p.add_argument("run")
    p.add_argument("finding")
    p.add_argument("--probe", choices=report.REVIEWS[1:], default="")
    p.add_argument("--flip", choices=report.REVIEWS[1:], default="")
    p.add_argument("--why", required=True)

    p = command(
        "simplify-queue", cmd_simplify_queue, "list what simplify passes left alone, to decide"
    )
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")
    p.add_argument("--all", action="store_true", help="also the rejected and done items")
    p.add_argument("--lens", default="", help="only one lens: simplify or an audit lens")
    p.add_argument("--severity", default="", help="only these severities, e.g. P0,P1")

    command("audit-rules", cmd_audit_rules, "download the semgrep rules of the nightly audit")

    p = sub.add_parser("rules", help="architecture rules: the ratchet and the whole-repo count")
    checks = p.add_subparsers(dest="rules_command", metavar="<rules command>", required=True)
    p = checks.add_parser(
        "check", help="exit 1 when --head adds rule violations over --base, per rule and file"
    )
    p.set_defaults(handler=cmd_rules_check)
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")
    p.add_argument("--base", required=True, help="the commit the change starts from")
    p.add_argument("--head", required=True, help="the commit of the change")
    p.add_argument("--config", help="a YAML file holding only `rules:`, used instead of verify.yml")
    p = checks.add_parser("baseline", help="count every violation at HEAD: per rule, top files")
    p.set_defaults(handler=cmd_rules_baseline)
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")
    p.add_argument("--config", help="a YAML file holding only `rules:`, used instead of verify.yml")

    p = command(
        "simplify-decide",
        cmd_simplify_decide,
        "accept (the next simplify pass fixes it) or reject a simplify queue item",
    )
    p.add_argument("item", help="the item id, e.g. S-1a2b3c")
    p.add_argument("decision", choices=backlog.DECISIONS)
    p.add_argument("--why", required=True)
    p.add_argument("--repo", default=".", help="repository path (default: current directory)")

    p = command("verify-p0", cmd_verify_p0, "let the verifier judge a finished run's open P0")
    p.add_argument("run", help="run id or unique prefix")
    p = command("remerge", cmd_remerge, "recompute a run's findings from its own QA output")
    p.add_argument("run")
    p.add_argument("--reason", required=True)

    p = command("cancel", cmd_cancel, "cancel a queued or running run")
    p.add_argument("run")

    p = command(
        "prune",
        cmd_prune,
        "delete the bulk and logs of old finished runs, stale containers and caches; "
        "the runner does it daily and when the free disk is low",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print what would be freed; delete nothing"
    )

    command(
        "doctor",
        cmd_doctor,
        "check docker, systemd, uv, git, claude, disk (runs wait below 10 GiB free), ports, images",
    )
    command("enable", cmd_enable, "activate qg: the runner starts executing runs")
    command("disable", cmd_disable, "deactivate qg: the runner executes nothing new")
    command("runner", cmd_runner, "the daemon loop (systemd runs this)")
    command("board", cmd_board, "write the static HTML board of the last verdict per branch")
    command(
        "install-service", cmd_install_service, "install the systemd user unit; enables nothing"
    )

    p = command("cycle-page", cmd_cycle_page, "rebuild a run's cycle page; print path and URL")
    p.add_argument("run")

    p = command("topics", cmd_topics, "rebuild a run's topic pages; print path and URL of each")
    p.add_argument("run")

    p = command("topic-artifact", cmd_topic_artifact, "store the artifact URL of a topic page")
    p.add_argument("cycle")
    p.add_argument("slug")
    p.add_argument("url")

    p = command("cycle-artifact", cmd_cycle_artifact, "store the artifact URL of a cycle")
    p.add_argument("cycle")
    p.add_argument("url")

    p = command("prd-artifact", cmd_prd_artifact, "store the artifact URL of a PRD")
    p.add_argument("prd")
    p.add_argument("url")

    p = command("cycle-assign", cmd_cycle_assign, "map a legacy cycle to a PRD phase (one-off)")
    p.add_argument("cycle")
    p.add_argument("prd")
    p.add_argument("phase")

    p = command(
        "shadow-report",
        cmd_shadow_report,
        "count the shadow window and say whether the cutover exit criterion holds",
    )
    p.add_argument("--project", help="project name (default: every project in shadow.jsonl)")
    p.add_argument("--k", type=int, default=5, help="check lines the window needs (default 5)")
    p.add_argument("--r", type=int, default=3, help="release lines the window needs (default 3)")
    p.add_argument("--m", type=int, default=3, help="deploys to hold after the flip (default 3)")

    p = command("prd", cmd_prd, "print every phase of a PRD: state, last run, artifact, deploy")
    p.add_argument("prd")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point of `qg`; the runner's own `_execute <run>` call bypasses the parser."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] == "_execute":
        return executor.execute(queue.resolve(arguments[1]))
    args = build_parser().parse_args(arguments)
    if shim.active(args.command):
        return shim.forward(args)
    if args.command == "check" and shim.shadow_enabled():
        return shim.shadow_check(args, args.handler)
    if args.command == "verify" and args.release and shim.shadow_enabled():
        return shim.shadow_release(args, args.handler)
    if args.command == "wait" and shim.shadow_enabled():
        return shim.shadow_wait(args, args.handler)
    return args.handler(args)
