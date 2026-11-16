"""The shadow window: a pure function over shadow.jsonl that says when the exit criterion holds."""

import json
from dataclasses import dataclass
from pathlib import Path

VERSION_KEYS = ("resoluto_engine", "resoluto_cli", "shim")


@dataclass(frozen=True)
class Criterion:
    """Sizes of the exit criterion: K check lines and R release lines in the window, and the M
    deploys that must follow the flip without a rollback."""

    k: int = 5
    r: int = 3
    m: int = 3


@dataclass(frozen=True)
class Item:
    """One item of the criterion; `met` is None when only the operator can attest it."""

    name: str
    met: bool | None
    detail: str


@dataclass(frozen=True)
class Window:
    """The trailing agreeing lines of a project, and the earlier lines that disagreed."""

    lines: list[dict]
    to_triage: list[dict]

    def of_kind(self, kind: str) -> list[dict]:
        """Returns the window's lines of `kind` (check or release)."""
        return [line for line in self.lines if line.get("kind", "check") == kind]


def load(path: Path) -> list[dict]:
    """Returns every line of shadow.jsonl in file order; an absent file has none."""
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text().splitlines() if row.strip()]


def agrees(line: dict) -> bool:
    """True when qg and resoluto said the same, and for a release line in every phase too."""
    if line["resoluto"] == "error" or line["qg"] != line["resoluto"]:
        return False
    return all(p["qg"] == p["resoluto"] for p in line.get("phases", {}).values())


def versions_of(line: dict) -> tuple:
    """Returns the code versions a line was measured with."""
    return tuple(line.get(key) for key in VERSION_KEYS)


def is_reset(line: dict, newest: dict) -> bool:
    """True when the line is a resoluto error or was measured with other code than the newest."""
    return line["resoluto"] == "error" or versions_of(line) != versions_of(newest)


def window(lines: list[dict]) -> Window:
    """Returns the window of one project's lines (file order, oldest first): the trailing run
    that ends at the newest line, holds no reset and agrees on every line."""
    if not lines:
        return Window([], [])
    newest = lines[-1]
    taken: list[dict] = []
    for line in reversed(lines):
        if is_reset(line, newest) or not agrees(line):
            break
        taken.append(line)
    taken.reverse()
    earlier = lines[: len(lines) - len(taken)]
    return Window(taken, [line for line in earlier if not agrees(line)])


def evaluate(found: Window, criterion: Criterion) -> list[Item]:
    """Returns the items of the exit criterion for `found`; items 4 and 5 are the operator's."""
    checks, releases = found.of_kind("check"), found.of_kind("release")

    def both(rows: list[dict]) -> bool:
        return any(r["qg"] == "ok" for r in rows) and any(r["qg"] == "not_verified" for r in rows)

    return [
        Item(
            "1 counts",
            len(checks) >= criterion.k and len(releases) >= criterion.r,
            f"{len(checks)}/{criterion.k} check lines, {len(releases)}/{criterion.r} release lines",
        ),
        Item("2 check polarity", both(checks), "an agreeing READY and an agreeing NOT READY"),
        Item("3 release polarity", both(releases), "an agreeing READY and an agreeing NOT READY"),
        Item(
            "4 triage",
            None,
            f"operator: {len(found.to_triage)} earlier disagreement or error lines to triage",
        ),
        Item("5 verdict rules", None, "operator: decision V1 made before the count started"),
    ]


def met(items: list[Item]) -> bool:
    """True when every item the calculator can decide is met."""
    return all(item.met is not False for item in items)


def describe(line: dict) -> str:
    """Returns a one-line description of a shadow line."""
    where = line.get("qg_run") or line.get("tree", "")[:12]
    return (
        f"{line.get('kind', 'check')} {line.get('env', '')} {where} "
        f"qg={line['qg']} resoluto={line['resoluto']} {'; '.join(line.get('reasons', []))}"
    )


def render(project: str, found: Window, items: list[Item], criterion: Criterion) -> str:
    """Returns the printable report of one project's window."""
    out = [f"project {project or '(unnamed)'}: window of {len(found.lines)} agreeing lines"]
    for item in items:
        mark = {True: "met", False: "UNMET", None: "operator"}[item.met]
        out.append(f"  [{mark}] {item.name}: {item.detail}")
    out.append(f"  after the flip: hold {criterion.m} deploys through the shim without rollback")
    if found.to_triage:
        out.append("  lines before the window to triage:")
        out += [f"    - {describe(line)}" for line in found.to_triage]
    return "\n".join(out)


def by_project(lines: list[dict]) -> dict[str, list[dict]]:
    """Groups the lines by project, keeping file order; lines without a project are skipped."""
    grouped: dict[str, list[dict]] = {}
    for line in lines:
        if line.get("project"):
            grouped.setdefault(line["project"], []).append(line)
    return grouped
