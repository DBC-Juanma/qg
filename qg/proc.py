"""Subprocess helpers: captured runs, logged runs with timeouts, systemd scope control."""

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_EXIT = 124
DEFAULT_TIMEOUT = 120.0


class Cancelled(SystemExit):
    """Raised by the executor's SIGTERM handler; exits with status 143."""

    def __init__(self) -> None:
        super().__init__(143)


class Interrupted(Exception):
    """Marks a run whose executor disappeared before writing its final state."""


class Infrastructure(Exception):
    """Ends a run for a cause outside the code under test: the run ends `error`, not `failed`."""


class StackFailure(Infrastructure):
    """The QA app stack did not boot, or died, or the agent left no result: queued again once."""


class AccountFailure(Infrastructure):
    """The Claude account cannot run an agent now (limit, disabled, login): never queued again."""


@dataclass(frozen=True)
class Result:
    """Exit code of a logged command and whether its timeout killed it."""

    code: int
    timed_out: bool


def default_signals() -> None:
    """Restores the default SIGTERM and SIGINT handlers in a child before it execs."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def shell(command: str) -> list[str]:
    """Returns the argv that runs a configured command in bash, a failing pipe stage failing it."""
    return ["bash", "-o", "pipefail", "-c", command]


def run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Runs `args` to completion within `timeout` seconds; failure is data.

    A command past its timeout is killed and returned with exit 124 and a stderr that says so.
    """
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            preexec_fn=default_signals,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout
        text = partial.decode(errors="replace") if isinstance(partial, bytes) else partial or ""
        note = f"timed out after {timeout:g}s"
        return subprocess.CompletedProcess(args, TIMEOUT_EXIT, text, note)


def run_logged(
    args: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    log: Path,
    timeout: float,
) -> Result:
    """Runs `args` in its own process group, appending output to `log`; returns code and timeout."""
    with log.open("ab") as out:
        child = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=default_signals,
        )
        timed_out = False
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGKILL)
            code = child.wait()
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return Result(code, timed_out)


def executor_alive(pid: int, run_id: str) -> bool:
    """Tells whether `pid` is still the executor of `run_id`, not a process that reused the pid."""
    try:
        words = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"_execute" in words and run_id.encode() in words


def scope_dead(run_id: str) -> bool:
    """Tells whether systemd affirms the run's scope is gone; a query that fails is no answer."""
    answer = run(["systemctl", "--user", "is-active", f"qg-{run_id}.scope"]).stdout.strip()
    return answer in ("inactive", "failed")


def stop_scope(run_id: str) -> None:
    """Stops the run's systemd scopes, the executor's and its lanes', which signals every process.

    Absent is fine. The lane scopes are named `qg-<run id>-<lane>-<n>.scope`.
    """
    run(["systemctl", "--user", "stop", f"qg-{run_id}.scope", f"qg-{run_id}-*.scope"])


def stop_lanes(run_id: str) -> None:
    """Stops only the run's lane scopes, never the executor's own; absent is fine."""
    run(["systemctl", "--user", "stop", f"qg-{run_id}-*.scope"])


KILL_CODES = (137, 143, -9, -15)
OOM_LINE = re.compile(r"oom-kill|killed by the OOM killer", re.IGNORECASE)
JOURNAL_SECONDS = 20


def unit_of(argv: list[str]) -> str | None:
    """Returns the systemd unit name of a command wrapped by `scoped`, None when it is not."""
    for word in argv:
        if word.startswith("--unit="):
            return word.removeprefix("--unit=")
    return None


def oom_in(journal: str) -> bool:
    """Tells whether a unit's journal says the kernel's OOM killer (or systemd) killed it."""
    return OOM_LINE.search(journal) is not None


def killed_by_oom(code: int, argv: list[str]) -> bool:
    """Tells whether a command that exited by a kill signal was killed for lack of memory.

    Only an exit by SIGKILL or SIGTERM is looked up, in the journal of its scope; any other
    code is a failure of the command itself.
    """
    unit = unit_of(argv)
    if unit is None or code not in KILL_CODES:
        return False
    journal = run(
        ["journalctl", "--user", "--no-pager", "-q", "-o", "cat", "--since=-3h", "-u", unit],
        timeout=JOURNAL_SECONDS,
    )
    return oom_in(journal.stdout)


def scoped(run_id: str, name: str, cpu_quota: str, memory_max: str, serial: int) -> list[str]:
    """Returns the argv prefix that runs a command in its own limited systemd scope.

    `name` says which lane the scope belongs to and `serial` keeps unit names unique within the
    run. The scope is a sibling of the executor's, so its limits are its own.
    """
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=qg-{run_id}-{name}-{serial}",
        "-p",
        f"CPUQuota={cpu_quota}",
        "-p",
        f"MemoryMax={memory_max}",
        "-p",
        "MemorySwapMax=0",
    ]


def service_state() -> str:
    """Returns systemd's state of the runner service: active, inactive, failed or unknown."""
    result = run(["systemctl", "--user", "is-active", "qg-runner.service"])
    return result.stdout.strip() or "unknown"
