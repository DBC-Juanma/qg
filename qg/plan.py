"""The generated plan: the commits since the base and the changed paths grouped by surface."""

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from qg import git, stages
from qg.config import Config

OTHER = "other"
PLAN_UNIT_CHARS = 3000
PLAN_FLOW = "plan:<short name>"
MAX_TOPICS = 12
OTHER_TOPIC = "other user-facing changes"
TOPIC_TITLE = re.compile(r"^### \d+\.[ \t]*(.+?)[ \t]*$", re.MULTILINE)
UNWALKABLE_MARKERS = re.compile(
    r"\b(?:manual|operator|operador)\b|real account|cuenta real", re.IGNORECASE
)
BOLD_SPAN = re.compile(r"\*\*([^*\n]+)\*\*")
SUBJECT_TYPE = re.compile(r"^\S+ (\w+)(?:\([^)]*\))?!?:")
QUIET_TYPES = frozenset({"test", "docs", "style", "chore", "ci", "build"})
QUIET_DIRS = frozenset({"tests", "__tests__", "e2e", "docs", "tasks", ".claude", ".github"})
QUIET_GLOBS = ("*.md", "*.test.*", "*.spec.*", "test_*", "*_test.*", "*.snap")


@dataclass
class Topic:
    """One user-facing topic of a plan: the surface it belongs to and the commits that touch it."""

    name: str
    also: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)


def commit_section(repo: str, base: str, sha: str) -> str:
    """Returns the markdown listing the commits `base..sha` adds, one `<sha> <subject>` each."""
    lines = git.commit_log(repo, base, sha)
    body = "\n".join(lines) if lines else "(no commits)"
    return f"## Commits since {base[:12]}\n\n```\n{body}\n```\n"


def surface_section(cfg: Config, repo: str, base: str, sha: str) -> str:
    """Returns the markdown listing the changed paths grouped by the surface map's entries."""
    changed = git.paths_between(repo, base, sha)
    groups: list[tuple[str, list[str], list[str]]] = []
    claimed: set[str] = set()
    for entry in stages.surfaces(cfg, repo, sha):
        hit = stages.touches(entry, changed)
        if hit:
            claimed.update(hit)
            title = entry.flows[0] if entry.flows else ", ".join(entry.globs)
            groups.append((title, list(entry.flows[1:]), hit))
    rest = [name for name in changed if name not in claimed]
    lines = [f"## Changed paths ({len(changed)})", ""]
    for title, flows, hit in groups:
        lines += [f"### {title}", ""]
        lines += [f"- also covers: {flow}" for flow in flows]
        lines += [f"- `{name}`" for name in hit]
        lines.append("")
    if rest:
        lines += [f"### {OTHER}", ""] + [f"- `{name}`" for name in rest] + [""]
    return "\n".join(lines)


def _quiet_file(name: str) -> bool:
    """Tells whether a path is a test, document, task note or tooling file no user sees."""
    path = PurePosixPath(name)
    return bool(QUIET_DIRS.intersection(path.parts[:-1])) or any(
        fnmatch.fnmatchcase(path.name, glob) for glob in QUIET_GLOBS
    )


def _user_facing(subject: str, files: list[str]) -> bool:
    """Tells whether a commit can change what a user sees; quiet types and files cannot."""
    kind = SUBJECT_TYPE.match(subject)
    if kind and kind.group(1).casefold() in QUIET_TYPES:
        return False
    if subject.split(" ", 1)[-1].startswith("Revert"):
        return False
    return any(not _quiet_file(name) for name in files)


def unwalkable_topics(text: str) -> list[str]:
    """Returns the `### N.` topics marked manual, operator or real-account, each with where.

    A marker counts in the title, or in the body as a bold span (the `**Data:** **manual,
    operator**` convention); plain body words never do.
    """
    heads = list(TOPIC_TITLE.finditer(text))
    found = []
    for index, head in enumerate(heads):
        title = " ".join(head.group(1).split())
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        in_title = UNWALKABLE_MARKERS.search(title)
        in_body = next(
            (
                m
                for span in BOLD_SPAN.findall(text[head.end() : end])
                if (m := UNWALKABLE_MARKERS.search(span))
            ),
            None,
        )
        if in_title:
            found.append(f'{title} (the word "{in_title.group(0)}" is in the title)')
        elif in_body:
            found.append(f'{title} (the word "{in_body.group(0)}" is in bold in its body)')
    return found


def topics(cfg: Config, repo: str, base: str, sha: str) -> tuple[list[Topic], int]:
    """Groups the user-facing commits of `base..sha` into at most MAX_TOPICS topics.

    A heuristic over what each commit touches, with no model call: a commit joins the surface-map
    entry that matches most of its files, one that matches none joins OTHER_TOPIC, and a commit of
    a quiet type (test, docs, style, chore, ci, build, a revert) or touching only tests, documents
    and tooling is left out. When more than MAX_TOPICS remain, the smallest are merged into
    OTHER_TOPIC. Returns the topics, biggest first, and how many commits were left out.
    """
    entries = stages.surfaces(cfg, repo, sha)
    named: dict[str, Topic] = {}
    other = Topic(OTHER_TOPIC)
    quiet = 0
    for subject, files in git.commit_files(repo, base, sha):
        if not _user_facing(subject, files):
            quiet += 1
            continue
        scores = [len(stages.touches(entry, files)) for entry in entries]
        best = max(scores, default=0)
        if best == 0:
            other.commits.append(subject)
            continue
        entry = entries[scores.index(best)]
        name = entry.flows[0] if entry.flows else ", ".join(entry.globs)
        topic = named.setdefault(name, Topic(name, list(entry.flows[1:])))
        topic.commits.append(subject)
    ranked = sorted(named.values(), key=lambda t: (-len(t.commits), t.name))
    for extra in ranked[MAX_TOPICS - 1 :]:
        other.commits += extra.commits
    ranked = ranked[: MAX_TOPICS - 1] if len(ranked) >= MAX_TOPICS else ranked
    return ([*ranked, other] if other.commits else ranked), quiet


def topic_section(cfg: Config, repo: str, base: str, sha: str) -> str:
    """Returns the markdown of the topics the QA pass walks, one `plan:` flow each."""
    found, quiet = topics(cfg, repo, base, sha)
    lines = [
        f"## Topics to walk ({len(found)})",
        "",
        "The commits below were grouped by the surface their files touch. Each topic is one",
        f"`{PLAN_FLOW}` flow; walk no more than {MAX_TOPICS}.",
        f"{quiet} further commit(s) touch only tests, documents or tooling and need no walk.",
        "",
    ]
    for number, topic in enumerate(found, start=1):
        lines += [f"### {number}. {topic.name}", ""]
        lines += [f"- also covers: {flow}" for flow in topic.also]
        lines += [f"- `{subject}`" for subject in topic.commits]
        lines.append("")
    return "\n".join(lines)


def generate(cfg: Config, repo: str, base: str, sha: str, explicit: str | None, source: str) -> str:
    """Builds plan.md: the commit log, then the explicit plan when one was given, else the paths.

    `source` says where the base came from, so the plan states what it was measured against.
    """
    title = [f"# Plan generated from {source}", ""]
    if explicit is not None:
        commits = commit_section(repo, base, sha)
        return "\n".join([*title, commits, "## Plan given with --plan", "", explicit.rstrip(), ""])
    topic_text = topic_section(cfg, repo, base, sha)
    return "\n".join(
        [*title, topic_text, commit_section(repo, base, sha), surface_section(cfg, repo, base, sha)]
    )


TOPIC_HEADING = re.compile(r"^### \d+\. ", re.MULTILINE)
MINUTES_PER_TOPIC = 7
MIN_QA_MINUTES = 14
MAX_QA_MINUTES = 70
PLAN_CAP_FACTOR = 1.9


def qa_minutes(text: str) -> int | None:
    """Returns the QA pass's minutes for a plan with generated topics: 7 per topic plus 7 for the
    retests, 14 to 70. One agent walks each topic and pays its own sign-in and navigation.

    None when the plan has no generated topics (an explicit --plan, or none).
    """
    count = len(TOPIC_HEADING.findall(text))
    if not count:
        return None
    return round(min(max(MINUTES_PER_TOPIC * (count + 1), MIN_QA_MINUTES), MAX_QA_MINUTES))


def scale(text: str, configured_minutes: int) -> float:
    """Returns how much longer than its configured limit the QA agent may run for this plan.

    A plan with generated topics gets `qa_minutes` (turns grow in the same proportion). A plan
    with none falls back to its length, at most PLAN_CAP_FACTOR.
    """
    minutes = qa_minutes(text)
    if minutes is not None:
        return minutes / configured_minutes
    return min(1.0 + 0.5 * (len(text) // PLAN_UNIT_CHARS), PLAN_CAP_FACTOR)
