"""Locations under the qg state root and the run ids that name them."""

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

STATE_ROOT = Path(os.environ.get("QG_STATE_ROOT") or Path.home() / ".local" / "state" / "qg")
QUEUE_LOCK = STATE_ROOT / "queue.lock"
PLAYWRIGHT_LOCK = STATE_ROOT / "playwright.lock"
QA_BOOT_LOCK = STATE_ROOT / "qa-boot.lock"
MEMINFO = Path("/proc/meminfo")
RUNS = STATE_ROOT / "runs"
CYCLES = STATE_ROOT / "cycles"
CACHE = STATE_ROOT / "cache"
CALIBRATION = STATE_ROOT / "calibration"
SIMPLIFY_QUEUE = STATE_ROOT / "simplify-queue"
AUDIT_RULES = STATE_ROOT / "audit" / "semgrep"
AUDIT_LEDGER = STATE_ROOT / "audit" / "ledger"
VENVS = CACHE / "venvs"
NODE = CACHE / "node"
NPM = CACHE / "npm"
ENABLED = STATE_ROOT / "enabled"
OVERRIDES = STATE_ROOT / "overrides.log"
SHADOW = STATE_ROOT / "shadow.jsonl"
SHADOW_PENDING = STATE_ROOT / "shadow-pending.jsonl"
DEPLOYED = STATE_ROOT / "deployed.json"
BOARD = STATE_ROOT / "board.html"
SETTINGS = STATE_ROOT / "settings.json"
PORT_RANGE = range(21000, 22000)
SERVICE_UNIT = "qg-runner.service"
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def run_dir(run_id: str) -> Path:
    """Returns the directory that holds everything of run `run_id`."""
    return RUNS / run_id


def state_file(run_id: str) -> Path:
    """Returns the path of the run's mutable state.json."""
    return run_dir(run_id) / "state.json"


def request_file(run_id: str) -> Path:
    """Returns the path of the run's immutable request.json."""
    return run_dir(run_id) / "request.json"


def logs_dir(run_id: str) -> Path:
    """Returns the directory of the run's per-stage logs."""
    return run_dir(run_id) / "logs"


def src_dir(run_id: str) -> Path:
    """Returns the path of the run's detached worktree."""
    return run_dir(run_id) / "src"


def report_md(run_id: str) -> Path:
    """Returns the path of the run's markdown report."""
    return run_dir(run_id) / "report.md"


def report_json(run_id: str) -> Path:
    """Returns the path of the run's JSON report."""
    return run_dir(run_id) / "report.json"


def findings_file(run_id: str) -> Path:
    """Returns the path of the run's findings.json, written by the QA stage."""
    return run_dir(run_id) / "findings.json"


def qa_file(run_id: str) -> Path:
    """Returns the path of the run's qa.json, the flows the QA stage walked."""
    return run_dir(run_id) / "qa.json"


def qa_dir(run_id: str) -> Path:
    """Returns the working directory of the run's QA agent: charter, findings, MCP config."""
    return run_dir(run_id) / "qa"


def qa_home(run_id: str) -> Path:
    """Returns the home directory of the run's confined QA agent, outside its working directory."""
    return run_dir(run_id) / "qa-home"


def plan_file(run_id: str) -> Path:
    """Returns the path of the run's plan.md, generated from the commits since the base."""
    return run_dir(run_id) / "plan.md"


def simplify_dir(run_id: str) -> Path:
    """Returns the directory of the S0 agent's diff, summary and output."""
    return run_dir(run_id) / "simplify"


def audit_dir(run_id: str) -> Path:
    """Returns the directory of the nightly audit's own files: tool hits, the claims the refuter
    judges and audit.json; no agent may write it, each one writes only its own subdirectory."""
    return run_dir(run_id) / "audit"


def simplify_home(run_id: str) -> Path:
    """Returns the home directory of the run's confined S0 agent."""
    return run_dir(run_id) / "simplify-home"


def probe_dir(run_id: str, finding_id: str) -> Path:
    """Returns the directory of one P0's probe: its card, file, run output and agent's home."""
    return run_dir(run_id) / "probes" / finding_id


def probe_log(run_id: str, finding_id: str) -> Path:
    """Returns the output of the last run of one P0's probe."""
    return probe_dir(run_id, finding_id) / "run.log"


def flip_patch(run_id: str, finding_id: str) -> Path:
    """Returns the patch of one P0's green flip."""
    return probe_dir(run_id, finding_id) / "flip.patch"


def calibration_dir(run_id: str, finding_id: str) -> Path:
    """Returns where the operator's review of one P0's probe and flip is kept, outside the run."""
    name = f"{run_id}-{finding_id}"
    if "/" in name or name.startswith(".") or ".." in name:
        raise ValueError(f"not a run and finding id: {name!r}")
    return CALIBRATION / name


def evidence_dir(run_id: str) -> Path:
    """Returns the directory the QA agent's screenshots and the log excerpts are saved in."""
    return run_dir(run_id) / "evidence"


def cycle_dir(cycle_id: str) -> Path:
    """Returns the directory that holds a cycle's json, attempts, thumbnails and page."""
    return CYCLES / cycle_id


def cycle_file(cycle_id: str) -> Path:
    """Returns the path of the cycle's cycle.json."""
    return cycle_dir(cycle_id) / "cycle.json"


def page_file(cycle_id: str) -> Path:
    """Returns the path of the cycle's published page."""
    return cycle_dir(cycle_id) / "page.html"


def attempts_dir(cycle_id: str) -> Path:
    """Returns the directory of the cycle's per-attempt snapshots."""
    return cycle_dir(cycle_id) / "attempts"


def topics_file(cycle_id: str) -> Path:
    """Returns the path of the cycle's topics.json, which holds each topic's artifact URL."""
    return cycle_dir(cycle_id) / "topics.json"


def topics_dir(cycle_id: str) -> Path:
    """Returns the directory of the cycle's topic pages and their index."""
    return cycle_dir(cycle_id) / "topics"


def topic_page(cycle_id: str, slug: str) -> Path:
    """Returns the path of one topic's page."""
    return topics_dir(cycle_id) / slug / "page.html"


def thumbs_dir(cycle_id: str) -> Path:
    """Returns the directory of the cycle's cached evidence thumbnails."""
    return cycle_dir(cycle_id) / "thumbs"


def scope_unit(run_id: str) -> str:
    """Returns the name of the systemd scope that holds the run's executor."""
    return f"qg-{run_id}.scope"


def is_enabled() -> bool:
    """Tells whether the operator has activated qg with `qg enable`."""
    return ENABLED.exists()


def new_run_id(after: str = "") -> str:
    """Returns a time-prefixed unique run id such as 20261014-120102-ab12, sorting after `after`."""
    while True:
        found = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        if found > after:
            return found


def ensure() -> None:
    """Creates the state-root directories every writer relies on."""
    for directory in (RUNS, CYCLES, VENVS, NODE, NPM):
        directory.mkdir(parents=True, exist_ok=True)
