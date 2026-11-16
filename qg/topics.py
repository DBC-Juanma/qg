"""Topic pages: one page per user-facing change the manual QA pass walked, and nothing else."""

import json
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template

from qg import cycle_page, cycles, names, paths, queue, redact
from qg.cycle_page import (
    CSS,
    FONTS,
    SCRIPT,
    Attempt,
    Budget,
    Merged,
    QaFlow,
    esc,
    fmt_time,
)
from qg.cycles import ARTIFACT_URL, Cycle
from qg.qa import NO_CHANGE_FLOW, PLAN_PREFIX
from qg.report import SEVERITIES
from qg.util import atomic_write, die, now_iso

SLUG_LENGTH = 48
WORKS = "works"
IMPROVES = "works with improvements"
BROKEN = "broken"
BLOCKED = "blocked"
VERDICT_LABEL = {
    WORKS: "Works",
    IMPROVES: "Works with improvements",
    BROKEN: "Broken",
    BLOCKED: "Blocked",
}
VERDICT_TONE = {WORKS: "ok", IMPROVES: "warn", BROKEN: "bad", BLOCKED: "skip"}
BLOCKED_RESULTS = ("blocked", "not reachable")
STOP_REASON = {
    "setup": "the environment could not be built",
    "simplify": "the simplify pass could not complete",
    "static": "a static check did not pass",
    "backend": "the backend suite did not pass",
    "frontend": "the frontend suite did not pass",
    "playwright": "an automated browser test did not pass",
    "qa": "the manual pass could not complete",
}
TOPIC_CSS = """
.topic-steps{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,17rem),1fr));gap:.9rem}
.step{display:grid;gap:.4rem;align-content:start;min-width:0;padding:.55rem;background:var(--surface);
  border:1px solid var(--line)}
.step img{display:block;width:100%;height:auto;border:1px solid var(--line-2)}
.step .cap{font-size:.88rem;overflow-wrap:anywhere}
.step .tags{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center}
.step.phone{max-width:15rem}
.note{color:var(--ink-2);font-size:.88rem}
.change{display:grid;gap:.5rem;padding:.7rem .95rem;background:var(--surface);
  border:1px solid var(--line)}
"""
PAGE = Template(
    """<title>$title</title>
<link rel="stylesheet" href="$fonts">
<style>$css</style>
<main class="page">
$body
</main>
<script>$script</script>
"""
)


@dataclass
class Topic:
    """One topic of a cycle: its page's identity, verdict and the artifact URL once published."""

    key: str
    slug: str
    name: str
    verdict: str = ""
    artifact_url: str | None = None
    updated: str = ""
    aliases: list[str] = field(default_factory=list)

    def resembles(self, name: str) -> bool:
        """Tells whether `name` is this topic under a near-duplicate wording."""
        return names.similar(self.name, name) or names.similar(self.key, name)

    @property
    def identities(self) -> set[str]:
        """Returns every separator-free name this topic has been walked or planned under."""
        return {match_key(self.key), match_key(self.name), *self.aliases}


def key_of(flow: str) -> str:
    """Returns the normalised topic name of a plan flow, the same across attempts."""
    return " ".join(flow[len(PLAN_PREFIX) :].split()).casefold()


def name_of(flow: str) -> str:
    """Returns the human topic name of a plan flow: what follows `plan:`."""
    return " ".join(flow[len(PLAN_PREFIX) :].split())


def match_key(name: str) -> str:
    """Returns a name with case, accents, spaces, hyphens and every other separator removed."""
    plain = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", plain)


PLAN_HEADING = re.compile(r"^#{2,4}[ \t]+(?:\d+\.[ \t]*)?(.+?)[ \t]*$", re.MULTILINE)
MIN_CONTAINED = 8


def plan_headings(attempt: Attempt) -> list[str]:
    """Returns the headings of the run's plan.md, empty when the run has no plan on disk."""
    plan = Path(attempt.run_dir) / "plan.md"
    return PLAN_HEADING.findall(plan.read_text()) if plan.exists() else []


def canonical_name(flow_name: str, headings: list[str]) -> str:
    """Returns the plan heading a flow walks when exactly one matches, else the flow's own name."""
    name = name_of(flow_name)
    mk = match_key(name)
    same = [h for h in headings if match_key(h) == mk]
    inside = [h for h in headings if len(mk) >= MIN_CONTAINED and mk in match_key(h)]
    pick = same or inside
    return " ".join(pick[0].split()) if len(pick) == 1 else name


def _slug(name: str) -> str:
    """Turns a topic name into an ASCII, URL-safe slug."""
    plain = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:SLUG_LENGTH].strip("-") or "topic"


def load(cycle_id: str) -> dict[str, Topic]:
    """Returns the cycle's topics by key; empty when none was ever written."""
    path = paths.topics_file(cycle_id)
    if not path.exists():
        return {}
    items = json.loads(path.read_text())["topics"]
    return {key: Topic(**item) for key, item in items.items()}


def _save(cycle_id: str, found: dict[str, Topic], history: dict | None = None) -> None:
    """Writes topics.json: the topics and the findings' first-seen attempt and state history."""
    path = paths.topics_file(cycle_id)
    if history is None:
        history = json.loads(path.read_text()).get("findings", {}) if path.exists() else {}
    body = {"topics": {key: asdict(topic) for key, topic in found.items()}, "findings": history}
    paths.cycle_dir(cycle_id).mkdir(parents=True, exist_ok=True)
    atomic_write(paths.topics_file(cycle_id), json.dumps(body, indent=2) + "\n")


def list_topics(cycle_id: str) -> list[Topic]:
    """Returns the cycle's topics ordered by slug."""
    return sorted(load(cycle_id).values(), key=lambda topic: topic.slug)


def set_artifact(cycle_id: str, slug: str, url: str) -> Topic:
    """Stores a topic's artifact URL once; a different URL is refused, one topic keeps one URL."""
    if not ARTIFACT_URL.match(url):
        die(f"{url!r} is not a claude.ai artifact URL (https://claude.ai/artifact/...)")
    found = load(cycle_id)
    match = [topic for topic in found.values() if topic.slug == slug]
    if not match:
        names = ", ".join(sorted(topic.slug for topic in found.values())) or "none"
        die(f"cycle {cycle_id} has no topic {slug!r} (topics: {names})")
    topic = match[0]
    if topic.artifact_url and topic.artifact_url != url:
        die(
            f"topic {slug} already has the artifact {topic.artifact_url}; one topic keeps one URL "
            f"(edit {paths.topics_file(cycle_id)} by hand to change it)"
        )
    topic.artifact_url = url
    _save(cycle_id, found)
    return topic


def _split_result(flow: QaFlow) -> tuple[str, str]:
    """Splits qa.json's `<result>: <notes>` into the result word and the notes."""
    word, _, notes = flow.result.partition(": ")
    return word, notes


def verdict_of(flow: QaFlow, items: list[Merged]) -> tuple[str, str]:
    """Returns the topic verdict and, for a blocked one, why: derived from the walk and findings."""
    result, notes = _split_result(flow)
    if result in BLOCKED_RESULTS:
        return BLOCKED, notes or result
    serious = [i for i in items if i.finding.severity in ("P0", "P1")]
    if result == "failed" or serious:
        return BROKEN, ""
    return (IMPROVES if items else WORKS), ""


def _stopped(number: int, attempt: Attempt) -> str | None:
    """Returns the one line for an attempt that stopped before S5, None when it reached it."""
    qa = attempt.stages.get("qa")
    if qa is not None and qa.status in ("passed", "failed"):
        return None
    stage = attempt.failed_stage
    if stage is None:
        started = [name for name, s in attempt.stages.items() if s.started]
        stage = started[-1] if started else "setup"
    if attempt.status in ("passed", "failed") and attempt.failed_stage is None:
        return None
    reason = STOP_REASON.get(stage, "the run did not complete")
    return f"attempt {number} stopped at {stage}: {reason}."


def _evidence_image(item: Merged, path: str, cycle_id: str, budget: Budget) -> str:
    """Renders one finding screenshot as an embedded image, or its path when it does not fit."""
    name = item.thumbs.get(path)
    if not name or name == cycle_page.REJECTED or name.startswith(cycle_page.TEXT_PREFIX):
        return ""
    thumb = paths.thumbs_dir(cycle_id) / name
    uri = budget.take(thumb) if thumb.exists() else None
    if uri is None:
        return f'<p class="mono">{esc(path)}</p>'
    return (
        f'<figure class="shot" tabindex="0" role="button" aria-label="Enlarge screenshot">'
        f'<img src="{uri}" alt="{esc(Path(path).name)}"></figure>'
    )


def _finding(item: Merged, cycle_id: str, budget: Budget) -> str:
    """Renders one finding or improvement of the topic with its screenshot."""
    f = item.finding
    improvement = f.id.startswith("I-")
    shots = "".join(_evidence_image(item, path, cycle_id, budget) for path in f.evidence)
    body = []
    if improvement:
        body.append(f'<div><p class="label">What should change</p><p>{esc(f.expected)}</p></div>')
        if f.actual:
            body.append(f'<div><p class="label">How it is now</p><p>{esc(f.actual)}</p></div>')
    else:
        steps = "".join(f"<li>{esc(step)}</li>" for step in f.steps)
        body.append(f'<div><p class="label">What happens</p><p>{esc(f.actual)}</p></div>')
        body.append(f'<div><p class="label">What was expected</p><p>{esc(f.expected)}</p></div>')
        if steps:
            body.append(f'<div><p class="label">Steps</p><ol class="steps">{steps}</ol></div>')
    shots_block = f'<div class="shots">{shots}</div>' if shots else ""
    still = (
        f'<p class="note">{esc(cycle_page._status_text(item))}</p>'
        if item.state == "open" and item.last > item.first
        else ""
    )
    return (
        '<article class="finding"><div class="f-head">'
        f'<span class="chip sev-{esc(f.severity)}">{esc(f.severity)}</span>'
        f"<h3>{esc(f.title)}</h3></div>{still}"
        f'<div class="cols3">{"".join(body)}</div>{shots_block}</article>'
    )


def _step(flow: QaFlow, index: int, attempt: Attempt, cycle_id: str, budget: Budget) -> str:
    """Renders one step of the walk: its screenshot, caption, width and locale."""
    step = flow.steps[index]
    name = attempt.thumbs.get(step.screenshot)
    thumb = paths.thumbs_dir(cycle_id) / name if name and name != cycle_page.REJECTED else None
    uri = budget.take(thumb) if thumb is not None and thumb.exists() else None
    picture = (
        f'<img src="{uri}" alt="{esc(step.caption)}" loading="lazy">'
        if uri
        else f'<p class="mono">{esc(step.screenshot)}</p>'
    )
    locale = f' <span class="chip skip">{esc(step.locale)}</span>' if step.locale else ""
    phone = " phone" if step.width <= 600 else ""
    return (
        f'<figure class="step{phone}">{picture}<figcaption><div class="tags">'
        f'<span class="chip ok">{index + 1}</span><span class="chip skip">{step.width} px</span>'
        f'{locale}</div><p class="cap">{esc(step.caption)}</p></figcaption></figure>'
    )


def render(
    cycle: Cycle,
    topic: Topic,
    number: int,
    attempt: Attempt,
    flow: QaFlow,
    items: list[Merged],
    stops: list[str],
    improved: list[Merged],
    attempts: list[Attempt],
) -> str:
    """Builds one topic page: the change, the verdict, the walk in screenshots and the findings."""
    verdict, why = verdict_of(flow, items)
    budget = Budget(cycle_page.IMAGE_BUDGET)
    ordered = sorted(items, key=lambda i: (SEVERITIES.index(i.finding.severity), i.finding.id))
    reason = f'<span class="why">{esc(why)}</span>' if why else ""
    seal = (
        f'<div class="seal {VERDICT_TONE[verdict]}"><span class="k">Verdict</span>'
        f'<span class="v">{esc(VERDICT_LABEL[verdict])}</span>{reason}</div>'
    )
    summary = flow.summary or why or "The QA did not describe this change."
    walk = "".join(_step(flow, i, attempt, cycle.id, budget) for i in range(len(flow.steps)))
    walk_block = (
        f'<section><h2>Walk</h2><div class="topic-steps">{walk}</div></section>'
        if walk
        else '<section><h2>Walk</h2><p class="empty">No walk.</p></section>'
    )
    findings = "".join(_finding(item, cycle.id, budget) for item in ordered)
    findings_block = (
        f"<section><h2>Findings</h2>{findings}</section>"
        if findings
        else '<section><h2>Findings</h2><p class="empty">None.</p></section>'
    )
    improved_block = (
        f"<section><h2>Improved since attempt {min(m.first for m in improved)}</h2>"
        f"{cycle_page.improved_rows(improved, attempts)}</section>"
        if improved
        else ""
    )
    stopped = "".join(f'<p class="note">{esc(line)}</p>' for line in stops)
    body = "\n".join(
        [
            '<header class="head">'
            f'<p class="eyebrow">Manual test · {esc(cycle.feature)} · attempt {number}</p>'
            f'<h1>{esc(topic.name)}</h1><div class="seals">{seal}</div></header>',
            f'<section><h2>What changes</h2><div class="change"><p>{esc(summary)}</p></div>'
            "</section>",
            walk_block,
            findings_block,
            improved_block,
            stopped,
            f"<footer>Generated by qg on {esc(fmt_time(now_iso()))}</footer>",
        ]
    )
    return PAGE.substitute(
        title=esc(topic.name), fonts=esc(FONTS), css=CSS + TOPIC_CSS, body=body, script=SCRIPT
    )


def _same_topic(cycle_id: str, known: dict[str, Topic]) -> None:
    """Folds topics whose names differ by separators or near-duplicate wording, a published first.

    A folded topic that had its own artifact is kept as it is: one topic keeps one URL.
    """
    keepers: list[Topic] = []
    for key, topic in sorted(known.items(), key=lambda kv: (not kv[1].artifact_url, kv[1].slug)):
        keeper = next(
            (
                k
                for k in keepers
                if match_key(k.key) == match_key(topic.key) or k.resembles(topic.name)
            ),
            None,
        )
        if keeper is None:
            keepers.append(topic)
        elif not topic.artifact_url:
            keeper.aliases = sorted(keeper.identities | topic.identities)
            del known[key]
            shutil.rmtree(paths.topic_page(cycle_id, topic.slug).parent, ignore_errors=True)


def _identify(
    attempts: list[Attempt], known: dict[str, Topic]
) -> dict[str, tuple[int, Attempt, QaFlow]]:
    """Gives every plan flow of every walked attempt its topic, creating the missing ones.

    A flow joins the existing topic that shares its name or its plan heading once separators are
    removed. Returns, per topic key, the latest attempt in which S5 walked it, with that flow.
    """
    taken = {t.slug for t in known.values()}
    latest: dict[str, tuple[int, Attempt, QaFlow]] = {}
    for number, attempt in enumerate(attempts, start=1):
        qa = attempt.stages.get("qa")
        if qa is None or qa.status not in ("passed", "failed"):
            continue
        headings = plan_headings(attempt)
        for flow in attempt.qa_flows:
            if not flow.is_plan or flow.name.casefold() == NO_CHANGE_FLOW:
                continue
            name = canonical_name(flow.name, headings)
            ids = {match_key(name), match_key(name_of(flow.name))}
            topic = next((t for t in known.values() if t.identities & ids), None) or next(
                (t for t in known.values() if t.resembles(name)), None
            )
            if topic is None:
                slug, count = _slug(name), 1
                while slug in taken:
                    count += 1
                    slug = f"{_slug(name)}-{count}"
                taken.add(slug)
                topic = Topic(key=key_of(f"{PLAN_PREFIX}{name}"), slug=slug, name=name)
                known[topic.key] = topic
            topic.aliases = sorted(topic.identities | ids)
            topic.name = name
            latest[topic.key] = (number, attempt, flow)
    return latest


def _history(merged: list[Merged], known: dict[str, Topic]) -> dict:
    """Returns, per finding id, its first-seen attempt, topic and state transitions."""
    out = {}
    for m in merged:
        steps = []
        for h in m.history:
            if not steps or steps[-1]["state"] != h.state:
                steps.append({"attempt": h.attempt, "state": h.state, "sha": h.sha})
        topic = next((t.slug for t in known.values() if _belongs(m, t)), "")
        out[m.finding.id] = {"first": m.first, "topic": topic, "history": steps}
    return out


def _index(cycle: Cycle, found: dict[str, Topic]) -> str:
    """Builds the local index of the cycle's topic pages."""
    rows = "".join(
        f'<li><a href="{esc(paths.topic_page(cycle.id, t.slug).as_uri())}">{esc(t.name)}</a> '
        f'<span class="chip {VERDICT_TONE.get(t.verdict, "skip")}">'
        f"{esc(VERDICT_LABEL.get(t.verdict, t.verdict))}</span> "
        f'<span class="mono">{esc(t.artifact_url or "not published")}</span></li>'
        for t in sorted(found.values(), key=lambda t: t.slug)
    )
    body = f"<h1>{esc(cycle.feature)}</h1><ul>{rows or '<li>No topic walked.</li>'}</ul>"
    return PAGE.substitute(
        title=esc(cycle.feature), fonts=esc(FONTS), css=CSS, body=body, script=""
    )


def _belongs(item: Merged, topic: Topic) -> bool:
    """Tells whether any attempt's mention of the finding names this topic."""
    return any(
        m.flow.casefold().startswith(PLAN_PREFIX)
        and match_key(m.flow[len(PLAN_PREFIX) :]) in topic.identities
        for m in item.history
    )


def write_all(cycle_id: str) -> list[Topic]:
    """Writes one page per topic S5 has walked, plus the index; a cycle with none writes none."""
    cycle = cycles.load(cycle_id)
    attempts = cycle_page.load_attempts(cycle)
    cycle_page.SECRETS = redact.secrets_of(cycle_page.request_configs(cycle))
    merged = cycle_page.merge_findings(attempts)
    with queue.locked():
        known = load(cycle_id)
        _same_topic(cycle_id, known)
        walked = _identify(attempts, known)
        for key, (number, attempt, flow) in walked.items():
            topic = known[key]
            mine = [m for m in merged if _belongs(m, topic)]
            items = [m for m in mine if m.state in ("open", "acknowledged")]
            improved = [m for m in mine if m.state in ("fixed", "acknowledged")]
            stops = [
                line
                for n, a in enumerate(attempts, start=1)
                if n != number and (line := _stopped(n, a))
            ]
            reused = attempt.stages["qa"].reused_from
            if reused:
                stops.append(f"qa: passed (reused from {reused}); this walk was made in that run.")
            topic.verdict = verdict_of(flow, items)[0]
            topic.updated = now_iso()
            page = paths.topic_page(cycle_id, topic.slug)
            page.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                page, render(cycle, topic, number, attempt, flow, items, stops, improved, attempts)
            )
        _save(cycle_id, known, _history(merged, known))
        if known:
            paths.topics_dir(cycle_id).mkdir(parents=True, exist_ok=True)
            atomic_write(paths.topics_dir(cycle_id) / "index.html", _index(cycle, known))
    return sorted(known.values(), key=lambda t: t.slug)


def refresh(run_id: str) -> None:
    """Rebuilds the cycle page of a finished run and the topic pages of its cycle."""
    cycle_page.refresh(run_id)
    write_all(queue.load(run_id).cycle)
