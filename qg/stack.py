"""The app stack of the QA stage: a boot command in its own process group, readiness, log."""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from qg import proc
from qg.util import read_tail

GRACE_SECONDS = 30
_running: list["Stack"] = []


@dataclass
class Stack:
    """A running boot command and the file its output goes to."""

    child: subprocess.Popen[bytes]
    log: Path

    def stop(self) -> None:
        """Terminates the boot command's process group, killing it when it ignores the request."""
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            deadline = time.monotonic() + GRACE_SECONDS
            while self.child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait()

    def died(self) -> str:
        """Returns why the boot command is gone (exit code, OOM, last log lines); empty if alive."""
        code = self.child.poll()
        if code is None:
            return ""
        unit = proc.unit_of([str(a) for a in self.child.args])
        oom = unit is not None and proc.killed_by_oom(137, [f"--unit={unit}"])
        tail = " | ".join(self.output().strip().splitlines()[-3:])
        return f"exit {code}{', out of memory in its cgroup' if oom else ''}; log tail: {tail}"

    def output(self) -> str:
        """Returns what the boot command printed so far."""
        return read_tail(self.log) if self.log.exists() else ""


def start(
    boot: str,
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    wrap: Callable[[list[str]], list[str]] = list,
) -> Stack:
    """Starts `boot` with bash in its own process group, logging to `log`; stays registered.

    `wrap` puts the command in a cgroup (it receives the argv and returns the argv to run).
    """
    with log.open("ab") as out:
        child = subprocess.Popen(
            wrap(proc.shell(boot)),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=proc.default_signals,
        )
    stack = Stack(child, log)
    _running.append(stack)
    return stack


def wait_ready(stack: Stack, url: str, timeout: float, marker: str | None = None) -> bool:
    """Waits until `url` answers and, when `marker` is set, the log holds it too.

    Returns False when the boot command exits first or the time is up.
    """
    deadline = time.monotonic() + timeout
    marked = marker is None
    while time.monotonic() < deadline:
        if stack.child.poll() is not None:
            return False
        marked = marked or (marker or "") in stack.output()
        probe = ["curl", "-fsS", "-o", "/dev/null", "--max-time", "3", url]
        if marked and proc.run(probe, timeout=10).returncode == 0:
            return True
        time.sleep(1)
    return False


def stop_all() -> None:
    """Stops every stack this process started."""
    while _running:
        _running.pop().stop()
