"""Forwards release commands to `resoluto` (QG_BACKEND=resoluto); QG_SHADOW=1 compares verdicts."""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from qg import cycles, git, paths, queue, report
from qg.util import atomic_write, die, now_iso

COMMANDS = ("verify", "wait", "check", "topics", "topic-artifact", "cycle-artifact")
BINARY_ENV = "QG_RESOLUTO_BIN"
ENGINE_DIR_ENV = "QG_RESOLUTO_ENGINE_DIR"
CLI_DIR_ENV = "QG_RESOLUTO_CLI_DIR"
REPO_ENV = "QG_RESOLUTO_REPO"
CHECK_EXIT = {0: 0, 5: 1, 6: 1}
WAIT_EXIT = {0: 0, 5: 1, 130: 2}
PASSTHROUGH = ("verify", "topics", "topic-artifact", "cycle-artifact")
VERIFY_UNSUPPORTED = {
    "plan": "--plan (phases and QA plans are tracked files matched by release.phase_globs)",
    "base": "--base (phases and QA plans are tracked files matched by release.phase_globs)",
    "reopen": "--reopen (a release never reopens a cycle)",
    "no_qa": "--no-qa (a release always runs the phase QA)",
    "simplify": "--simplify (dropped: there is no resoluto equivalent)",
    "allow_manual": "--allow-manual (QA plans are checked by the harness rules)",
}


def backend() -> str:
    """Returns the backend named by QG_BACKEND (`qg` when unset); dies on any other value."""
    name = os.environ.get("QG_BACKEND", "") or "qg"
    if name not in ("qg", "resoluto"):
        die(f"QG_BACKEND={name!r}: expected qg or resoluto")
    return name


def active(command: str) -> bool:
    """True when `command` is forwarded to resoluto: QG_BACKEND=resoluto and a shimmed command."""
    return backend() == "resoluto" and command in COMMANDS


def shadow_enabled() -> bool:
    """True when QG_SHADOW=1 asks `qg check` to also compute resoluto's verdict."""
    return os.environ.get("QG_SHADOW") == "1"


def argv_of(args: argparse.Namespace) -> list[str]:
    """Maps a parsed qg command to the argv of the `resoluto` command after its binary name."""
    command = args.command
    if command == "verify":
        return _verify_argv(args)
    if command == "wait":
        if args.timeout is not None:
            die(
                "wait --timeout is not supported with QG_BACKEND=resoluto: "
                "resoluto waits on events, never on a clock"
            )
        return ["wait", args.run]
    if command == "check":
        return _check_argv(args, dry_run=args.dry_run)
    if command == "topics":
        return ["topics", "show", args.run]
    if command == "topic-artifact":
        return ["topics", "artifact", phase_of_cycle(args.cycle), args.slug, args.url]
    return ["cycles", "artifact", phase_of_cycle(args.cycle), args.url]


def phase_of_cycle(cycle: str) -> str:
    """Returns the PRD:PHASE of a qg cycle id (slug plus hash); an argument that already holds a
    colon is a PRD:PHASE and passes through. Dies for a cycle that has no PRD and phase."""
    if ":" in cycle:
        return cycle
    found = cycles.load(cycles.resolve(cycle))
    if found.prd == "RELEASE":
        die(f"cycle {found.id} is a release run's own cycle: resoluto has no such cycle")
    if not found.prd or not found.phase:
        die(
            f"cycle {found.id} ({found.feature!r}) belongs to no PRD phase: it is a legacy "
            "feature cycle and resoluto has no equivalent; assign it with qg cycle-assign"
        )
    return f"{found.prd}:{found.phase}"


def resoluto_repo() -> list[str]:
    """The `--repo` flag naming the resoluto repository a qg worktree is: QG_RESOLUTO_REPO, else
    `.`, the label of a flat project whose one repository is the workspace root (how qg sees a
    repo). Release records are named by it, so `check` asks about the repo they name."""
    return ["--repo", os.environ.get(REPO_ENV) or "."]


def _verify_argv(args: argparse.Namespace) -> list[str]:
    """Maps `qg verify`: --release --phases L, or --prd X --phase Y as a one-phase release."""
    if args.plan_dir:
        die(
            "--plan-dir is not supported with QG_BACKEND=resoluto: the phases are the files "
            "matched by release.phase_globs in the harness"
        )
    if bool(args.prd) != bool(args.phase):
        die("verify needs --prd and --phase together: they name one phase")
    refused = [flag for name, flag in VERIFY_UNSUPPORTED.items() if getattr(args, name)]
    if refused:
        die(f"verify {', '.join(refused)} has no resoluto equivalent")
    if args.prd:
        if args.release or args.phases:
            die("verify --prd/--phase is one phase: it takes neither --release nor --phases")
        phases = f"{args.prd.upper()}:{args.phase.upper()}"
    else:
        if not args.release:
            die(
                "verify without --release is not supported with QG_BACKEND=resoluto: pass "
                "--release --phases L, or --prd X --phase Y for one phase (--feature has no "
                "equivalent)"
            )
        if not args.phases:
            die("verify --release needs --phases")
        phases = args.phases
    argv = [
        "verify",
        "--release",
        "--ref",
        args.ref,
        "--phases",
        phases,
        "--path",
        args.repo,
        *resoluto_repo(),
        "--env",
        args.env,
        "--stress",
        str(args.stress),
    ]
    if args.force:
        argv.append("--force")
    if args.dry_run:
        argv.append("--dry-run")
    return argv


def _check_argv(args: argparse.Namespace, dry_run: bool, as_json: bool = False) -> list[str]:
    """Maps `qg check` flags; qg's --repo is a worktree path, which resoluto calls --path, and
    resoluto's --repo is the repository label its records name (`resoluto_repo`)."""
    argv = ["check", args.sha, "--env", args.env, "--path", args.repo, *resoluto_repo()]
    if args.unverified:
        argv += ["--unverified", args.unverified]
    if dry_run:
        argv.append("--dry-run")
    if as_json:
        argv += ["-o", "json"]
    return argv


def binary() -> str:
    """Returns the resoluto executable (QG_RESOLUTO_BIN or PATH); dies when absent."""
    found = shutil.which(os.environ.get(BINARY_ENV) or "resoluto")
    if found is None:
        die("resoluto is not on PATH (QG_BACKEND=resoluto)")
    return found


def exit_code(command: str, code: int) -> int:
    """Maps a resoluto exit code to qg's for `command`; check and wait fail closed on others."""
    if command == "check":
        table = CHECK_EXIT
    elif command == "wait":
        table = WAIT_EXIT
    else:
        return code
    if code in table:
        return table[code]
    print(f"qg: resoluto {command} exited {code}", file=sys.stderr)
    return 1


def forward(args: argparse.Namespace) -> int:
    """Runs the mapped resoluto command with inherited stdio and returns qg's exit code."""
    argv = argv_of(args)
    done = subprocess.run([binary(), *argv], check=False)
    return exit_code(args.command, done.returncode)


def versions() -> tuple[dict[str, str], list[str]]:
    """Returns the HEAD commit of the installed resoluto engine, resoluto cli and this shim, and
    the problems met reading them (the checkouts come from QG_RESOLUTO_ENGINE_DIR and
    QG_RESOLUTO_CLI_DIR)."""
    found: dict[str, str] = {}
    problems: list[str] = []
    sources = (
        ("resoluto_engine", os.environ.get(ENGINE_DIR_ENV, ""), ENGINE_DIR_ENV),
        ("resoluto_cli", os.environ.get(CLI_DIR_ENV, ""), CLI_DIR_ENV),
        ("shim", str(Path(__file__).resolve().parent), "the qg checkout"),
    )
    for key, directory, label in sources:
        if not directory:
            found[key] = ""
            problems.append(f"{label} is not set: the commit of {key} is unknown")
            continue
        done = subprocess.run(
            ["git", "-C", directory, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        found[key] = done.stdout.strip() if done.returncode == 0 else ""
        if done.returncode != 0:
            problems.append(f"{key}: {directory} is not a git checkout: {done.stderr.strip()}")
    return found, problems


def _append(line: dict) -> None:
    """Appends one JSON line to shadow.jsonl."""
    paths.SHADOW.parent.mkdir(parents=True, exist_ok=True)
    with paths.SHADOW.open("a") as log:
        log.write(json.dumps(line) + "\n")


def _tee(call: Callable[[], int]) -> tuple[int, str]:
    """Runs `call`, echoing what it printed once it returns; returns its code and its output."""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = call()
    finally:
        sys.stdout.write(buffer.getvalue())
    return code, buffer.getvalue()


def shadow_check(args: argparse.Namespace, qg_check: Callable[[argparse.Namespace], int]) -> int:
    """Runs qg's check, then resoluto's with --dry-run, appends both verdicts to shadow.jsonl and
    returns qg's exit code; an --unverified override is not shadowed."""
    if args.unverified:
        return qg_check(args)
    code, printed = _tee(lambda: qg_check(args))
    repo = git.canonical_repo(args.repo)
    sha = git.resolve_commit(args.repo, args.sha)
    tree = git.tree_of(repo, sha)
    reasons = [
        f"qg: {line.strip()[2:]}" for line in printed.splitlines() if line.startswith("  - ")
    ]
    verdict, found = _resoluto_verdict(args)
    reasons += [f"resoluto: {reason}" for reason in found]
    commits, problems = versions()
    if problems:
        verdict = "error"
        reasons += [f"resoluto: {problem}" for problem in problems]
    _append(
        {
            "at": now_iso(),
            "project": Path(repo).name,
            "repo": repo,
            "tree": tree,
            "env": args.env,
            "qg": "ok" if code == 0 else "not_verified",
            "resoluto": verdict,
            "reasons": reasons,
            "kind": "check",
            "qg_run": "",
            "resoluto_run": "",
            "phases": {},
            **commits,
        }
    )
    return code


def _resoluto_verdict(args: argparse.Namespace) -> tuple[str, list[str]]:
    """Returns resoluto's verdict for the check (ok, not_verified or error) and its reasons."""
    found = shutil.which(os.environ.get(BINARY_ENV) or "resoluto")
    if found is None:
        return "error", ["resoluto is not on PATH"]
    done = subprocess.run(
        [found, *_check_argv(args, dry_run=True, as_json=True)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode not in (0, 5):
        return "error", [f"resoluto check exited {done.returncode}: {done.stderr.strip()}"]
    report_ = json.loads(done.stdout)
    reasons = [*report_["problems"], *(f"finding: {item}" for item in report_["findings"])]
    return ("ok" if done.returncode == 0 else "not_verified"), reasons


def _pending() -> list[dict]:
    """Returns the dual runs whose two ends have not both been recorded."""
    if not paths.SHADOW_PENDING.exists():
        return []
    return [json.loads(row) for row in paths.SHADOW_PENDING.read_text().splitlines() if row]


def _save_pending(rows: list[dict]) -> None:
    """Rewrites shadow-pending.jsonl with `rows`."""
    paths.SHADOW_PENDING.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.SHADOW_PENDING, "".join(json.dumps(row) + "\n" for row in rows))


def _open_resoluto_release(args: argparse.Namespace, sha: str) -> tuple[str, str]:
    """Opens resoluto's release of the same ref and phases; returns its run id and the problem
    that left it without one."""
    found = shutil.which(os.environ.get(BINARY_ENV) or "resoluto")
    if found is None:
        return "", "resoluto is not on PATH"
    argv = ["verify", "--release", "--ref", sha, "--phases", args.phases, "--path", args.repo]
    argv += resoluto_repo()
    argv += ["--stress", str(args.stress), "-o", "json"]
    done = subprocess.run([found, *argv], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return "", f"resoluto verify exited {done.returncode}: {done.stderr.strip()}"
    opened = json.loads(done.stdout)
    if not opened.get("run_id"):
        return "", f"resoluto verify {opened.get('status')} opened no run"
    return str(opened["run_id"]), ""


def shadow_release(args: argparse.Namespace, qg_verify: Callable[[argparse.Namespace], int]) -> int:
    """Runs qg's release verify, then opens resoluto's release of the same ref and phases and
    records the pair as pending; `qg wait` on the qg run completes it. Returns qg's exit code."""
    before = {run.id for run in queue.list_runs()}
    code, _ = _tee(lambda: qg_verify(args))
    if code != 0 or args.dry_run:
        return code
    sha = git.resolve_commit(args.repo, args.ref)
    opened = [
        run
        for run in queue.list_runs()
        if run.id not in before and run.mode == "release" and not run.release
    ]
    if len(opened) != 1:
        print("qg shadow: no new qg release run to pair (deduplicated?)", file=sys.stderr)
        return code
    repo = git.canonical_repo(args.repo)
    run, problem = _open_resoluto_release(args, sha)
    _save_pending(
        [
            *_pending(),
            {
                "qg_run": opened[0].id,
                "resoluto_run": run,
                "error": problem,
                "project": Path(repo).name,
                "repo": repo,
                "tree": git.tree_of(repo, sha),
                "env": args.env,
            },
        ]
    )
    return code


def _qg_release(run_id: str) -> tuple[str, dict[str, str], list[str]]:
    """Returns qg's verdict of a finished release run, of each of its phases, and its reasons."""
    state = queue.load(run_id)
    verdict, reasons = report.verdict_of(state, queue.load_request(run_id))
    phases: dict[str, str] = {}
    for child in queue.list_runs():
        if child.release != run_id:
            continue
        request = queue.load_request(child.id)
        said, _ = report.verdict_of(child, request)
        phases[f"{request.prd}:{request.phase}".upper()] = (
            "ok" if said == report.READY else "not_verified"
        )
    return (
        ("ok" if verdict == report.READY else "not_verified"),
        phases,
        [f"qg: {r}" for r in reasons],
    )


def _resoluto_release(run_id: str, problem: str) -> tuple[str, dict[str, str], list[str]]:
    """Returns resoluto's verdict of a release run (waiting for it to end), of each phase of its
    records, and its reasons; `error` when it has no verdict."""
    if not run_id:
        return "error", {}, [f"resoluto: {problem}"]
    found = shutil.which(os.environ.get(BINARY_ENV) or "resoluto")
    if found is None:
        return "error", {}, ["resoluto: resoluto is not on PATH"]
    done = subprocess.run(
        [found, "wait", run_id, "-o", "json"], capture_output=True, text=True, check=False
    )
    if done.returncode not in (0, 5):
        return "error", {}, [f"resoluto: wait exited {done.returncode}: {done.stderr.strip()}"]
    waited = json.loads(done.stdout)
    if waited["verdict"] == "none":
        return "error", {}, [f"resoluto: run {run_id} ended {waited['status']} with no records"]
    phases: dict[str, str] = {}
    reasons: list[str] = []
    for summary in waited["records"]:
        reasons += [f"resoluto: {reason}" for reason in summary["reasons"]]
        for phase in summary["record"]["phases"]:
            label = phase["label"].upper()
            passed = phase["passed"] and phases.get(label, "ok") == "ok"
            phases[label] = "ok" if passed else "not_verified"
    return ("ok" if waited["verdict"] == "ready" else "not_verified"), phases, reasons


def shadow_wait(args: argparse.Namespace, qg_wait: Callable[[argparse.Namespace], int]) -> int:
    """Runs qg's wait; when it ends a release run that has a pending resoluto pair, waits for
    that run, appends the `release` line to shadow.jsonl and drops the pending entry. Returns
    qg's exit code."""
    code = qg_wait(args)
    if code not in (0, 1):
        return code
    run_id = queue.resolve(args.run)
    pending = [row for row in _pending() if row["qg_run"] == run_id]
    if not pending:
        return code
    row = pending[0]
    qg_verdict, qg_phases, qg_reasons = _qg_release(run_id)
    verdict, found_phases, found = _resoluto_release(row["resoluto_run"], row["error"])
    commits, problems = versions()
    if problems:
        verdict = "error"
        found += [f"resoluto: {problem}" for problem in problems]
    labels = sorted({*qg_phases, *found_phases})
    _append(
        {
            "at": now_iso(),
            "project": row["project"],
            "repo": row["repo"],
            "tree": row["tree"],
            "env": row["env"],
            "qg": qg_verdict,
            "resoluto": verdict,
            "reasons": [*qg_reasons, *found],
            "kind": "release",
            "qg_run": run_id,
            "resoluto_run": row["resoluto_run"],
            "phases": {
                label: {
                    "qg": qg_phases.get(label, "absent"),
                    "resoluto": found_phases.get(label, "absent"),
                }
                for label in labels
            },
            **commits,
        }
    )
    _save_pending([item for item in _pending() if item["qg_run"] != run_id])
    return code
