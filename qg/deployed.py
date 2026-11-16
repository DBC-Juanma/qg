"""The last-deployed record: per repo and env, the sha and tree a passing check released."""

import json
from typing import Any

from qg import paths, queue
from qg.util import atomic_write, now_iso


def load() -> dict[str, dict[str, dict[str, Any]]]:
    """Returns the whole record, empty when nothing was ever deployed."""
    if not paths.DEPLOYED.exists():
        return {}
    return json.loads(paths.DEPLOYED.read_text())


def get(repo: str, env: str) -> dict[str, Any] | None:
    """Returns {sha, tree, at} of the last deploy of `repo` to `env`, None when unknown."""
    return load().get(repo, {}).get(env)


def record(repo: str, env: str, sha: str, tree: str) -> None:
    """Stores `sha` as the last deploy of `repo` to `env`."""
    with queue.locked():
        paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        data = load()
        data.setdefault(repo, {})[env] = {"sha": sha, "tree": tree, "at": now_iso()}
        atomic_write(paths.DEPLOYED, json.dumps(data, indent=2) + "\n")
