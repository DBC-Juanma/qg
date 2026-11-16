"""S5, the manual QA pass: boot the stack, brief a confined headless Claude Code tester, judge it."""

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TextIO
from urllib.parse import urlsplit

from qg import (
    admission,
    cycle_page,
    cycles,
    git,
    jail,
    paths,
    proc,
    queue,
    redact,
    release,
    sandbox,
    settings,
    stack,
    stages,
    twins,
)
from qg.config import Config, Qa, parse
from qg.cycle_page import Merged
from qg.names import closest
from qg.plan import MAX_TOPICS
from qg.queue import Request
from qg.report import RUNNER_PREFIXES, SEVERITIES, Finding, load_findings, save_findings
from qg.sandbox import Sandbox, base_env
from qg.util import atomic_write, fill

MCP_VERSION = "0.0.41"
BOOT_SECONDS = 600
GRACE_SECONDS = 30
WIDTHS = (1280, 390)
LOCALES = ("en", "es", "vi")
ALLOWED_TOOLS = "mcp__playwright,Read,Write"
DISALLOWED_TOOLS = "mcp__playwright__browser_file_upload,mcp__playwright__browser_install"
PROMPT_FILE = Path(__file__).with_name("qa_prompt.md")
QA_MODEL = "sonnet"
MIN_AGENT_MINUTES = 7
MIN_AGENT_TURNS = 30
CLOCK_FILE = "clock.txt"
CLOCK_TICK_SECONDS = 10
STOP_MARGIN_SECONDS = 120
EARLY_FRACTION = 0.75
PLAN_LIMIT = 60_000
STAT_LINES = 200
LOG_WINDOW_BYTES = 16 << 20
EXCERPT_LINES = 300
MAX_LOG_FINDINGS = 40
SHOWN_ITEMS = 6
TRACEBACK = "Traceback (most recent call last):"
CHAIN = re.compile(r"^(During handling of the above exception|The above exception was)")
ERROR_LINE = re.compile(r"(?:^|\s)ERROR(?::|\s)")
ACCESS_5XX = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" (5\d\d)\b')
NUMBER = re.compile(r"\d+")
IDENT = re.compile(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,})*")
TMP_PATH = re.compile(r"(?:/tmp|/dev/shm|/var/tmp|/var/folders|/run/user/\d+)/[^\s'\"):,]*")
NEW_ID = {"F": re.compile(r"^F-\d+$"), "I": re.compile(r"^I-\d+$")}
RESERVED_PREFIXES = (*RUNNER_PREFIXES, "W-")
BLOCKED_RESULTS = ("blocked", "not reachable")
WALKED_RESULTS = ("walked", "failed", *BLOCKED_RESULTS)
PLAN_PREFIX = release.PLAN_PREFIX
PLAN_RESULTS = ("walked", "blocked", "not reachable", "failed")
NO_CHANGE_FLOW = release.NO_CHANGE_FLOW
MIN_PLAN_LOCALES = 1
RETEST_RESULTS = ("fixed", "still_present", "cannot_reproduce")
RECHECKED = ("P0", "P1")
RETEST = release.RETEST
is_plan = release.is_plan
RETEST_STEPS_RULE = (
    "A `retest <id>` entry also carries `steps` like a plan flow: one screenshot of this run",
    "per width you tested, each step with its caption, width, locale and screenshot, so the",
    "report shows the proof under that retest.",
)
SCREENSHOT_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


class QaFailure(Exception):
    """A reason the QA stage cannot go on; its text becomes the stage summary."""


@dataclass(frozen=True)
class QaOutcome:
    """What the stage reports back to the executor."""

    status: str
    summary: str
    cost_usd: float | None
    turns: int | None
    flows: list[str]


@dataclass(frozen=True)
class AgentRun:
    """How the headless Claude Code process ended."""

    code: int
    timed_out: bool


@dataclass(frozen=True)
class Step:
    """One step of a plan flow: what its screenshot proves, at which width and locale."""

    caption: str
    width: int
    screenshot: str
    locale: str = ""


@dataclass(frozen=True)
class Walked:
    """One flow, or one retest of an earlier finding, the agent says it walked."""

    flow: str
    widths: list[int]
    locales: list[str]
    result: str
    notes: str
    evidence: list[str]
    summary: str = ""
    steps: list[Step] = field(default_factory=list)


@dataclass(frozen=True)
class AgentFindings:
    """The validated content of the agent's findings.json; `dropped` names the invalid entries."""

    walked: list[Walked]
    findings: list[Finding]
    renumbered: tuple[tuple[str, str], ...] = ()
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Charter:
    """The charter text and what the agent must account for in `walked`."""

    text: str
    required: tuple[str, ...]
    plan_required: bool = False
    topics: int = 0
    headings: tuple[str, ...] = ()


def read_secrets(path: str | None, keys: tuple[str, ...]) -> dict[str, str]:
    """Reads only `keys` from the KEY=VALUE lines of the env file `path`; no shell is involved."""
    if not keys or path is None:
        return {}
    file = Path(path).expanduser()
    if not file.is_file():
        raise QaFailure(f"secrets_env_file {path} does not exist")
    found = redact.parse_env(file.read_text())
    missing = [key for key in keys if not found.get(key)]
    if missing:
        raise QaFailure(f"{', '.join(missing)} missing or empty in {path}")
    return {key: found[key] for key in keys}


def origin(url: str) -> str:
    """Returns scheme://host[:port] of `url`, the form the browser's --allowed-origins takes."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise QaFailure(f"{url!r} is not an absolute URL: no origin to allow the browser")
    return f"{parts.scheme}://{parts.netloc}"


def allowed_origins(qa: Qa, values: dict[str, str]) -> tuple[str, ...]:
    """Returns the origins of the run's app, api and surfaces, the only ones the browser may use."""
    urls = (qa.app_url, qa.api_url, *(surface.url for surface in qa.surfaces))
    return tuple(dict.fromkeys(origin(fill(url, values)) for url in urls))


def mcp_config(evidence: Path, npx: str, origins: tuple[str, ...]) -> dict[str, Any]:
    """Returns the MCP config: the pinned Playwright MCP, isolated, headless, held to `origins`."""
    return {
        "mcpServers": {
            "playwright": {
                "command": npx,
                "args": [
                    "-y",
                    f"@playwright/mcp@{MCP_VERSION}",
                    "--isolated",
                    "--headless",
                    "--viewport-size",
                    f"{WIDTHS[0]}x800",
                    "--output-dir",
                    str(evidence),
                    "--allowed-origins",
                    ";".join(origins),
                ],
            }
        }
    }


def claude_command(
    claude: Path,
    prompt: str,
    mcp_path: Path,
    max_turns: int,
    max_cost_usd: float,
    allowed_tools: str = ALLOWED_TOOLS,
) -> list[str]:
    """Returns the headless Claude Code command: no settings, hooks or shell, one MCP, no upload."""
    return [
        str(claude),
        "-p",
        prompt,
        "--model",
        QA_MODEL,
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        f"{max_cost_usd:g}",
        "--output-format",
        "json",
        "--verbose",
        "--restricted",
        "--strict-mcp-config",
        f"--mcp-config={mcp_path}",
        "--tools=Read,Write",
        f"--allowedTools={allowed_tools}",
        f"--disallowedTools={DISALLOWED_TOOLS}",
    ]


def stamp(moment: datetime) -> str:
    """Formats a moment as wall-clock time with its zone: `07:27:00 +07 (2026-10-16)`."""
    return moment.strftime("%H:%M:%S %Z (%Y-%m-%d)")


def agent_limit(time_limit_minutes: int, stage_minutes: int, elapsed: float) -> float:
    """Returns the agent's seconds: its own limit capped by what the stage has left; never <= 0.

    `elapsed` counts from the end of the login renewal, so waiting for the login or the queue
    never eats it. Raises QaFailure when nothing is left (the stack boot used the stage).
    """
    limit = min(time_limit_minutes * 60, stage_minutes * 60 - elapsed)
    if limit <= 0:
        raise QaFailure(
            f"no time left for the qa agent: the stage allows {stage_minutes} min and the stack "
            f"boot took {int(elapsed // 60)} min"
        )
    return limit


def stop_at(deadline: datetime) -> datetime:
    """Returns when the agent should stop starting work: STOP_MARGIN_SECONDS before the deadline."""
    return deadline - timedelta(seconds=STOP_MARGIN_SECONDS)


def render_prompt(
    qa: Qa, values: dict[str, str], deadline: datetime, max_turns: int | None = None
) -> str:
    """Fills qa_prompt.md with the stack's URLs, the turn cap and the absolute deadline."""
    return fill(
        PROMPT_FILE.read_text(),
        {
            "app_url": fill(qa.app_url, values),
            "api_url": fill(qa.api_url, values),
            "max_turns": str(qa.max_turns if max_turns is None else max_turns),
            "deadline": stamp(deadline),
            "stop_at": stamp(stop_at(deadline)),
        },
    )


def clock_text(now: datetime, deadline: datetime) -> str:
    """Returns clock.txt: the current time, the deadline and what is left, for the agent's Read."""
    left = max(int((deadline - now).total_seconds()), 0)
    until_stop = max(int((stop_at(deadline) - now).total_seconds()), 0)
    verdict = (
        "KEEP WALKING the plan topics that remain."
        if until_stop > 0
        else "Time to finish: write out/findings.json now."
    )
    return "\n".join(
        [
            f"Now:       {stamp(now)}",
            f"Deadline:  {stamp(deadline)}  (the run is killed then)",
            f"Stop at:   {stamp(stop_at(deadline))}  (write out/findings.json by then)",
            f"Left:      {left // 60}m{left % 60:02d}s until the deadline, "
            f"{until_stop // 60}m{until_stop % 60:02d}s until you stop",
            verdict,
            "",
        ]
    )


def keep_clock(path: Path, deadline: datetime) -> threading.Event:
    """Writes `path` now and every CLOCK_TICK_SECONDS from a daemon thread; set the event to stop.

    The agent has no shell: this file, read with its Read tool, is its only way to see the time.
    """
    done = threading.Event()

    def write() -> None:
        """Rewrites the file atomically with the current time."""
        atomic_write(path, clock_text(datetime.now().astimezone(), deadline))

    write()

    def tick() -> None:
        """Rewrites the clock until the event is set."""
        while not done.wait(CLOCK_TICK_SECONDS):
            write()

    threading.Thread(target=tick, name="qa-clock", daemon=True).start()
    return done


def stopped_early(elapsed: float, budget: float, ended_by_itself: bool) -> bool:
    """Tells whether the agent ended on its own with more than 25% of its time unused."""
    return ended_by_itself and elapsed < EARLY_FRACTION * budget


def plan_text(box: Sandbox, plan: str) -> str:
    """Returns the plan file's text, from the sandbox when it lies inside the repository."""
    if not plan:
        return "No plan file was given."
    path = Path(plan)
    if path.is_relative_to(box.repo):
        inside = box.src / path.relative_to(box.repo)
        if inside.is_file():
            path = inside
    if not path.is_file():
        return f"The plan file {plan} could not be read."
    text = path.read_text(errors="replace")
    if len(text) > PLAN_LIMIT:
        return text[:PLAN_LIMIT] + f"\n\n[plan cut at {PLAN_LIMIT} characters]"
    return text


def _diff(box: Sandbox) -> tuple[str, list[str]]:
    """Returns `git diff --stat` and the changed files between the merge-base and the sha."""
    base = git.out(box.repo, "merge-base", box.sha, git.base_ref(box.repo))
    stat = git.out(box.repo, "diff", "--stat", f"{base}..{box.sha}").splitlines()
    if len(stat) > STAT_LINES:
        stat = [*stat[:STAT_LINES], f"[{len(stat) - STAT_LINES} more lines]"]
    return "\n".join(stat), git.changed_files(box.repo, box.sha)


def previous_before(cycle_id: str, run_id: str) -> list[Merged]:
    """Returns the findings of the cycle's finished attempts before `run_id`, merged by id."""
    cycle = cycles.load(cycle_id)
    earlier = [a for a in cycle_page.load_attempts(cycle) if a.run_id < run_id]
    return cycle_page.merge_findings(earlier)


def previous_state(request: Request) -> list[Merged]:
    """Returns the findings of the cycle's finished attempts before this run, merged by id."""
    return previous_before(request.cycle, request.id)


def next_ids(previous: list[Merged]) -> tuple[str, str]:
    """Returns the first free F-nnn and I-nnn ids above every id of the earlier attempts."""

    def free(prefix: str) -> str:
        """Returns the next id of one prefix."""
        numbers = [
            int(match.group(1))
            for item in previous
            if (match := re.fullmatch(rf"{prefix}-(\d+)", item.finding.id))
        ]
        return f"{prefix}-{max(numbers, default=0) + 1:03d}"

    return free("F"), free("I")


def _norm(text: str) -> str:
    """Collapses whitespace and case so a journey copied with small drift still matches."""
    return " ".join(text.split()).casefold()


def _code(text: str) -> str:
    """Wraps `text` in a markdown code span that survives backticks inside it."""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _listed(names: list[str]) -> str:
    """Joins up to six names with `; ` and says how many more there are."""
    shown = "; ".join(names[:SHOWN_ITEMS])
    extra = len(names) - SHOWN_ITEMS
    return shown + (f" (+{extra} more)" if extra > 0 else "")


@dataclass(frozen=True)
class Assignment:
    """What one QA agent of a run walks: some plan topics, or the retests of earlier findings."""

    topics: tuple[str, ...]
    retests: tuple[str, ...]


def assignments(headings: tuple[str, ...], retests: list[str]) -> list[Assignment]:
    """Returns one agent per plan topic, plus one for the retests when there are any.

    Empty when the plan has no topics: the run then briefs one agent with the whole charter, as
    before. Short agents cost a fraction of one long one: the price of a turn is the whole
    context read again, and the context of one topic stays small.
    """
    if not headings:
        return []
    found = [Assignment((heading,), ()) for heading in headings]
    if retests:
        found.append(Assignment((), tuple(retests)))
    return found


def split_budget(minutes: int, turns: int, agents: int) -> tuple[int, int]:
    """Returns the minutes and turns of each of `agents` agents sharing the run's budget.

    Each agent gets at least MIN_AGENT_MINUTES and MIN_AGENT_TURNS, so a plan with many topics
    may take longer than the run's time limit: the stage's own timeout still bounds it.
    """
    if agents <= 1:
        return minutes, turns
    return (
        max(MIN_AGENT_MINUTES, math.ceil(minutes / agents)),
        max(MIN_AGENT_TURNS, math.ceil(turns / agents)),
    )


def assignment_note(task: Assignment, next_f: str, next_i: str, minutes: int, turns: int) -> str:
    """Returns the text appended to the prompt of one agent of a split run."""
    lines = [
        "",
        "",
        "YOUR ASSIGNMENT. This run is split among several testers; you are one of them, and the",
        f"budget in the charter is the whole run's. Yours is {turns} turns and {minutes} minutes.",
    ]
    if task.topics:
        lines += [
            "Walk ONLY these plan topics, each as its own `plan:` flow named exactly as its heading:",
            *[f"- `{PLAN_PREFIX}{heading}`" for heading in task.topics],
            "Do not walk, record or mention any other plan topic: another tester has it.",
        ]
    else:
        lines.append("Walk no plan topic: another tester has them all.")
    if task.retests:
        lines += [
            "Re-check ONLY these earlier findings, each as a `walked` entry whose `flow` is the",
            "label below:",
            *[f"- `{label}`" for label in task.retests],
        ]
    else:
        lines += [
            "Re-check no earlier finding: another tester has them. Every earlier finding the",
            "charter lists is already known: never file it again, not under a new id either,",
            "even when you run into it while walking your topics.",
        ]
    lines += [
        f"New ids start at {next_f} for findings and {next_i} for improvements.",
        "`out/findings.json` holds only your own work; it is merged with the other testers' later.",
    ]
    return "\n".join(lines)


def ids_used(doc: dict[str, Any]) -> tuple[int, int]:
    """Returns the highest F-nnn and I-nnn numbers an agent's findings document uses, 0 when none."""

    def highest(key: str, prefix: str) -> int:
        items = doc.get(key) if isinstance(doc.get(key), list) else []
        numbers = [
            int(match.group(1))
            for item in items
            if isinstance(item, dict)
            and (match := re.fullmatch(rf"{prefix}-(\d+)", str(item.get("id", ""))))
        ]
        return max(numbers, default=0)

    return highest("findings", "F"), highest("improvements", "I")


def merge_outputs(files: list[Path]) -> tuple[dict[str, Any], list[str]]:
    """Joins the findings documents of a split run's agents into one, list by list.

    Returns the merged document and one problem per agent whose file is missing or not an
    object; the merged lists keep the order of the agents.
    """
    merged: dict[str, Any] = {"walked": [], "findings": [], "improvements": []}
    problems = []
    for index, file in enumerate(files, start=1):
        try:
            doc = json.loads(file.read_text())
        except (OSError, ValueError):
            problems.append(f"qa agent {index}/{len(files)} produced no findings file")
            continue
        if not isinstance(doc, dict):
            problems.append(f"qa agent {index}/{len(files)} findings file is not a JSON object")
            continue
        for key in merged:
            value = doc.get(key)
            if isinstance(value, list):
                merged[key] += value
    return merged, problems


def combined_result(results: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Sums the claude results of a split run's agents into one result object.

    Cost, turns and usage add up; the subtype is the first one that is not `success`, so the
    caps are checked over the whole run; None when no agent left a result.
    """
    found = [r for r in results if r is not None]
    if not found:
        return None
    usage: dict[str, int] = {}
    for r in found:
        for key, value in (r.get("usage") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    off = [r for r in found if r.get("subtype") not in (None, "success")]
    first = off[0] if off else found[0]
    return {
        "type": "result",
        "subtype": first.get("subtype", "success"),
        "is_error": any(bool(r.get("is_error")) for r in found),
        "result": str(first.get("result", "")),
        "total_cost_usd": sum(float(r.get("total_cost_usd") or 0) for r in found),
        "num_turns": sum(int(r.get("num_turns") or 0) for r in found),
        "usage": usage,
        "agents": len(found),
    }


def build_charter(box: Sandbox, request: Request, cfg: Config, previous: list[Merged]) -> Charter:
    """Builds charter.md from the feature, plan, diff, journeys, earlier findings and accounts.

    Every journey, and every earlier finding that still blocks, is listed with the exact text the
    agent must use as `flow` in `walked`: the stage fails when one is missing there. Earlier open
    P2 and P3 findings are listed apart, as known: never re-checked nor filed again.
    """
    qa = cfg.qa_scaled(request.qa_scale)
    headings = tuple(
        " ".join(h.split())
        for h in re.findall(
            r"^### \d+\.[ \t]*(.+?)[ \t]*$", plan_text(box, request.plan), re.MULTILINE
        )
    )
    topic_count = len(headings)
    selection = stages.select_specs(
        cfg, request.repo, request.sha, lambda spec: (box.src / spec).is_file()
    )
    stat, changed = _diff(box)
    critical = set(cfg.playwright.critical if cfg.playwright else ())
    journeys = [
        Path(spec).name.removesuffix(".ts").removesuffix(".spec")
        for spec in selection.specs
        if spec in critical
    ]
    notes = "No notes file is configured."
    if qa.notes_file:
        file = box.src / qa.notes_file
        notes = (
            file.read_text(errors="replace") if file.is_file() else f"{qa.notes_file} not found."
        )
    open_before = [m for m in previous if m.state in ("open", "acknowledged")]
    carried = [m for m in open_before if m.finding.severity in RECHECKED]
    minor = [m for m in open_before if m.finding.severity not in RECHECKED]
    must_retest = [m for m in carried if cycle_page.report.blocking(m.finding)]
    retest_label = {m.finding.id: f"retest {m.finding.id}" for m in carried}
    has_plan = bool(request.plan)
    kinds = [] if has_plan else [(name, "critical journey") for name in journeys]
    kinds += [] if has_plan else [(f, "surface-map flow the diff touches") for f in selection.flows]
    kinds += [
        (retest_label[m.finding.id], "earlier finding that still blocks") for m in must_retest
    ]
    required = tuple(dict.fromkeys(label for label, _ in kinds))
    lines = [
        f"# QA charter: {request.feature}",
        "",
        f"Run {request.id}, commit {request.sha}, branch {request.branch}, env {request.env}.",
        f"Retest of run {request.retest_of}." if request.retest_of else "",
        "",
        "## Where",
        "",
        f"- app: {fill(qa.app_url, box.values)}",
        f"- api: {fill(qa.api_url, box.values)}",
        *[
            f"- {surface.name}: {fill(surface.url, box.values)}"
            + (f" ({fill(surface.notes, box.values)})" if surface.notes else "")
            for surface in qa.surfaces
        ],
        "",
        "## Accounts",
        "",
        "| role | name | email | password |",
        "|---|---|---|---|",
        *[f"| {a.role} | {a.name} | {a.email} | {a.password} |" for a in qa.accounts],
        "",
        "## Widths and languages",
        "",
        f"Walk at {' and '.join(f'{w} px' for w in WIDTHS)} wide. One locale is enough; when the UI "
        f"offers a second ({', '.join(LOCALES)}) and there is time, record it too.",
        "",
        "## Journeys to walk",
        "",
        "Record each one in `walked` with `flow` set to exactly the text in its code span. A journey",
        "with no `walked` entry is recorded as a P2 finding.",
        "",
        *(
            [f"- {_code(label)} ({kind})" for label, kind in dict.fromkeys(kinds)]
            or (
                ["None: Playwright covers the generic journeys. Walk only the plan topics below."]
                if has_plan
                else ["None."]
            )
        ),
        "",
        "## Plan changes to walk",
        "",
        "The feature plan below lists what changed since the last deploy. In addition to the",
        "journeys above:",
        "",
        "1. Derive from the plan every **user-facing change**: a new screen, control, setting,",
        "   message type or flow. A change with no user-facing side (a refactor, a test, internal",
        "   tooling) is not walked. When the plan has a `Topics to walk` section, its numbered",
        "   topics are the changes: one flow each, never more than 12 flows in all, named exactly",
        "   as the topic's heading (listed below) in every attempt; add a flow beyond them only for",
        "   a user-facing change they clearly miss.",
        "2. Walk each change in the real app at 1280 px and at 390 px, in at least one of the",
        f"   locales {', '.join(LOCALES)}; a second locale is optional.",
        f"3. Record each change in `walked` as its own flow named `{PLAN_PREFIX}<short name>`",
        "   (two to five words), `result` `walked`, `blocked` (reason in `notes`, for example it",
        "   needs a real provider) or `not reachable` (reason in `notes`). A `walked` flow also",
        "   carries `summary` (one paragraph: what the change is, for a reader without the plan)",
        "   and `steps`.",
        "4. `steps` is what you did, one entry per step and width, each with a screenshot:",
        '   `{"caption": "one line on what the screenshot proves", "width": 1280, "locale": "en",',
        '   "screenshot": "evidence/plan-<slug>-<step>-1280.jpg"}`. Screenshot every step',
        "   of every plan flow at both 1280 and 390; both widths must appear in `steps`.",
        '5. A finding or improvement about a change carries `"flow": "plan:<same name>"`. For an',
        "   improvement, `expected` says what should change and `evidence[0]` is the screenshot",
        "   it is anchored to.",
        f"6. A plan with no user-facing change gets one flow named `{NO_CHANGE_FLOW}`, result",
        "   `not reachable`, and the reason in `notes`.",
        "",
        *(
            [
                "Name each topic's flow EXACTLY with its plan heading, character for character,",
                f"as `{PLAN_PREFIX}<heading>`, and give every finding about it the same `flow`.",
                "Never shorten, translate or reword a heading: a name that is not one of these is",
                "mapped to the closest heading or recorded as a defect. The headings:",
                "",
                *[f"- `{PLAN_PREFIX}{heading}`" for heading in headings],
                "",
            ]
            if headings
            else []
        ),
        "A plan with no `plan:` flow recorded is a P1 finding.",
        "",
        "## Budget",
        "",
        f"You have {qa.max_turns} turns, {qa.time_limit_minutes} minutes and a hard cap of",
        f"${qa.max_cost_usd:g}; the run stops when any of them is reached. Walk the plan topics,",
        "most user-visible first. After every topic, rewrite",
        "`out/findings.json` in full with what you have so far (`walked`, `findings`,",
        "`improvements`), so a run that is stopped keeps its walk. A topic you cannot get to",
        "is recorded as `not reachable` with `not walked: out of budget` in `notes`.",
        f"Your clock is `{CLOCK_FILE}` in your working directory: read it with the Read tool after",
        "every topic and at least every ten steps. It gives the current time, your deadline and",
        f"when to stop ({STOP_MARGIN_SECONDS // 60} minutes before the deadline). Keep walking",
        "topics until then and never stop while topics remain: a run that ends early with topics",
        "unwalked is recorded as",
        "a defect (`stopped early`), not as being out of budget.",
        *(
            [
                f"The plan lists {topic_count} topics (never walk more than {MAX_TOPICS}): "
                f"about {qa.time_limit_minutes / topic_count:.1f} minutes each. "
                "Blocked flows with a reason are fine (a blocked plan topic is kept as a P2 "
                "coverage note); a topic you did not reach is a P2 finding."
            ]
            if topic_count
            else []
        ),
        "",
        "## Feature plan",
        "",
        plan_text(box, request.plan),
        "",
        "## What changed",
        "",
        "```",
        stat,
        "```",
        "",
        "Changed files:",
        "",
        *[f"- {name}" for name in changed],
        "",
    ]
    if carried:
        lines += [
            "## Earlier findings to re-check",
            "",
            "Answer each with a `walked` entry whose `flow` is `retest <id>` and whose `result` is",
            "`fixed`, `still_present` or `cannot_reproduce`. `fixed` counts only with at least one",
            "screenshot of this run proving the steps now work, in `evidence`; when unsure use",
            "`cannot_reproduce`: the finding then stays open. Never list an earlier finding under",
            "`findings`. A finding of a backend log (`L-` id) is also checked against the stack log.",
            *RETEST_STEPS_RULE,
            "",
        ]
        for item in carried:
            f = item.finding
            need = "required" if item in must_retest else "optional"
            lines += [
                f"### {_code(retest_label[f.id])} [{f.severity}] {f.title} ({need})",
                f"Area: {f.area}. Expected: {f.expected} Actual then: {f.actual}",
                *[f"{n}. {step}" for n, step in enumerate(f.steps, start=1)],
                "",
            ]
    if minor:
        lines += [
            "## Earlier P2 and P3 findings: known, not re-checked",
            "",
            "The session fixes these without another run. Do not re-check them, do not answer",
            "them with a `retest` entry and never file them again, under their id or a new one,",
            "even when you run into one while walking:",
            "",
            *[f"- `{m.finding.id}` [{m.finding.severity}] {m.finding.title}" for m in minor],
            "",
        ]
    next_finding, next_improvement = next_ids(previous)
    lines += [
        f"Next free finding id: {next_finding}. Next free improvement id: {next_improvement}.",
        "",
        "## Project notes",
        "",
        notes,
        "",
    ]
    return Charter("\n".join(lines), required, has_plan, topic_count, headings)


def _group_alive(pgid: int) -> bool:
    """Tells whether any process of group `pgid` still exists."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_group(child: "subprocess.Popen[bytes]") -> None:
    """Stops a process group: SIGTERM, a grace period, then SIGKILL for whatever is left."""
    pgid = child.pid
    if _group_alive(pgid):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline and _group_alive(pgid):
        child.poll()
        time.sleep(0.2)
    if _group_alive(pgid):
        os.killpg(pgid, signal.SIGKILL)
    child.wait()


def run_agent(
    command: list[str], *, cwd: Path, env: dict[str, str], out: Path, err: Path, timeout: float
) -> AgentRun:
    """Runs the agent in its own process group under `timeout`; the group dies with the call."""
    with out.open("wb") as stdout, err.open("ab") as stderr:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            preexec_fn=proc.default_signals,
        )
        timed_out = False
        try:
            child.wait(timeout=max(timeout, 1))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _terminate_group(child)
    return AgentRun(child.returncode, timed_out)


USAGE_LIMIT = re.compile(
    r"hit your (?:[\w-]+ )?limit|usage limit|session limit|disabled Claude subscription access"
    r"|rate_limit.{0,200}429|429.{0,200}rate_limit",
    re.IGNORECASE | re.DOTALL,
)
MIN_RETRY_SECONDS = 300.0
RESUME_NOTE = (
    "\n\nA previous run of you was cut off by an account usage limit. out/findings.json and the "
    "files in evidence/ may hold its partial work: Read them first, keep what is true and carry on "
    "from where it stopped instead of starting over. The deadline above is unchanged."
)


def usage_limit(out: Path, err: Path, err_offset: int = 0) -> str | None:
    """Returns the line in which the CLI itself reported that the account cannot be used now.

    That is a usage limit (the line carries its reset time when the CLI gives one), or an
    organisation that disabled subscription access for Claude Code: either way another account
    can take over. Only text the CLI generated counts: the JSON result flagged `is_error`, or the
    new stderr when the CLI left no readable result. What the agent wrote (its result when not an
    error) never decides it. None when the CLI reported no such thing.
    """
    result = read_result(out)
    if result is not None:
        said = str(result.get("result", "")) if result.get("is_error") else ""
    else:
        raw = err.read_bytes()[err_offset:] if err.exists() else b""
        said = raw.decode("utf-8", errors="replace")
    found = USAGE_LIMIT.search(said)
    if found is None:
        return None
    end = said.find("\n", found.end())
    return said[said.rfind("\n", 0, found.start()) + 1 : end if end >= 0 else None].strip()


def blocked(said: str) -> proc.AccountFailure:
    """Returns the failure that ends a run whose agent the CLI refused with `said`."""
    return proc.AccountFailure(f"the Claude account cannot run the agent now: {said}")


def retry_on_usage_limit(
    start: Callable[[float, bool], AgentRun],
    *,
    home: Path,
    budget: float,
    out: Path,
    err: Path,
    renew: Callable[[float], None],
    switch_wait: float = jail.SWITCH_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentRun:
    """Runs `start(seconds, resumed)`; on a usage-limit ending runs it once more.

    The rotator is told the account is exhausted (jail.signal_limit) and the live login is awaited
    until its token differs from the one the first run used. The second run gets only what is left
    of `budget`, with a login renewed by `renew` and copied afresh. When the account did not
    change, too little time is left, the login cannot cover it or the second run is refused too,
    raises AccountFailure with the CLI's words (see `blocked`); the first run's output stays.
    """
    began = clock()
    offset = err.stat().st_size if err.exists() else 0
    run = start(budget, False)
    said = usage_limit(out, err, offset)
    if said is None:
        return run
    used = jail.home_token(home)
    jail.signal_limit(jail.DEFAULT_LIMIT_TEXT)
    if not jail.wait_for_new_login(
        used, min(switch_wait, budget - (clock() - began)), clock=clock, sleep=sleep
    ):
        raise blocked(said)
    remaining = budget - (clock() - began)
    if remaining < MIN_RETRY_SECONDS:
        raise blocked(said)
    valid_for = remaining + jail.LOGIN_MARGIN_SECONDS
    try:
        renew(valid_for)
        jail.prepare_home(home, valid_for)
    except jail.JailError as exc:
        raise blocked(said) from exc
    out.replace(out.with_name(out.name + ".limited"))
    offset = err.stat().st_size if err.exists() else 0
    run = start(remaining, True)
    said = usage_limit(out, err, offset)
    if said is not None:
        raise blocked(said)
    return run


def read_result(path: Path) -> dict[str, Any] | None:
    """Returns the result object of claude's JSON output, None when it is missing or unreadable."""
    if not path.exists() or not path.read_text().strip():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    if isinstance(data, list):
        results = [i for i in data if isinstance(i, dict) and i.get("type") == "result"]
        data = results[-1] if results else None
    return data if isinstance(data, dict) else None


def _field(raw: dict[str, Any], name: str, kind: type, where: str) -> Any:
    """Returns raw[name] when it is a `kind` (a string non-empty), else raises QaFailure."""
    value = raw.get(name)
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise QaFailure(f"findings.json {where}: {name!r} must be a non-empty {kind.__name__}")
    return value


def _strings(raw: dict[str, Any], name: str, where: str) -> list[str]:
    """Returns raw[name] as a list of strings or raises QaFailure."""
    value = raw.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise QaFailure(f"findings.json {where}: {name!r} must be a list of strings")
    return value


def _resolve_evidence(name: str, evidence: Path) -> Path | None:
    """Returns the real file `name` points to inside `evidence`, None when there is none."""
    relative = name.removeprefix("./").removeprefix("evidence/")
    root = Path(os.path.realpath(evidence))
    candidate = Path(os.path.realpath(evidence / relative))
    return candidate if candidate.is_file() and candidate.is_relative_to(root) else None


def _evidence_path(name: str, evidence: Path) -> str:
    """Makes an evidence name absolute inside `evidence` when the file is there; else unchanged."""
    found = _resolve_evidence(name, evidence)
    return str(found) if found else name


def _is_image(path: Path) -> bool:
    """Tells whether the file starts like a JPEG, PNG or WebP image."""
    with path.open("rb") as handle:
        head = handle.read(12)
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def screenshots(evidence: Path) -> list[Path]:
    """Returns the real image files under `evidence`: regular, not links, with an image header."""
    found = []
    for path in sorted(evidence.rglob("*")):
        plain = path.is_file() and not path.is_symlink()
        if plain and path.suffix.lower() in SCREENSHOT_SUFFIXES and _is_image(path):
            found.append(path)
    return found


def _new_id(
    raw: dict[str, Any],
    where: str,
    prefix: str,
    used: set[str],
    renumbered: list[tuple[str, str]],
) -> str:
    """Validates a new finding's id and marks it used; a taken id gets the next free one.

    The swap is appended to `renumbered` as (old, new). Reserved prefixes and bad formats raise.
    """
    ident = _field(raw, "id", str, where)
    if ident.startswith(RESERVED_PREFIXES):
        raise QaFailure(
            f"findings.json {where}: id {ident!r} starts with L- or W-, which the runner "
            "reserves for its own findings"
        )
    if not NEW_ID[prefix].match(ident):
        raise QaFailure(f"findings.json {where}: id {ident!r} must look like {prefix}-001")
    if ident in used:
        taken = [int(i.split("-")[1]) for i in used if i.startswith(f"{prefix}-")]
        fresh = f"{prefix}-{max(taken) + 1:03d}"
        renumbered.append((ident, fresh))
        ident = fresh
    used.add(ident)
    return ident


def _finding(
    raw: Any,
    where: str,
    evidence: Path,
    floor: int,
    prefix: str,
    used: set[str],
    renumbered: list[tuple[str, str]],
) -> Finding:
    """Validates one finding or improvement; `floor` is the lowest severity index allowed.

    The id is taken last, so an entry that fails validation never uses an id.
    """
    if not isinstance(raw, dict):
        raise QaFailure(f"findings.json {where} must be an object")
    severity = _field(raw, "severity", str, where)
    if severity not in SEVERITIES or SEVERITIES.index(severity) < floor:
        allowed = ", ".join(SEVERITIES[floor:])
        raise QaFailure(f"findings.json {where}: severity {severity!r} must be one of {allowed}")
    for name in ("expected", "actual"):
        if not isinstance(raw.get(name), str):
            raise QaFailure(f"findings.json {where}: {name!r} must be a string")
    title = _field(raw, "title", str, where)
    area = _field(raw, "area", str, where)
    steps = _strings(raw, "steps", where)
    shots = _strings(raw, "evidence", where)
    return Finding(
        id=_new_id(raw, where, prefix, used, renumbered),
        severity=severity,
        title=title,
        area=area,
        steps=steps,
        expected=raw["expected"],
        actual=raw["actual"],
        evidence=[_evidence_path(name, evidence) for name in shots],
        flow=" ".join(raw["flow"].split()) if isinstance(raw.get("flow"), str) else "",
    )


def _steps(raw: dict[str, Any], where: str, evidence: Path) -> list[Step]:
    """Validates a flow's `steps`: caption, width and screenshot each, absolute screenshot path."""
    found = raw.get("steps", [])
    if not isinstance(found, list):
        raise QaFailure(f"findings.json {where}: 'steps' must be a list")
    steps = []
    for index, item in enumerate(found):
        here = f"{where}.steps[{index}]"
        if not isinstance(item, dict):
            raise QaFailure(f"findings.json {here} must be an object")
        width = item.get("width")
        if isinstance(width, bool) or not isinstance(width, int):
            raise QaFailure(f"findings.json {here}: 'width' must be an integer")
        locale = item.get("locale", "")
        if not isinstance(locale, str):
            raise QaFailure(f"findings.json {here}: 'locale' must be a string")
        steps.append(
            Step(
                caption=" ".join(_field(item, "caption", str, here).split()),
                width=width,
                screenshot=_evidence_path(_field(item, "screenshot", str, here), evidence),
                locale=locale,
            )
        )
    return steps


def _walked(raw: Any, where: str, evidence: Path, carried: set[str]) -> Walked:
    """Validates one `walked` entry: a flow, or `retest <id>` of an earlier open finding."""
    if not isinstance(raw, dict):
        raise QaFailure(f"findings.json {where} must be an object")
    flow = " ".join(_field(raw, "flow", str, where).split())
    widths = raw.get("widths")
    if not isinstance(widths, list) or not all(
        isinstance(w, int) and not isinstance(w, bool) for w in widths
    ):
        raise QaFailure(f"findings.json {where}: 'widths' must be a list of integers")
    retest = RETEST.match(flow)
    if retest and retest.group(1) not in carried:
        raise QaFailure(f"findings.json {where}: {flow!r} names no earlier open finding")
    allowed = RETEST_RESULTS if retest else PLAN_RESULTS if is_plan(flow) else WALKED_RESULTS
    result = raw.get("result")
    if result not in allowed:
        raise QaFailure(f"findings.json {where}: result of {flow!r} must be one of {allowed}")
    return Walked(
        flow=flow,
        widths=widths,
        locales=_strings(raw, "locales", where),
        result=result,
        notes=raw["notes"] if isinstance(raw.get("notes"), str) else "",
        evidence=_strings(raw, "evidence", where) if "evidence" in raw else [],
        summary=" ".join(raw["summary"].split()) if isinstance(raw.get("summary"), str) else "",
        steps=_steps(raw, where, evidence),
    )


def parse_agent_findings(
    path: Path, evidence: Path, carried: set[str], taken: set[str], lenient: bool = False
) -> AgentFindings:
    """Reads and validates the agent's findings.json; raises QaFailure when absent or wrong.

    `carried` are the earlier open findings the agent answers with `retest <id>` entries and
    `taken` every id the cycle already used; a new finding or improvement whose id is taken, or
    repeated in the file, gets the next free id of its prefix and the swap is in `renumbered`.

    The errors of every invalid entry are raised together; a `retest <id>` already answered by an
    earlier entry is one. With `lenient` the invalid entries are dropped instead, their errors in
    `dropped`. A file that is missing, not JSON, not an object or without its lists always raises.
    """
    if not path.is_file():
        raise QaFailure("qa agent produced no findings file")
    try:
        raw = json.loads(path.read_text())
    except ValueError as exc:
        raise QaFailure(f"qa agent findings file is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise QaFailure("qa agent findings file must be a JSON object")
    entries = _field(raw, "walked", list, "<root>")
    reported = _field(raw, "findings", list, "<root>")
    suggestions = raw.get("improvements", [])
    if not isinstance(suggestions, list):
        raise QaFailure("findings.json: 'improvements' must be a list")
    errors: list[str] = []
    walked: list[Walked] = []
    for index, item in enumerate(entries):
        try:
            entry = _walked(item, f"walked[{index}]", evidence, carried)
        except QaFailure as exc:
            errors.append(str(exc))
            continue
        if RETEST.match(entry.flow) and any(w.flow == entry.flow for w in walked):
            errors.append(f"findings.json walked[{index}]: {entry.flow!r} repeats an earlier entry")
            continue
        walked.append(entry)
    used = set(taken)
    renumbered: list[tuple[str, str]] = []
    items: list[Finding] = []
    sections = (
        ("findings", reported, 0, "F"),
        ("improvements", suggestions, SEVERITIES.index("P2"), "I"),
    )
    for name, raws, floor, prefix in sections:
        for index, item in enumerate(raws):
            where = f"{name}[{index}]"
            try:
                items.append(_finding(item, where, evidence, floor, prefix, used, renumbered))
            except QaFailure as exc:
                errors.append(str(exc))
    if errors and not lenient:
        raise QaFailure("; ".join(errors))
    return AgentFindings(walked, items, tuple(renumbered), tuple(errors))


def dropped_problem(errors: tuple[str, ...]) -> str:
    """Returns the stage problem of a findings.json whose invalid entries were dropped."""
    noun = "entry" if len(errors) == 1 else "entries"
    first = errors[0].removeprefix("findings.json ")
    return f"findings.json: {len(errors)} invalid {noun} dropped: {first}"


REPAIR_MAX_TURNS = 6
REPAIR_MAX_COST_USD = 1.0
REPAIR_SECONDS = 300.0
REPAIR_ATTEMPTS = 3


def repair_prompt(error: str) -> str:
    """Returns the one-turn prompt that asks the agent to fix findings.json and nothing else."""
    return "\n".join(
        (
            "The runner rejected `out/findings.json`. Nothing has been scored yet.",
            f"Validation error: {error}",
            "Allowed values: severity "
            + ", ".join(SEVERITIES)
            + "; a plan flow's result "
            + ", ".join(PLAN_RESULTS)
            + "; any other flow's result "
            + ", ".join(WALKED_RESULTS)
            + "; a `retest <id>` entry's result "
            + ", ".join(RETEST_RESULTS)
            + ".",
            "Read and rewrite `out/findings.json` only, in full, with every error listed "
            "fixed, and any other entry with the same slip. Fix the format only: never change a "
            "verdict's meaning, a note, a step or a finding. For a `retest <id>` flow choose fixed, still_present or cannot_reproduce from "
            "that entry's own notes. Change nothing else: no other entry, note, step or file.",
        )
    )


def run_repair(
    prompt: str,
    *,
    wrap: Callable[[list[str]], list[str]],
    claude: Path,
    qa_dir: Path,
    out_dir: Path,
    evidence: Path,
    home: Path,
    tools: Any,
    logs: Path,
    max_cost_usd: float,
) -> AgentRun:
    """Runs one fresh short agent in the QA jail that rewrites out/findings.json.

    It uses the QA agent's mechanics (Read and Write only, no MCP server) but no resumed session.
    Its stdout and stderr both end up in logs/qa-repair-output.json.
    """
    mcp = qa_dir / "mcp-repair.json"
    atomic_write(mcp, '{"mcpServers": {}}\n')
    output = logs / "qa-repair-output.json"
    errors = logs / "qa-repair-stderr.txt"
    errors.unlink(missing_ok=True)

    def renew(seconds: float) -> None:
        """Renews the live login outside the jail until it covers `seconds`."""
        jail.ensure_login(claude, base_env(), seconds)

    valid_for = REPAIR_SECONDS + jail.LOGIN_MARGIN_SECONDS
    renew(valid_for)
    jail.prepare_home(home, valid_for)
    command = wrap(
        jail.command(
            claude_command(
                claude,
                prompt,
                mcp,
                REPAIR_MAX_TURNS,
                min(REPAIR_MAX_COST_USD, max_cost_usd),
                "Read,Write",
            ),
            workdir=qa_dir,
            writable=[out_dir, evidence],
            workdir_writable=False,
            home=home,
            tools=tools,
            claude=claude,
            npm_cache=paths.NPM,
        )
    )
    run = retry_on_usage_limit(
        lambda seconds, _resumed: run_agent(
            command,
            cwd=qa_dir,
            env=base_env(),
            out=output,
            err=errors,
            timeout=seconds,
        ),
        home=home,
        budget=REPAIR_SECONDS,
        out=output,
        err=errors,
        renew=renew,
    )
    stderr = errors.read_text(encoding="utf-8", errors="replace") if errors.exists() else ""
    errors.unlink(missing_ok=True)
    with output.open("a") as f:
        f.write(f"\n--- exit {run.code} timed_out {run.timed_out}\n")
        if stderr:
            f.write(f"--- stderr\n{stderr}")
    return run


def parse_or_repair(
    parse: Callable[[bool], AgentFindings], ask: Callable[[str], None]
) -> AgentFindings:
    """Parses the findings; on a validation error gives the agent up to REPAIR_ATTEMPTS repair turns.

    `parse(lenient)` reads the file; `ask` runs a repair turn with the prompt it is given, each
    with the error of the latest parse. A missing file is not repaired. A file still invalid after
    the last attempt is parsed leniently: its valid entries are kept and the invalid ones named in
    `dropped`; one that is not JSON, or not an object with its lists, raises its QaFailure.
    """
    for _ in range(REPAIR_ATTEMPTS):
        try:
            return parse(False)
        except QaFailure as exc:
            if "no findings file" in str(exc):
                raise
            ask(repair_prompt(str(exc)))
    return parse(True)


def _heading_of(flow: str, headings: tuple[str, ...]) -> str | None:
    """Returns the plan heading a `plan:` flow name stands for, None when it names none."""
    name = flow[len(PLAN_PREFIX) :]
    exact = [h for h in headings if _norm(h) == _norm(name)]
    return exact[0] if exact else closest(name, list(headings))


def canonical_flows(
    parsed: AgentFindings, headings: tuple[str, ...]
) -> tuple[AgentFindings, list[str]]:
    """Renames plan flows (and the findings that cite them) to the plan heading they walk.

    Returns the renamed result and the names that match no heading, which the caller flags.
    """
    if not headings:
        return parsed, []
    rename: dict[str, str] = {}
    unknown: list[str] = []
    for w in parsed.walked:
        if not is_plan(w.flow) or w.flow.casefold() == NO_CHANGE_FLOW or w.flow in rename:
            continue
        heading = _heading_of(w.flow, headings)
        if heading is None:
            unknown.append(w.flow)
        else:
            rename[w.flow] = f"{PLAN_PREFIX}{heading}"

    def renamed(flow: str) -> str:
        return rename.get(flow, flow)

    walked = [replace(w, flow=renamed(w.flow)) for w in parsed.walked]
    findings = [replace(f, flow=renamed(f.flow)) for f in parsed.findings]
    return replace(parsed, walked=walked, findings=findings), unknown


def _no_change(parsed: AgentFindings) -> bool:
    """Tells whether the walk's only plan flow is NO_CHANGE_FLOW: nothing of the plan to walk."""
    plan = [w.flow.casefold() for w in parsed.walked if is_plan(w.flow)]
    return bool(plan) and set(plan) == {NO_CHANGE_FLOW}


def coverage_problems(
    parsed: AgentFindings, evidence: Path, charter: Charter | None = None
) -> list[str]:
    """Names what makes the walk worthless: no flow walked at all, or no screenshot of it.

    Every flow (retests of earlier findings aside) blocked or not reachable, and a plan the walk
    never touched, also fail the stage: nothing of the change was tested, so it cannot be READY.
    A walk whose only plan flow is NO_CHANGE_FLOW is complete without a screenshot or a walked
    flow: the charter prescribes it for a plan with no user-facing change.

    Anything less (a plan topic blocked or not reachable with its reason, a journey or topic left
    out, a missing locale or width) is a gap, recorded as a finding by `coverage_gaps`, never a
    reason to fail the stage.
    """
    problems = []
    no_change = _no_change(parsed)
    if not parsed.walked:
        problems.append("qa agent walked no flow")
    if not no_change and not screenshots(evidence):
        problems.append("qa agent saved no screenshot in the evidence directory")
    flows = [w for w in parsed.walked if not RETEST.match(w.flow)]
    blocked = [
        w
        for w in flows
        if w.result in BLOCKED_RESULTS
        and not (w.result == "not reachable" and "not walked" in w.notes.casefold())
    ]
    if flows and len(blocked) == len(flows) and not no_change:
        problems.append(f"qa agent could not test anything: {_why_blocked(blocked)}")
    if (
        charter is not None
        and charter.plan_required
        and not any(is_plan(w.flow) for w in parsed.walked)
    ):
        problems.append("qa agent walked no plan topic (no flow named plan:<name>)")
    return problems


def _why_blocked(flows: list[Walked]) -> str:
    """Returns the blocked flows' names with the first one's reason."""
    names = ", ".join(w.flow for w in flows)
    return f"{names}: {flows[0].notes.strip() or flows[0].result}"


def _gap(severity: str, flow: str, why: str, actual: str = "") -> Finding:
    """Returns the runner's finding for work the agent did not do: `not walked: <why>`."""
    ident = "G-" + hashlib.sha1(f"gap|{_norm(flow)}|{_norm(why)}".encode()).hexdigest()[:8]
    return Finding(
        id=ident,
        severity=severity,
        title=why if why.startswith("not walked") else f"not walked: {why}",
        area=flow or "qa coverage",
        steps=[f"Walk {flow!r} as the charter describes." if flow else "Walk the charter."],
        expected="The change is walked at 1280 and 390 px with a screenshot per step.",
        actual=actual or f"The QA pass did not complete it: {why}.",
        flow=flow,
    )


def coverage_gaps(
    parsed: AgentFindings, charter: Charter, evidence: Path, early: str = ""
) -> list[Finding]:
    """Returns a finding for each journey, plan topic or step the agent left undone.

    A plan flow that is incomplete, blocked or not reachable (the agent's reason in `actual`), or
    `not reachable` with `not walked` in its notes, is a P2 on that topic; a charter journey with
    no entry is a P2; a plan with no plan flow at all is a P1, since nothing of the change was
    looked at. NO_CHANGE_FLOW, and a blocked flow outside the plan with a reason, are no gap.
    """
    gaps: list[Finding] = []
    done = {_norm(w.flow) for w in parsed.walked}
    for label in charter.required:
        if _norm(label) not in done:
            gaps.append(_gap("P2", label, "charter journey left out"))
    for w in parsed.walked:
        if w.result in BLOCKED_RESULTS and not w.notes.strip() and not is_plan(w.flow):
            gaps.append(_gap("P2", w.flow, "blocked without the reason in notes"))
    flows = [w for w in parsed.walked if is_plan(w.flow)]
    for w in flows:
        reason = w.notes.strip()
        if w.result == "not reachable" and "not walked" in w.notes.casefold():
            why = f"stopped early ({early})" if early else reason
            gaps.append(_gap("P2", w.flow, why, reason))
        elif w.result in BLOCKED_RESULTS and reason and w.flow.casefold() != NO_CHANGE_FLOW:
            gaps.append(_gap("P2", w.flow, "blocked or not reachable", f"{w.result}: {reason}"))
        elif why := _plan_flow_problem(w, evidence):
            gaps.append(_gap("P2", w.flow, why))
    if charter.plan_required and not flows:
        gaps.append(_gap("P1", "", "no plan change was walked (no flow named plan:<name>)"))
    elif charter.topics and len(flows) < charter.topics:
        left = charter.topics - len(flows)
        why = f"{left} of {charter.topics} plan topics have no flow"
        gaps.append(_gap("P2", "", f"stopped early ({early}): {why}" if early else why))
    return gaps


def _plan_flow_problem(walked: Walked, evidence: Path) -> str:
    """Returns what a plan flow lacks, empty when it is complete for its result."""
    if walked.result in BLOCKED_RESULTS:
        return "" if walked.notes.strip() else f"{walked.result} without the reason in notes"
    if walked.result != "walked":
        return ""
    if not walked.summary:
        return "no summary of the change"
    if len({loc for loc in walked.locales if loc.strip()}) < MIN_PLAN_LOCALES:
        return "no locale recorded"
    widths = {step.width for step in walked.steps}
    if not set(WIDTHS) <= widths:
        return f"no step at {' and '.join(str(w) for w in WIDTHS if w not in widths)} px"
    for step in walked.steps:
        found = _resolve_evidence(step.screenshot, evidence)
        if found is None or not _is_image(found):
            return f"step {step.caption!r} has no screenshot file"
    return ""


def _names(finding: Finding, flow: str) -> bool:
    """Tells whether a finding's area or title names `flow` as a whole word or phrase."""
    pattern = rf"(?<!\w){re.escape(_norm(flow))}(?!\w)"
    return re.search(pattern, _norm(f"{finding.area} {finding.title}")) is not None


def flow_findings(parsed: AgentFindings, known: list[Finding], evidence: Path) -> list[Finding]:
    """Makes a P0 finding for each flow reported `failed` that no P0 or P1 finding names."""
    named = [f for f in [*parsed.findings, *known] if f.severity in ("P0", "P1")]
    made: dict[str, Finding] = {}
    for walked in parsed.walked:
        covered = any(_names(f, walked.flow) or f.flow == walked.flow for f in named)
        if walked.result != "failed" or covered:
            continue
        ident = "W-" + hashlib.sha1(f"flow|{_norm(walked.flow)}".encode()).hexdigest()[:8]
        proofs = [_resolve_evidence(name, evidence) for name in walked.evidence]
        made.setdefault(
            ident,
            Finding(
                id=ident,
                severity="P0",
                title=f"flow failed: {walked.flow}",
                area=walked.flow,
                steps=[f"Walk the flow {walked.flow!r} as the charter describes."],
                expected="The flow completes.",
                actual=walked.notes
                or "The QA agent reported the flow as failed and filed no P0 or P1 for it.",
                evidence=[str(p) for p in proofs if p is not None],
                flow=walked.flow,
            ),
        )
    return list(made.values())


def _block_end(lines: list[str], start: int) -> int:
    """Returns the index after the traceback that starts at `start`, chained ones included."""
    end = start + 1
    while True:
        while end < len(lines) and lines[end].startswith((" ", "\t")):
            end += 1
        if end < len(lines):
            end += 1
        probe = end
        while probe < len(lines) and not lines[probe].strip():
            probe += 1
        if probe < len(lines) and CHAIN.match(lines[probe]):
            probe += 1
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if probe < len(lines) and lines[probe].startswith(TRACEBACK):
                end = probe + 1
                continue
        return end


def _normal(text: str) -> str:
    """Strips numbers and identifiers so two occurrences of one problem share a signature."""
    return NUMBER.sub("#", IDENT.sub("#", TMP_PATH.sub("<tmp>", text))).strip()


@dataclass(frozen=True)
class LogHit:
    """One distinct problem found in the stack log."""

    kind: str
    signature: str
    title: str
    first_line: str
    excerpt: list[str]


def scan_log(text: str) -> list[LogHit]:
    """Finds distinct tracebacks, ERROR lines and HTTP 5xx access lines in a stack log window."""
    lines = stages.ANSI.sub("", text).splitlines()
    hits: dict[str, LogHit] = {}
    covered: set[int] = set()
    index = 0
    while index < len(lines):
        if lines[index].startswith(TRACEBACK):
            end = _block_end(lines, index)
            first = max(index - 1, 0)
            covered.update(range(first, end))
            frames = [ln for ln in lines[index:end] if 'File "' in ln]
            final = lines[end - 1].strip()
            signature = f"{_normal(final)}|{_normal(frames[-1].strip()) if frames else ''}"
            hits.setdefault(
                f"traceback|{signature}",
                LogHit(
                    "traceback",
                    signature,
                    f"Backend traceback: {final[:140]}",
                    final,
                    lines[first:end][:EXCERPT_LINES],
                ),
            )
            index = end
        else:
            index += 1
    for index, line in enumerate(lines):
        if index in covered:
            continue
        access = ACCESS_5XX.search(line)
        if access:
            method, target, status = access.groups()
            route = _normal(target.split("?", 1)[0])
            hits.setdefault(
                f"access|{method}|{route}|{status}",
                LogHit(
                    "access",
                    f"{method} {route} {status}",
                    f"HTTP {status} from {method} {target.split('?', 1)[0]}",
                    line.strip(),
                    lines[max(index - 5, 0) : index + 1],
                ),
            )
        elif ERROR_LINE.search(line):
            hits.setdefault(
                f"error|{_normal(line)}",
                LogHit(
                    "error",
                    _normal(line),
                    f"Backend ERROR: {line.strip()[:140]}",
                    line.strip(),
                    lines[index : index + 6],
                ),
            )
    return list(hits.values())


def log_id(hit: LogHit) -> str:
    """Returns the finding id of a log problem: L- and a hash of its signature, stable per run."""
    return "L-" + hashlib.sha1(f"{hit.kind}|{hit.signature}".encode()).hexdigest()[:8]


def stack_log_ids(log: Path) -> set[str] | None:
    """Returns the ids of the log problems a stack log reproduces; None when there is no log."""
    if not log.exists():
        return None
    return {log_id(hit) for hit in scan_log(log.read_text(errors="replace"))}


def log_findings(
    hits: list[LogHit], evidence: Path, secrets: tuple[str, ...] = ()
) -> list[Finding]:
    """Turns log hits into P0 findings with their excerpt saved as evidence/backend-<n>.txt."""
    found = []
    for number, hit in enumerate(hits[:MAX_LOG_FINDINGS], start=1):
        target = evidence / f"backend-{number}.txt"
        atomic_write(target, redact.redact("\n".join(hit.excerpt), secrets) + "\n")
        found.append(
            Finding(
                id=log_id(hit),
                severity="P0",
                title=hit.title,
                area="backend log",
                steps=["Use the app while the QA agent walks it; the stack log records this."],
                expected="No server error is logged while the app is used.",
                actual=redact.redact(hit.first_line, secrets),
                evidence=[str(target)],
            )
        )
    return found


def _carry(
    item: Merged, answer: Walked | None, tree: str, log_ids: set[str], evidence: Path
) -> tuple[Finding, str]:
    """Returns an earlier open finding as this run records it, and a note when a claim failed.

    It becomes `fixed` only when the agent reported it `fixed` with a screenshot of this run (in `evidence` or a step), on a
    tree other than the one it was last seen on, and, for a log finding, when its signature is
    gone from this run's log window. Anything else keeps it open.
    """
    finding = item.finding
    names = [*answer.evidence, *(step.screenshot for step in answer.steps)] if answer else []
    proofs = [_resolve_evidence(name, evidence) for name in dict.fromkeys(names)]
    shown = [p for p in proofs if p is not None]
    kept = replace(finding, status="open", evidence=[str(p) for p in shown])
    if answer is None or answer.result != "fixed":
        return kept, ""
    if item.tree == tree:
        why = "it was last seen on this same tree"
    elif not any(_is_image(p) for p in shown):
        why = "no screenshot in evidence"
    elif finding.id in log_ids:
        why = "its log signature is still in the stack log"
    else:
        return replace(kept, status="fixed"), ""
    return kept, f"{finding.id} reported fixed but kept open: {why}"


def merge_final(
    *,
    agent: list[Finding],
    runner: list[Finding],
    previous: list[Merged],
    answers: dict[str, Walked],
    tree: str,
    log_ids: set[str],
    evidence: Path,
    gaps_judged: bool = False,
    log_scanned: bool = False,
) -> tuple[list[Finding], list[str]]:
    """Returns the run's findings and the notes on fixed claims that were not accepted.

    New findings of the agent and the runner's own come first; a runner finding the operator
    dismissed on this same tree keeps its dismissal. Every earlier open or acknowledged finding is
    carried forward, so no run can drop one; it is `fixed` only as `_carry` says.
    """
    by_id = {m.finding.id: m for m in previous}
    final = list(agent)
    for finding in runner:
        before = by_id.get(finding.id)
        if before and before.state == "dismissed" and before.tree == tree:
            finding = replace(finding, dismissed=replace(before.finding.dismissed))
        final.append(finding)
    reported = {f.id for f in runner}
    notes = []
    for item in previous:
        if item.state not in ("open", "acknowledged") or item.finding.id in reported:
            continue
        if gaps_judged and item.finding.id.startswith("G-"):
            final.append(replace(item.finding, status="fixed"))
            continue
        if (
            log_scanned
            and item.finding.id.startswith("L-")
            and item.finding.id not in log_ids
            and item.tree != tree
        ):
            final.append(replace(item.finding, status="fixed"))
            continue
        record, note = _carry(item, answers.get(item.finding.id), tree, log_ids, evidence)
        final.append(record)
        if note:
            notes.append(note)
    return final, notes


def _qa_source(run_id: str) -> str:
    """Returns the run that executed the QA stage `run_id` holds, following reuse to its origin."""
    source = run_id
    while (origin := queue.load(source).stages["qa"].reused_from) is not None:
        source = origin
    return source


def remerge(run_id: str, request: Request) -> tuple[list[Finding], list[str]]:
    """Returns the run's findings merged again from its QA output, and the notes on fixed claims.

    A run that reused a QA stage is merged as the run that executed it was.

    The agent's new findings and the runner's own are the ones the run stored; the earlier
    findings are decided from the retest answers in qa/out/findings.json by `merge_final`. The
    file is read leniently, as the run read it last: an invalid retest answer counts as none.
    """
    run_dir = paths.run_dir(run_id)
    evidence = run_dir / "evidence"
    origin = _qa_source(run_id)
    source = paths.run_dir(origin)
    previous = previous_before(request.cycle, origin)
    carried = {m.finding.id for m in previous if m.state in ("open", "acknowledged")}
    parsed = parse_agent_findings(
        source / "qa" / "out" / "findings.json",
        evidence,
        carried,
        {m.finding.id for m in previous},
        lenient=True,
    )
    answers = {RETEST.match(w.flow).group(1): w for w in parsed.walked if RETEST.match(w.flow)}
    stored = load_findings(run_id)
    known = {m.finding.id for m in previous}
    log = stack_log_ids(source / "logs" / "qa-stack.log")
    own = [
        f
        for f in stored
        if f.id.startswith(RESERVED_PREFIXES)
        and (f.id not in known or (log is not None and f.id in log))
    ]
    final, notes = merge_final(
        agent=[f for f in stored if f.id not in known and not f.id.startswith(RESERVED_PREFIXES)],
        runner=own,
        previous=previous,
        answers=answers,
        tree=request.tree,
        log_ids=log or set(),
        evidence=evidence,
        log_scanned=log is not None,
    )
    hidden = redact.run_secrets(parse(request.config, f"request of run {run_id}"))
    return [redact.finding(item, hidden, False) for item in final], notes


def _write_flows(
    run_id: str,
    walked: list[Walked],
    elapsed: float | None = None,
    budget: float = 0.0,
    early: bool = False,
) -> None:
    """Writes qa.json: the flows the cycle page lists under Pruebas and the agent's timing.

    `timing` holds the seconds the agent ran, the seconds it was given and whether it stopped
    early (ended on its own with over 25% of its time unused).
    """
    flows = [
        {
            "name": w.flow,
            "widths": w.widths,
            "locales": w.locales,
            "result": f"{w.result}: {w.notes}" if w.notes else w.result,
            "summary": w.summary,
            "steps": [asdict(step) for step in w.steps],
        }
        for w in walked
    ]
    timing = {
        "elapsed_seconds": None if elapsed is None else round(elapsed),
        "budget_seconds": round(budget),
        "stopped_early": early,
    }
    body = {"flows": flows, "timing": timing}
    atomic_write(paths.qa_file(run_id), json.dumps(body, indent=2) + "\n")


def _last_line(path: Path) -> str:
    """Returns the last non-empty line of a log file, empty when there is none."""
    if not path.exists():
        return ""
    lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def cost_of(result: dict[str, Any] | None) -> float | None:
    """Returns total_cost_usd of claude's result when it is a number."""
    cost = result.get("total_cost_usd") if result else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def turns_of(result: dict[str, Any] | None) -> int | None:
    """Returns num_turns of claude's result when it is an integer."""
    turns = result.get("num_turns") if result else None
    return turns if isinstance(turns, int) and not isinstance(turns, bool) else None


def agent_problems(
    agent: AgentRun,
    result: dict[str, Any] | None,
    *,
    max_turns: int | None,
    max_cost_usd: float | None,
    limit: float,
    err: Path,
    who: str = "qa agent",
    budget: list[str] | None = None,
) -> list[str]:
    """Names how the agent process ended badly; a pass needs its caps verified, never assumed. A cap
    given as None was not set and is not checked.

    With `budget` given, running out of time, turns or money goes there instead of into the
    returned problems: the agent was stopped, not broken, and what it walked is kept.
    """
    spent = budget if budget is not None else []
    if agent.timed_out:
        spent.append(f"{who} timed out after {int(limit // 60)} min")
        return [] if budget is not None else spent
    if result is None:
        hint = _last_line(err)
        detail = f" ({hint})" if hint else ""
        return [
            f"{who} left no readable JSON result (exit {agent.code}): "
            f"cost and turn caps are unchecked{detail}"
        ]
    subtype = str(result.get("subtype", ""))
    cost = result.get("total_cost_usd")
    turns = result.get("num_turns")
    problems = []
    if subtype == "error_max_turns":
        spent.append(f"{who} hit the turn cap ({max_turns})")
    elif agent.code != 0 or result.get("is_error"):
        said = str(result.get("result", "")).strip().splitlines()
        problems.append(
            f"{who} failed (exit {agent.code}, {subtype})" + (f": {said[0]}" if said else "")
        )
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        problems.append("claude reported no total_cost_usd: the cost cap is unchecked")
    elif max_cost_usd is not None and (cost > max_cost_usd or subtype == "error_max_budget_usd"):
        spent.append(f"cost cap exceeded (${cost:.2f} > ${max_cost_usd:g})")
    if isinstance(turns, bool) or not isinstance(turns, int):
        problems.append("claude reported no num_turns: the turn cap is unchecked")
    elif max_turns is not None and turns > max_turns:
        spent.append(f"turn cap exceeded ({turns} > {max_turns})")
    return [*problems, *spent] if budget is None else problems


BOOT_LOCK_KEY = "qa boot lock"


def _locked(handle: TextIO) -> bool:
    """Takes the exclusive lock of an open file without waiting; tells whether it got it."""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def boot_turn(
    run_id: str,
    seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    pause: Callable[[float], object] = admission.STOP.wait,
) -> Iterator[None]:
    """Holds the machine-wide QA boot lock: no two QA stacks boot (seed, build) at the same time.

    It waits at most `seconds` for another run's stack to finish booting, then raises QaFailure;
    a cancelled run or a stopping executor raises Cancelled, as for the Playwright turn. Only a
    wait touches the run's state, where `qg status` shows it.
    """
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    began = clock()
    held_up = False
    with paths.QA_BOOT_LOCK.open("a") as handle:
        while not _locked(handle):
            waited = clock() - began
            if admission.STOP.is_set() or queue.load(run_id).cancel_requested:
                queue.set_waiting(run_id, BOOT_LOCK_KEY, None)
                raise proc.Cancelled
            if waited >= seconds:
                queue.set_waiting(run_id, BOOT_LOCK_KEY, None)
                raise QaFailure(
                    f"waited {int(waited // 60)} min for another QA stack to finish booting"
                )
            reason = "waiting for another run's QA stack to finish booting"
            queue.set_waiting(run_id, BOOT_LOCK_KEY, reason)
            held_up = True
            pause(min(admission.POLL_SECONDS, seconds - waited))
        if held_up:
            queue.set_waiting(run_id, BOOT_LOCK_KEY, None)
        yield


def run_qa(box: Sandbox, cfg: Config, request: Request) -> QaOutcome:
    """Boots the stack, runs the confined QA agent, applies the log oracles, writes the findings.

    Raises StackFailure when the stack did not boot or died or the agent left no result, and
    AccountFailure when the Claude account cannot run the agent: neither is a verdict on the code.
    """
    assert cfg.qa is not None
    qa = cfg.qa_scaled(request.qa_scale)
    started = time.monotonic()
    qa_dir = paths.qa_dir(box.run_id)
    evidence = paths.evidence_dir(box.run_id)
    home = paths.qa_home(box.run_id)
    logs = paths.logs_dir(box.run_id)
    qa_dir.mkdir(exist_ok=True)
    out_dir = qa_dir / "out"
    out_dir.mkdir(exist_ok=True)
    evidence.mkdir(exist_ok=True)
    previous = previous_state(request)
    carried = {m.finding.id for m in previous if m.state in ("open", "acknowledged")}
    hidden = redact.run_secrets(cfg)
    problems: list[str] = []
    budget: list[str] = []
    agent_seconds: float | None = None
    early = False
    limit = 0.0
    hits: list[LogHit] = []
    log_scanned = False
    result: dict[str, Any] | None = None
    charter: Charter | None = None
    agent_ran = False
    turn_cap = qa.max_turns
    parsed: AgentFindings | None = None
    parse_error = ""
    try:
        tools = settings.load()
        claude = jail.claude_binary()
        needed = BOOT_SECONDS + qa.time_limit_minutes * 60 + jail.LOGIN_MARGIN_SECONDS
        jail.ensure_login(claude, base_env(), needed)
        started = time.monotonic()
        try:
            secrets = read_secrets(qa.secrets_env_file, qa.secrets_keys)
            charter = build_charter(box, request, cfg, previous)
            atomic_write(qa_dir / "charter.md", charter.text)
            origins = allowed_origins(qa, box.values)
            mcp = mcp_config(evidence, tools.npx, origins)
            atomic_write(qa_dir / "mcp.json", json.dumps(mcp, indent=2) + "\n")
            stack_log = logs / "qa-stack.log"
            try:
                sandbox.start_postgres(box, cfg, logs / "setup-qa.log", cfg.timeouts["setup"] * 60)
            except SystemExit as exc:
                raise QaFailure(str(exc)) from exc
            endpoint: twins.Endpoint | None = None
            running: stack.Stack | None = None
            stage_seconds = cfg.stage_minutes("qa", request.qa_scale) * 60
            try:
                boot_wait = stage_seconds - BOOT_SECONDS - (time.monotonic() - started)
                with boot_turn(box.run_id, boot_wait):
                    running = stack.start(
                        fill(qa.boot, box.values),
                        cwd=box.src,
                        env={**box.env, **secrets},
                        log=stack_log,
                        wrap=lambda argv: box.wrap("qa_stack", argv),
                    )
                    ready = stack.wait_ready(
                        running, fill(qa.ready_url, box.values), BOOT_SECONDS, qa.ready_marker
                    )
                if not ready:
                    raise proc.StackFailure(
                        f"stack not ready after {BOOT_SECONDS // 60} min or it exited "
                        f"({running.died() or 'still booting'}); see {stack_log}"
                    )
                window_start = stack_log.stat().st_size
                twin = twins.discover(base_env().get("TMPDIR"), box.values["backend_port"])
                allowed_tools = ALLOWED_TOOLS
                if twin is not None:
                    endpoint = twins.Endpoint(twin, box.src, box.env, hidden)
                    endpoint.start()
                    mcp["mcpServers"][twins.SERVER] = twins.mcp_entry(endpoint)
                    mcp["mcpServers"]["playwright"]["args"][-1] = ";".join(
                        (*origins, *twin.browser_origins)
                    )
                    atomic_write(qa_dir / "mcp.json", json.dumps(mcp, indent=2) + "\n")
                    atomic_write(qa_dir / "charter.md", charter.text + twins.charter_section(twin))
                    allowed_tools = f"{ALLOWED_TOOLS},{twins.ALLOWED_TOOL}"
                allowed = allowed_tools
                plan = assignments(
                    charter.headings, [r for r in charter.required if RETEST.match(r)]
                )
                minutes, turns = split_budget(qa.time_limit_minutes, qa.max_turns, len(plan))
                turn_cap = turns * max(len(plan), 1)
                next_f, next_i = next_ids(previous)
                agents: list[AgentRun] = []
                results: list[dict[str, Any] | None] = []
                documents: list[Path] = []
                agent_seconds = 0.0
                limit = 0.0

                def renew(seconds: float) -> None:
                    """Renews the live login outside the jail until it covers `seconds`."""
                    jail.ensure_login(claude, base_env(), seconds)

                for index, task in enumerate(plan or [None]):
                    limit_k = agent_limit(
                        minutes,
                        cfg.stage_minutes("qa", request.qa_scale),
                        time.monotonic() - started,
                    )
                    deadline = datetime.now().astimezone() + timedelta(seconds=limit_k)
                    jail.prepare_home(home, limit_k + jail.LOGIN_MARGIN_SECONDS)
                    out_k = logs / (
                        f"qa-claude-output-{index + 1}.json" if plan else "qa-claude-output.json"
                    )
                    note = (
                        assignment_note(task, next_f, next_i, minutes, turns)
                        if task is not None
                        else ""
                    )

                    def build_command(
                        extra: str,
                        _note: str = note,
                        _when: datetime = deadline,
                        _turns: int = turns,
                    ) -> list[str]:
                        """Returns the jailed agent command, its prompt followed by `extra`."""
                        return box.wrap(
                            "qa_agent",
                            jail.command(
                                claude_command(
                                    claude,
                                    render_prompt(qa, box.values, _when, _turns) + extra + _note,
                                    qa_dir / "mcp.json",
                                    _turns,
                                    qa.max_cost_usd,
                                    allowed,
                                ),
                                workdir=qa_dir,
                                writable=[out_dir, evidence],
                                workdir_writable=False,
                                home=home,
                                tools=tools,
                                claude=claude,
                                npm_cache=paths.NPM,
                            ),
                        )

                    def run_once(
                        seconds: float, resumed: bool, _build=build_command, _out: Path = out_k
                    ) -> AgentRun:
                        """Runs the QA agent for `seconds`."""
                        return run_agent(
                            _build(RESUME_NOTE if resumed else ""),
                            cwd=qa_dir,
                            env=base_env(),
                            out=_out,
                            err=logs / "qa-agent.log",
                            timeout=seconds,
                        )

                    clock = keep_clock(qa_dir / CLOCK_FILE, deadline)
                    began = time.monotonic()
                    try:
                        agents.append(
                            retry_on_usage_limit(
                                run_once,
                                home=home,
                                budget=limit_k,
                                out=out_k,
                                err=logs / "qa-agent.log",
                                renew=renew,
                            )
                        )
                    finally:
                        clock.set()
                    agent_seconds += time.monotonic() - began
                    limit += limit_k
                    results.append(read_result(out_k))
                    if plan:
                        document = out_dir / f"findings-{index + 1}.json"
                        if (out_dir / "findings.json").is_file():
                            (out_dir / "findings.json").replace(document)
                            try:
                                used_f, used_i = ids_used(json.loads(document.read_text()))
                            except ValueError:
                                used_f, used_i = 0, 0
                            next_f = f"F-{max(int(next_f[2:]), used_f + 1):03d}"
                            next_i = f"I-{max(int(next_i[2:]), used_i + 1):03d}"
                        documents.append(document)
                if plan:
                    merged, missing = merge_outputs(documents)
                    if len(missing) < len(documents):
                        atomic_write(out_dir / "findings.json", json.dumps(merged, indent=2))
                    problems += missing
                    combined = combined_result(results)
                    atomic_write(
                        logs / "qa-claude-output.json",
                        json.dumps(combined) if combined is not None else "",
                    )
                agent = AgentRun(
                    max((a.code for a in agents), key=abs, default=0),
                    any(a.timed_out for a in agents),
                )
                agent_ran = True
                died = running.died()
                if died:
                    raise proc.StackFailure(f"the QA stack died while the agent ran: {died}")
                taken = {m.finding.id for m in previous}

                def ask(prompt: str) -> None:
                    """Gives the agent one repair turn; a broken jail keeps the findings."""
                    try:
                        repair(prompt)
                    except proc.Infrastructure:
                        raise
                    except jail.JailError as exc:
                        problems.append(str(exc))

                def repair(prompt: str) -> None:
                    """Runs the repair agent."""
                    run_repair(
                        prompt,
                        wrap=lambda argv: box.wrap("qa_agent", argv),
                        claude=claude,
                        qa_dir=qa_dir,
                        out_dir=out_dir,
                        evidence=evidence,
                        home=home,
                        tools=tools,
                        logs=logs,
                        max_cost_usd=qa.max_cost_usd,
                    )

                try:
                    parsed = parse_or_repair(
                        lambda lenient: parse_agent_findings(
                            out_dir / "findings.json", evidence, carried, taken, lenient
                        ),
                        ask,
                    )
                except QaFailure as exc:
                    parse_error = str(exc)
                window = stack_log.read_bytes()[window_start:][-LOG_WINDOW_BYTES:]
                hits = scan_log(window.decode("utf-8", errors="replace"))
                log_scanned = True
            finally:
                if running is not None:
                    running.stop()
                if endpoint is not None:
                    endpoint.stop()
        finally:
            jail.discard_home(home)
        result = read_result(logs / "qa-claude-output.json")
        if result is None and not agent.timed_out:
            hint = _last_line(logs / "qa-agent.log")
            raise proc.StackFailure(
                f"qa agent left no readable JSON result (exit {agent.code})"
                + (f" ({hint})" if hint else "")
            )
        problems += agent_problems(
            agent,
            result,
            max_turns=turn_cap,
            max_cost_usd=qa.max_cost_usd,
            limit=limit,
            err=logs / "qa-agent.log",
            budget=budget,
        )
    except proc.Infrastructure:
        raise
    except (QaFailure, jail.JailError, settings.SettingsError) as exc:
        problems.append(str(exc))
    if agent_ran and parse_error:
        if not problems or "no findings file" not in parse_error:
            problems.append(parse_error)
    if parsed is not None and parsed.dropped:
        problems.append(dropped_problem(parsed.dropped))
    gaps: list[Finding] = []
    if parsed is not None and charter is not None and not problems:
        parsed, unknown = canonical_flows(parsed, charter.headings)
        gaps += [_gap("P2", name, "flow name matches no plan heading") for name in unknown]
        problems += coverage_problems(parsed, evidence, charter)
        early = (
            stopped_early(agent_seconds, limit, not budget and not agent.timed_out)
            if agent_seconds is not None
            else False
        )
        left = int(limit - (agent_seconds or 0))
        said = (
            f"{int((agent_seconds or 0) // 60)} of {int(limit // 60)} min used, {left // 60} left"
        )
        gaps += coverage_gaps(parsed, charter, evidence, said if early else "")
    gaps += [_gap("P2", "", reason) for reason in budget]
    blocking_known = [m.finding for m in previous if m.finding.id in carried]
    runner = [*log_findings(hits, evidence, hidden), *gaps]
    if parsed is not None:
        runner += flow_findings(parsed, blocking_known, evidence)
    answers = {
        RETEST.match(w.flow).group(1): w
        for w in (parsed.walked if parsed else [])
        if RETEST.match(w.flow)
    }
    final, notes = merge_final(
        agent=parsed.findings if parsed else [],
        runner=runner,
        previous=previous,
        answers=answers,
        tree=request.tree,
        log_ids={log_id(hit) for hit in hits},
        evidence=evidence,
        log_scanned=log_scanned,
        gaps_judged=charter is not None and parsed is not None and not problems,
    )
    if parsed:
        notes += [f"renumbered new finding {old} to {new}" for old, new in parsed.renumbered]
    if len(hits) > MAX_LOG_FINDINGS:
        notes.append(f"{len(hits) - MAX_LOG_FINDINGS} more log problem(s) not recorded")
    final = [redact.finding(item, hidden, False) for item in final]
    if final:
        save_findings(box.run_id, final)
    if parsed:
        _write_flows(box.run_id, parsed.walked, agent_seconds, limit, early)
    cost = cost_of(result)
    turns = turns_of(result)
    p0 = sum(1 for f in final if f.status == "open" and f.severity == "P0" and not f.dismissed)
    stats = f"{len(parsed.walked) if parsed else 0} flows, {len(final)} findings ({p0} P0)"
    if turns is not None:
        stats += f", {turns} turns"
    if cost is not None:
        stats += f", ${cost:.2f}"
    return QaOutcome(
        "failed" if problems else "passed",
        "; ".join([*problems, *notes, stats]),
        cost,
        turns,
        [w.flow for w in parsed.walked] if parsed else [],
    )
