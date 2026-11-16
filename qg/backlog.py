"""The simplify queue: what a simplify pass left alone and what an audit lens confirmed, kept per
repository until the operator accepts it (the next pass fixes it) or rejects it (no pass proposes
it again)."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from qg import paths
from qg.util import atomic_write, die, now_iso

PENDING, ACCEPTED, REJECTED, DONE = "pending", "accepted", "rejected", "done"
SIMPLIFY = "simplify"
DECISIONS = (ACCEPTED, REJECTED)
LEADING_PATH = re.compile(r"^`?([\w.-]+(?:/[\w.-]+)*\.\w+)")
CITED = re.compile(r"\[(S-[0-9a-f]{6})\]")


@dataclass
class Item:
    """One finding a simplify pass left alone or an audit lens confirmed, with the operator's
    decision on it. `key` is what makes two audit findings the same: lens, file, rule, symbol."""

    id: str
    file: str
    text: str
    run_id: str
    sha: str
    when: str
    status: str = PENDING
    why: str = ""
    done_in: str = ""
    lens: str = SIMPLIFY
    severity: str = ""
    evidence: str = ""
    fix: str = ""
    key: str = ""


def store(repo: str) -> Path:
    """Returns the queue file of the canonical repository path `repo`."""
    return paths.SIMPLIFY_QUEUE / f"{paths.repo_key(repo)}.json"


def load(repo: str) -> list[Item]:
    """Returns the queue of `repo`, oldest first; empty when it has none."""
    target = store(repo)
    if not target.is_file():
        return []
    return [Item(**item) for item in json.loads(target.read_text())]


def save(repo: str, items: list[Item]) -> None:
    """Writes the queue of `repo`."""
    paths.SIMPLIFY_QUEUE.mkdir(parents=True, exist_ok=True)
    body = json.dumps([asdict(item) for item in items], indent=2, ensure_ascii=False) + "\n"
    atomic_write(store(repo), body)


def file_of(text: str, src: Path) -> str:
    """Returns the file a finding starts with when it exists in `src`, else empty."""
    found = LEADING_PATH.match(text)
    return found.group(1) if found and (src / found.group(1)).is_file() else ""


def ingest(repo: str, run_id: str, sha: str, left_alone: list[str], src: Path) -> list[Item]:
    """Queues every finding of `left_alone` that names a file of `src` and is not queued yet;
    returns the new items. A note that names no file (how much was reviewed) is not a finding."""
    items = load(repo)
    known = {item.text for item in items}
    added = []
    for text in left_alone:
        name = file_of(text, src)
        if name and text not in known:
            added.append(Item(_item_id(repo, text), name, text, run_id, sha, now_iso()))
            known.add(text)
    if added:
        save(repo, items + added)
    return added


def _item_id(repo: str, identity: str) -> str:
    """Returns the queue id of a finding: S- and six hex digits of its repository and identity."""
    return "S-" + hashlib.sha1(f"{repo}\0{identity}".encode()).hexdigest()[:6]


def ingest_audit(repo: str, run_id: str, sha: str, found: list[Item]) -> list[Item]:
    """Queues every audit finding whose key no item holds, whatever that item's status, so a
    rejected finding is never proposed again; returns the new items with their ids and dates."""
    items = load(repo)
    known = {item.key for item in items if item.key}
    added = []
    for item in found:
        if item.key not in known:
            item.id, item.run_id, item.sha, item.when = (
                _item_id(repo, item.key),
                run_id,
                sha,
                now_iso(),
            )
            added.append(item)
            known.add(item.key)
    if added:
        save(repo, items + added)
    return added


def close(repo: str, run_id: str, changes: list[str]) -> list[str]:
    """Marks done every accepted item a published change cites by its [S-id]; returns their ids."""
    cited = {match for change in changes for match in CITED.findall(change)}
    items = load(repo)
    closed = [i.id for i in items if i.status == ACCEPTED and i.id in cited]
    if closed:
        for item in items:
            if item.id in closed:
                item.status, item.done_in = DONE, run_id
        save(repo, items)
    return closed


def decide(repo: str, item_id: str, status: str, why: str) -> Item:
    """Records the operator's decision on a pending or decided item; returns it."""
    items = load(repo)
    match = [item for item in items if item.id == item_id]
    if not match:
        die(f"the simplify queue of {repo} has no item {item_id!r}")
    if match[0].status == DONE:
        die(f"{item_id} was already fixed in run {match[0].done_in}")
    match[0].status, match[0].why = status, why
    save(repo, items)
    return match[0]


def prompt_block(items: list[Item]) -> str:
    """Returns what the simplify prompt says of the queue: what to fix, skip and not list again."""
    groups = {
        ACCEPTED: "Accepted by the operator: fix these first, even when the file is outside the "
        "diff, and start the change line of each with its id, e.g. `[S-1a2b3c] path: what changed`",
        PENDING: "Already queued for the operator: do not list them again in `left_alone`",
        REJECTED: "Rejected by the operator: do not make or propose these changes",
    }
    parts = []
    for status, lead in groups.items():
        listed = [f"- [{i.id}] {i.text}" for i in items if i.status == status]
        if listed:
            parts.append(f"{lead}:\n\n" + "\n".join(listed))
    return "\n\n".join(parts) if parts else "The queue is empty."
