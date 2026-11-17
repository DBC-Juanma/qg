"""No test may touch the real qg state, the real antfarm inbox or the real home directory."""

import os
import tempfile
from pathlib import Path

_SESSION = Path(tempfile.mkdtemp(prefix="qg-tests-"))
os.environ["QG_STATE_ROOT"] = str(_SESSION / "state")
os.environ["ANTFARM_DIR"] = str(_SESSION / "antfarm")
os.environ["HOME"] = str(_SESSION / "home")

import pytest  # noqa: E402

from qg import paths  # noqa: E402

DERIVED = {
    "QUEUE_LOCK": "queue.lock",
    "PLAYWRIGHT_LOCK": "playwright.lock",
    "QA_BOOT_LOCK": "qa-boot.lock",
    "RUNS": "runs",
    "CYCLES": "cycles",
    "CACHE": "cache",
    "CALIBRATION": "calibration",
    "SIMPLIFY_QUEUE": "simplify-queue",
    "AUDIT_RULES": "audit/semgrep",
    "AUDIT_LEDGER": "audit/ledger",
    "LITE": "lite",
    "RULES": "rules",
    "VENVS": "cache/venvs",
    "NODE": "cache/node",
    "NPM": "cache/npm",
    "ENABLED": "enabled",
    "OVERRIDES": "overrides.log",
    "SHADOW": "shadow.jsonl",
    "SHADOW_PENDING": "shadow-pending.jsonl",
    "DEPLOYED": "deployed.json",
    "BOARD": "board.html",
    "SETTINGS": "settings.json",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Points the state root, the antfarm inbox and the home directory at the test's tmp_path."""
    root = tmp_path.resolve() / "isolated-state"
    monkeypatch.setenv("QG_STATE_ROOT", str(root))
    monkeypatch.setenv("ANTFARM_DIR", str(tmp_path / "isolated-antfarm"))
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(paths, "STATE_ROOT", root)
    for name, relative in DERIVED.items():
        monkeypatch.setattr(paths, name, root / relative)
    monkeypatch.setattr(paths, "USER_UNIT_DIR", tmp_path / "isolated-home/.config/systemd/user")
    paths.ensure()
