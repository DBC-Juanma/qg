"""Tells the antfarm coordinator, through its inbox file, that a run reached its final verdict."""

import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from qg import cycles, paths, queue, release, report
from qg.queue import RunState

REASONS_LIMIT = 300
QA_NOT_RUN = (
    "QA not run: the verdict covers the other stages only, never a deploy "
    "(QA runs at phase close, --prd and --phase, or at release)"
)


def inbox() -> Path:
    """Returns the antfarm inbox file, under $ANTFARM_DIR or ~/.local/state/antfarm."""
    root = os.environ.get("ANTFARM_DIR")
    return (Path(root) if root else Path.home() / ".local/state/antfarm") / "inbox.jsonl"


def _clip(text: str) -> str:
    """Returns the text cut to REASONS_LIMIT characters."""
    return text if len(text) <= REASONS_LIMIT else text[: REASONS_LIMIT - 3] + "..."


def _summary(state: RunState) -> tuple[str, str]:
    """Returns the verdict word and the one-line reasons of a final run or release."""
    verdict, reasons = report.verdict_of(state, queue.load_request(state.id))
    if state.status in ("passed", "failed"):
        word = verdict
    else:
        word = state.status.upper()
    if state.mode == "release":
        phases = "; ".join(
            f"{release.phase_label(kid)} {report.display_verdict(kid)}"
            for kid in release.children(state.id)
        )
        reasons = [f"phases: {phases}", *reasons] if phases else reasons
    if report.qa_notice(state):
        reasons = [QA_NOT_RUN, *reasons]
    return word, _clip("; ".join(reasons) if reasons else "all checks passed")


def _minor_note(state: RunState) -> str:
    """Returns the clause on the open P2 and P3 findings of a run or of its release's phases:
    fixed in the working tree and closed with `qg fixed`, never with a run; empty when none."""
    runs = release.children(state.id) if state.mode == "release" else [state]
    minor = [
        f"{f.id} (run {run.id})"
        for run in runs
        for f in report.load_findings(run.id)
        if f.severity in ("P2", "P3") and report.status_word(f) == "open"
    ]
    if not minor:
        return ""
    return (
        f"; {len(minor)} open P2/P3: {', '.join(minor[:5])}: fix them in the working tree and "
        "close each with qg fixed <run> <finding> --commit <sha> --note <text>, no run"
    )


def _target(state: RunState) -> RunState | None:
    """Returns the run whose verdict is final and announced now, or None while it is not."""
    if state.mode == "phase" and state.release:
        if not paths.state_file(state.release).exists():
            return state
        parent = queue.load(state.release)
        if parent.status == "superseded":
            return state
        state = parent
    if state.status not in queue.FINISHED or state.status == "superseded":
        return None
    if state.mode == "release":
        kids = release.children(state.id)
        if any(kid.status not in queue.FINISHED for kid in kids):
            return None
        pairs = release.phase_pairs(state)
        declared = {cycles.identify_phase(state.repo, prd, phase) for prd, phase in pairs}
        if state.status in ("passed", "failed") and not declared <= {kid.cycle for kid in kids}:
            return None
    return state


def _append_once(run_id: str, entry: dict[str, str]) -> None:
    """Appends one JSON line to the inbox, then marks the run notified, under one exclusive lock.

    The marker exists only after the line is written, so a failed append leaves the notice to be
    retried and a concurrent finalizer finds the marker only once the line is there.
    """
    target = inbox()
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = paths.run_dir(run_id) / "notified"
    with open(target, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if marker.exists():
            return
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        marker.touch()


def finished(run_id: str) -> None:
    """Announces the run's final verdict once; a release speaks only when all its phases are final.

    A write failure is logged and never reaches the run.
    """
    try:
        target = _target(queue.load(run_id))
        if target is None or (paths.run_dir(target.id) / "notified").exists():
            return
        word, reasons = _summary(target)
        text = (
            f"{Path(target.repo).name} run {target.id} {word} on {target.sha[:8]} "
            f"({target.feature}): {reasons}{_minor_note(target)}; "
            f"report {paths.report_md(target.id)}"
        )
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session": "qg",
            "cwd": target.repo,
            "kind": "done" if word == report.READY else "failed",
            "text": text,
        }
        _append_once(target.id, entry)
    except Exception as exc:
        print(f"qg: antfarm notice for run {run_id} failed: {exc!r}", file=sys.stderr)
