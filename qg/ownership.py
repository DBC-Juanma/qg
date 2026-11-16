"""Who owns a failing test: the release phase whose AC tag or PRD tag its scenario carries."""

import re
from collections import Counter

from qg import acs, git

NO_OWNER = "unowned"


def label(prd: str, phase: str) -> str:
    """Returns how a phase is written on the pages: `F01·P1`."""
    return f"{prd.upper()}·{phase.upper()}"


def owner_of(tags: tuple[str, ...], phases: list[tuple[str, str]]) -> str:
    """Returns the phase label that owns a scenario with `tags`, empty when none does.

    An AC tag `@F01-P1-AC03` or `@ABC-11-P1-AC03` names its phase. Any other tag `@F01-D10` names
    only the PRD (the longest known PRD id the tag starts with, then a hyphen), and owns the
    scenario when the release holds exactly one phase of that PRD.
    """
    known = {(prd.upper(), phase.upper()) for prd, phase in phases}
    for tag in tags:
        for prd, phase in sorted(known):
            if re.fullmatch(rf"@{re.escape(prd)}-{re.escape(phase)}-AC\d+", tag, re.IGNORECASE):
                return label(prd, phase)
    for tag in tags:
        named = [prd for prd, _ in known if tag.upper().startswith(f"@{prd}-")]
        if named:
            prd = max(named, key=len)
            mine = sorted(pair for pair in known if pair[0] == prd)
            if len(mine) == 1:
                return label(*mine[0])
    return ""


def scenario_tags(repo: str, sha: str) -> list[tuple[str, tuple[str, ...]]]:
    """Returns (scenario name, tags) of every feature file of the repo at `sha`."""
    listed = git.out(repo, "ls-tree", "-r", "--name-only", sha, "--", acs.FEATURES_DIR)
    scenarios: list[tuple[str, tuple[str, ...]]] = []
    for file in (name for name in listed.splitlines() if name.endswith(".feature")):
        scenarios += acs.walk(git.out(repo, "show", f"{sha}:{file}"))
    return scenarios


def assign(
    failures: list[str],
    scenarios: list[tuple[str, tuple[str, ...]]],
    phases: list[tuple[str, str]],
) -> list[str]:
    """Returns the owner label of every failure line, in order; empty for a test with no owner.

    A failure line belongs to the scenario whose name, as a test-function slug, it holds; the
    longest such name wins so a short name never claims a longer one's failure.
    """
    ordered = sorted(((acs.slug(name), tags) for name, tags in scenarios), key=lambda x: -len(x[0]))
    owners = []
    for line in failures:
        text = acs.slug(line)
        match = next((tags for name, tags in ordered if name and name in text), None)
        owners.append(owner_of(match, phases) if match is not None else "")
    return owners


def summary(owners: list[str]) -> str:
    """Returns the one line of a failed Playwright stage: how many tests, owned by whom."""
    count = len(owners)
    noun = "test" if count == 1 else "tests"
    owned = Counter(owner for owner in owners if owner)
    parts = [f"{count} {noun} failed"]
    if owned:
        names = ", ".join(f"{who} ({n})" if count > 1 else who for who, n in sorted(owned.items()))
        parts.append(f"owned by {names}")
    orphans = owners.count("")
    if orphans:
        parts.append(f"{orphans} {NO_OWNER}")
    return ", ".join(parts)


def blocked_by(owners: list[str], mine: str) -> str:
    """Returns the reason a phase page gives for a failed stage: its own tests, or who blocks it."""
    own = owners.count(mine)
    others = sorted({owner for owner in owners if owner and owner != mine})
    orphans = owners.count("")
    parts = []
    if own:
        parts.append(f"{own} test(s) of this phase failed")
    if others:
        parts.append(f"blocked by {', '.join(others)}")
    if orphans:
        parts.append(f"{orphans} test(s) {NO_OWNER}")
    return "; ".join(parts)
