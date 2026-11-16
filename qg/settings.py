"""Tool paths fixed at install time: the node and npx the QA agent's browser server runs with."""

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from qg import paths, proc
from qg.util import atomic_write, die

MIN_NODE_MAJOR = 18
NODE_VERSION = re.compile(r"^v(\d+)\.\d+\.\d+")


class SettingsError(Exception):
    """The install-time tool paths are missing or unreadable; the text names the fix."""


@dataclass(frozen=True)
class Tools:
    """Absolute node and npx, as the runner unit's PATH finds them."""

    node: str
    npx: str

    @property
    def bin_dirs(self) -> list[str]:
        """Returns the distinct directories holding node and npx, node's first."""
        return list(dict.fromkeys([os.path.dirname(self.node), os.path.dirname(self.npx)]))

    @property
    def roots(self) -> list[Path]:
        """Returns the install roots, the parents of the bin directories, the agent must read."""
        return list(dict.fromkeys(Path(directory).parent for directory in self.bin_dirs))


def node_major(node: str, path: str) -> int | None:
    """Returns the major version `node --version` reports under PATH `path`; None when it fails."""
    result = proc.run([node, "--version"], env={"PATH": path}, timeout=30)
    found = NODE_VERSION.match(result.stdout.strip())
    return int(found.group(1)) if result.returncode == 0 and found else None


def npx_version(npx: str, path: str) -> str | None:
    """Returns what `npx --version` prints under PATH `path`; None when it fails."""
    result = proc.run([npx, "--version"], env={"PATH": path}, timeout=60)
    return result.stdout.strip() or None if result.returncode == 0 else None


def resolve(path: str) -> Tools:
    """Finds absolute node and npx on `path`; exits unless node is 18 or newer and npx runs."""
    node = shutil.which("node", path=path)
    npx = shutil.which("npx", path=path)
    if not node or not npx:
        die(f"node and npx must be on PATH to install qg (found node={node}, npx={npx})")
    tools = Tools(os.path.abspath(node), os.path.abspath(npx))
    major = node_major(tools.node, path)
    if major is None or major < MIN_NODE_MAJOR:
        die(f"{tools.node} must be node >= {MIN_NODE_MAJOR} for @playwright/mcp (got {major})")
    if npx_version(tools.npx, path) is None:
        die(f"{tools.npx} --version failed")
    return tools


def save(tools: Tools) -> None:
    """Writes the tool paths the QA stage launches its browser server with."""
    paths.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.SETTINGS, json.dumps(asdict(tools), indent=2) + "\n")


def load() -> Tools:
    """Reads the tool paths saved by `qg install-service`; raises SettingsError when unusable."""
    if not paths.SETTINGS.is_file():
        raise SettingsError(f"{paths.SETTINGS} is missing: run `qg install-service`")
    try:
        raw = json.loads(paths.SETTINGS.read_text())
        tools = Tools(node=raw["node"], npx=raw["npx"])
    except (ValueError, KeyError, TypeError) as exc:
        message = f"{paths.SETTINGS} is unreadable ({exc!r}): run `qg install-service`"
        raise SettingsError(message) from exc
    for name, path in asdict(tools).items():
        if not os.path.isabs(path) or not os.access(path, os.X_OK):
            raise SettingsError(f"{name} {path} is not an executable: run `qg install-service`")
    return tools
