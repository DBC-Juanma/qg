"""Cycles: every run of one feature on one branch, and the single artifact that reports them."""

import hashlib
import json
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from qg import paths
from qg.util import atomic_write, die, now_iso

STATUSES = ("open", "ready")
ARTIFACT_URL = re.compile(r"^https://claude\.ai/(?:code/)?artifact/\S+$")


@dataclass
class Cycle:
    """The runs of one repo+branch+feature, in order, and the artifact URL once published."""

    id: str
    repo: str
    branch: str
    feature: str
    plan: str
    runs: list[str] = field(default_factory=list)
    status: str = "open"
    artifact_url: str | None = None
    created: str = ""
    updated: str = ""
    closed_at: str | None = None
    closed_sha: str = ""
    closed_run: str = ""
    closed_env: str = ""
    reopened: list[dict[str, str]] = field(default_factory=list)
    prd: str = ""
    phase: str = ""
    prd_artifact_url: str | None = None
    acs: list[str] = field(default_factory=list)


def cycle_id(repo: str, branch: str, feature: str) -> str:
    """Returns the stable id of the cycle of `feature` on `repo`@`branch`: slug plus short hash."""
    normalised = " ".join(feature.split()).casefold()
    digest = hashlib.sha256(f"{repo}\n{branch}\n{normalised}".encode()).hexdigest()[:8]
    plain = unicodedata.normalize("NFKD", normalised).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:32].strip("-") or "feature"
    return f"{slug}-{digest}"


def phase_cycle_id(repo: str, prd: str, phase: str) -> str:
    """Returns the id of the cycle of phase `phase` of PRD `prd` on `repo`: slug plus short hash."""
    digest = hashlib.sha256(f"{repo}\n{prd}\n{phase}".encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{prd}-{phase}".casefold()).strip("-")[:32].strip("-")
    return f"{slug or 'phase'}-{digest}"


def find_phase(repo: str, prd: str, phase: str) -> Cycle | None:
    """Returns the cycle of that PRD phase on `repo`, whatever its id, None when there is none."""
    return next(
        (c for c in list_all() if (c.repo, c.prd, c.phase) == (repo, prd, phase)), None
    )


def identify_phase(repo: str, prd: str, phase: str) -> str:
    """Returns the cycle a run of PRD `prd`, phase `phase` joins: the existing one, else a new id."""
    found = find_phase(repo, prd, phase)
    return found.id if found else phase_cycle_id(repo, prd, phase)


def of_prd(prd: str) -> list[Cycle]:
    """Returns every phase cycle of PRD `prd`, ordered by phase."""
    return sorted((c for c in list_all() if c.prd == prd), key=lambda c: c.phase)


def assign(cycle: str, prd: str, phase: str) -> Cycle:
    """Maps an existing cycle to a PRD phase; refused when another cycle already holds it."""
    current = load(cycle)
    other = find_phase(current.repo, prd, phase)
    if other is not None and other.id != current.id:
        die(f"{prd} {phase} is already cycle {other.id}")
    current.prd, current.phase = prd, phase
    siblings = [c.prd_artifact_url for c in of_prd(prd) if c.prd_artifact_url]
    if siblings and not current.prd_artifact_url:
        current.prd_artifact_url = siblings[0]
    save(current)
    return current


def set_acs(cycle: str, ids: list[str]) -> None:
    """Records the AC ids the phase's page showed; the caller holds the queue lock."""
    current = load(cycle)
    if current.acs != ids:
        current.acs = ids
        save(current)


def set_prd_artifact(prd: str, url: str) -> list[Cycle]:
    """Stores the PRD's artifact URL on every phase cycle of it; the caller holds the lock."""
    if not ARTIFACT_URL.match(url):
        die(f"{url!r} is not a claude.ai artifact URL (https://claude.ai/artifact/...)")
    found = of_prd(prd)
    if not found:
        die(f"no cycle belongs to PRD {prd!r}")
    for cycle in found:
        cycle.prd_artifact_url = url
        save(cycle)
    return found


def _feature_key(feature: str) -> str:
    """Returns the feature name as cycle ids see it: whitespace collapsed, case folded."""
    return " ".join(feature.split()).casefold()


def identify(repo: str, branch: str, feature: str) -> str:
    """Returns the cycle a run of `feature` joins: the branch's own, else a detached twin.

    A detached HEAD and a named branch are the same work seen two ways: when only the twin
    exists the run joins it, and when both do the one with an artifact URL wins.
    """
    own = cycle_id(repo, branch, feature)
    twins = [
        c
        for c in list_all()
        if c.repo == repo
        and _feature_key(c.feature) == _feature_key(feature)
        and "detached" in (c.branch, branch)
    ]
    own_cycle = load(own) if exists(own) else None
    pool = [c for c in [own_cycle, *twins] if c is not None]
    if not pool:
        return own
    return sorted(pool, key=lambda c: (not c.artifact_url, c.created, c.id))[0].id


def fold_twins() -> None:
    """Merges every pair of cycles of one repo+feature where one is detached; caller holds the lock.

    The survivor is the cycle with an artifact URL, else the older one. It takes the other's runs,
    attempt snapshots and topics (its own win); each moved run's state names the survivor.
    """
    from qg import queue

    groups: dict[tuple[str, str, str], list[Cycle]] = {}
    for cycle in list_all():
        key = (
            (cycle.repo, cycle.prd, cycle.phase)
            if cycle.prd
            else (cycle.repo, "", _feature_key(cycle.feature))
        )
        groups.setdefault(key, []).append(cycle)
    for key, group in groups.items():
        if len(group) < 2 or not (key[1] or any(c.branch == "detached" for c in group)):
            continue
        group.sort(key=lambda c: (not c.artifact_url, c.created, c.id))
        keeper, *others = group
        for other in others:
            if not key[1] and other.branch != "detached" and keeper.branch != "detached":
                continue
            _absorb(keeper, other, queue)


def _absorb(keeper: Cycle, other: Cycle, queue: Any) -> None:
    """Moves `other`'s runs, snapshots and topics into `keeper` and deletes `other`."""
    keeper.runs = sorted({*keeper.runs, *other.runs})
    for name in ("attempts", "thumbs"):
        source = paths.cycle_dir(other.id) / name
        target = paths.cycle_dir(keeper.id) / name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if not (target / item.name).exists():
                    shutil.move(str(item), target / item.name)
    mine, theirs = paths.topics_file(keeper.id), paths.topics_file(other.id)
    if theirs.exists():
        base = json.loads(mine.read_text()) if mine.exists() else {"topics": {}, "findings": None}
        extra = json.loads(theirs.read_text())
        for key, topic in extra.get("topics", {}).items():
            base["topics"].setdefault(key, topic)
        atomic_write(mine, json.dumps(base, indent=2) + "\n")
    for run_id in other.runs:
        if paths.state_file(run_id).exists():
            queue.update(run_id, lambda state: setattr(state, "cycle", keeper.id))
    save(keeper)
    shutil.rmtree(paths.cycle_dir(other.id), ignore_errors=True)


def exists(cycle: str) -> bool:
    """Tells whether cycle `cycle` has a cycle.json."""
    return paths.cycle_file(cycle).exists()


def load(cycle: str) -> Cycle:
    """Reads cycle `cycle`; exits when it does not exist."""
    path = paths.cycle_file(cycle)
    if not path.exists():
        die(f"no cycle {cycle!r}")
    return Cycle(**json.loads(path.read_text()))


def save(cycle: Cycle) -> None:
    """Writes the cycle atomically, stamping its update time."""
    cycle.updated = now_iso()
    paths.cycle_dir(cycle.id).mkdir(parents=True, exist_ok=True)
    atomic_write(paths.cycle_file(cycle.id), json.dumps(asdict(cycle), indent=2) + "\n")


def list_all() -> list[Cycle]:
    """Returns every cycle on disk, ordered by id."""
    if not paths.CYCLES.exists():
        return []
    found = []
    for entry in sorted(paths.CYCLES.iterdir()):
        if (entry / "cycle.json").exists():
            found.append(load(entry.name))
    return found


def resolve(prefix: str) -> str:
    """Returns the id of the one cycle whose id equals or starts with `prefix`."""
    ids = [c.id for c in list_all()]
    if prefix in ids:
        return prefix
    matches = [i for i in ids if i.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    die(
        f"{'ambiguous' if matches else 'unknown'} cycle {prefix!r}"
        + (f": {', '.join(matches)}" if matches else "")
    )


def append_run(
    run_id: str,
    ident: str,
    repo: str,
    branch: str,
    feature: str,
    plan: str,
    prd: str = "",
    phase: str = "",
) -> Cycle:
    """Opens cycle `ident` when new and appends `run_id`; the caller holds the queue lock."""
    if exists(ident):
        cycle = load(ident)
    else:
        inherited = next((c.prd_artifact_url for c in of_prd(prd) if c.prd_artifact_url), None)
        cycle = Cycle(
            id=ident,
            repo=repo,
            branch=branch,
            feature=feature,
            plan=plan,
            created=now_iso(),
            prd=prd,
            phase=phase,
            prd_artifact_url=inherited if prd else None,
        )
    cycle.runs.append(run_id)
    cycle.status = "open"
    if plan:
        cycle.plan = plan
    save(cycle)
    return cycle


def set_status(cycle: str, status: str) -> None:
    """Sets the cycle to open or ready; the caller holds the queue lock."""
    current = load(cycle)
    current.status = status
    save(current)


def close(cycle: str, run_id: str, sha: str, env: str) -> Cycle:
    """Closes the cycle at a deploy: the READY run that was deployed and the sha released."""
    current = load(cycle)
    current.closed_at = now_iso()
    current.closed_sha = sha
    current.closed_run = run_id
    current.closed_env = env
    current.status = "ready"
    save(current)
    return current


def reopen(cycle: str, reason: str) -> Cycle:
    """Reopens a closed cycle after its deploy was rolled back, recording why."""
    current = load(cycle)
    current.reopened.append(
        {
            "at": now_iso(),
            "reason": reason,
            "sha": current.closed_sha,
            "run": current.closed_run,
            "env": current.closed_env,
        }
    )
    current.closed_at = None
    current.closed_sha = current.closed_run = current.closed_env = ""
    save(current)
    return current


def set_artifact(cycle: str, url: str) -> Cycle:
    """Stores the cycle's artifact URL once; a different URL is refused, one cycle keeps one URL."""
    if not ARTIFACT_URL.match(url):
        die(f"{url!r} is not a claude.ai artifact URL (https://claude.ai/artifact/...)")
    current = load(cycle)
    if current.artifact_url and current.artifact_url != url:
        die(
            f"cycle {cycle} already has the artifact {current.artifact_url}; "
            f"one cycle keeps one URL (edit {paths.cycle_file(cycle)} by hand to change it)"
        )
    current.artifact_url = url
    save(current)
    return current
