"""Bubblewrap confinement of the QA agent: what headless Claude Code and its MCP child may see."""

import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from qg import paths, proc
from qg.settings import Tools

BWRAP = "/usr/bin/bwrap"
READ_ONLY = (Path("/usr"), Path("/etc"), Path("/opt"))
TOP_LEVEL_LINKS = ("bin", "lib", "lib64")
RESOLVER = Path("/run/systemd/resolve")
SAFE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
LOGIN_MARGIN_SECONDS = 900
REFRESH_BUDGET_USD = "0.05"
REFRESH_WINDOW_SECONDS = 270
RENEW_POLL_SECONDS = 60
RENEW_GRACE_SECONDS = 600
RENEW_ATTEMPTS = 3
SWITCH_WAIT_SECONDS = 150
SWITCH_POLL_SECONDS = 5
ROTATION_WAIT_SECONDS = SWITCH_WAIT_SECONDS
REAL_EXPIRY_MS = 10**12
LIMIT_TRANSCRIPT = Path("projects") / "-qg-qa" / "limits.jsonl"
DEFAULT_LIMIT_TEXT = "You've hit your session limit"


class JailError(Exception):
    """The QA agent cannot be confined or started; the text becomes the stage summary."""


class LoginError(JailError, proc.AccountFailure):
    """The Claude login is missing, unreadable or too short for the agent and did not renew."""


def real_home() -> Path:
    """Returns the home directory of the user the runner runs as."""
    return Path.home()


def login_file() -> Path:
    """Returns the Claude login the QA agent borrows.

    It is $QG_CLAUDE_CONFIG_DIR/.credentials.json, ~/.claude by default: the directory the
    account rotator (claude-rotate) swaps when an account hits its usage limit.
    """
    configured = os.environ.get("QG_CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else real_home() / ".claude"
    return base / ".credentials.json"


def live_config_dir() -> Path:
    """Returns the config directory holding the live login."""
    return login_file().parent


def playwright_cache() -> Path:
    """Returns the directory of Playwright's own browsers, which the agent may read."""
    return real_home() / ".cache" / "ms-playwright"


def claude_binary() -> Path:
    """Returns the real path of the claude executable on the runner's PATH, links resolved."""
    found = shutil.which("claude")
    if found is None:
        raise JailError("claude is not on the runner's PATH")
    return Path(found).resolve()


def _read_login() -> dict[str, object]:
    """Returns the claudeAiOauth entry of the login file, raising LoginError when unusable."""
    file = login_file()
    if not file.is_file():
        raise LoginError(f"{file} is missing: sign in with `claude` once")
    try:
        entry = json.loads(file.read_text())["claudeAiOauth"]
        float(entry["expiresAt"])
    except (ValueError, KeyError, TypeError) as exc:
        raise LoginError(f"{file} holds no usable claudeAiOauth login ({exc!r})") from exc
    return entry


def login_seconds_left() -> float:
    """Returns how many seconds the access token of the Claude login stays valid."""
    return float(_read_login()["expiresAt"]) / 1000 - time.time()


def refresh_login(claude: Path, env: dict[str, str]) -> None:
    """Runs one restricted claude call outside the jail so the CLI itself renews the login.

    The call runs on the real config directory of the login: the CLI refreshes the token only
    close to its expiry and rotates the refresh token in place. The jail only ever gets a copy,
    since a token refreshed in the copy would rotate the one every other Claude session holds.
    """
    proc.run(
        [
            str(claude),
            "-p",
            "Reply with the single word OK",
            "--strict-mcp-config",
            "--max-turns",
            "1",
            "--max-budget-usd",
            REFRESH_BUDGET_USD,
            "--output-format",
            "json",
        ],
        cwd=paths.STATE_ROOT,
        env={**env, "CLAUDE_CONFIG_DIR": str(login_file().parent)},
        timeout=180,
    )


def _say(text: str) -> None:
    """Writes one line of the renewal's progress to the runner's log."""
    print(f"qg: {text}", file=sys.stderr, flush=True)


def _rotating() -> str:
    """Returns why the login file looks caught mid-rotation, empty when it does not.

    A file that is unreadable, holds no expiry, or one before REAL_EXPIRY_MS (2001: zero,
    negative, or seconds instead of milliseconds) is no real login. A missing file is no rotation.
    """
    if not login_file().is_file():
        return ""
    try:
        expires = float(_read_login()["expiresAt"])
    except JailError as exc:
        return str(exc)
    return "" if expires >= REAL_EXPIRY_MS else f"its expiresAt is {expires:g}"


def _settle(
    clock: Callable[[], float], sleep: Callable[[float], None], say: Callable[[str], None]
) -> None:
    """Reads the login again every SWITCH_POLL_SECONDS while it looks caught mid-rotation.

    Returns once it does not, or after ROTATION_WAIT_SECONDS: the file is then judged as it is.
    """
    end = clock() + ROTATION_WAIT_SECONDS
    why = _rotating()
    if why:
        say(
            f"the Claude login looks mid-rotation ({why}): "
            f"reading it again for up to {ROTATION_WAIT_SECONDS} s"
        )
    while why and clock() < end:
        sleep(SWITCH_POLL_SECONDS)
        why = _rotating()


def ensure_login(
    claude: Path,
    env: dict[str, str],
    valid_for: float,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = _say,
) -> None:
    """Returns once the login stays valid for `valid_for` seconds, renewing it on the real config dir.

    The CLI refreshes only when under REFRESH_WINDOW_SECONDS remain, so a login that is valid
    but short is waited down to the window, then renewed by one claude call, and the new expiry
    is confirmed. Raises JailError when the wait bound (the time to reach the window plus a
    grace) passes or RENEW_ATTEMPTS calls leave the expiry where it was.

    A login file caught mid-rotation by the account rotator is first read again for up to
    ROTATION_WAIT_SECONDS (see `_settle`), with no renewal call meanwhile.
    """
    _settle(clock, sleep, say)
    start = clock()
    left = float(_read_login()["expiresAt"]) / 1000 - start
    if left >= valid_for:
        return
    deadline = start + max(0.0, left - REFRESH_WINDOW_SECONDS) + RENEW_GRACE_SECONDS
    attempts = 0
    while True:
        entry = _read_login()
        left = float(entry["expiresAt"]) / 1000 - clock()
        if left >= valid_for:
            return
        if clock() >= deadline or attempts >= RENEW_ATTEMPTS:
            raise LoginError(
                f"the Claude login in {login_file()} is valid for {int(left // 60)} min and the "
                f"agent needs {int(valid_for // 60)}: it did not renew "
                f"({attempts} renewal call(s)); open `claude` once to renew it"
            )
        if left > REFRESH_WINDOW_SECONDS:
            wait = min(RENEW_POLL_SECONDS, left - REFRESH_WINDOW_SECONDS)
            say(
                f"waiting for the Claude login to renew, {int((left - REFRESH_WINDOW_SECONDS) // 60) + 1} min"
            )
            sleep(max(wait, 1.0))
            continue
        attempts += 1
        say(f"renewing the Claude login ({int(left // 60)} min left, attempt {attempts})")
        refresh_login(claude, env)
        if float(_read_login()["expiresAt"]) <= float(entry["expiresAt"]):
            sleep(RENEW_POLL_SECONDS)


def prepare_home(home: Path, valid_for: float) -> None:
    """Creates the agent's fresh HOME: only the Claude login, mode 0600, and the npm mount point.

    Only the oauth token is copied. The CLI authenticates with it alone, so ~/.claude.json's
    oauthAccount, which the rotator also swaps, stays out of the jail.

    Raises JailError unless the login stays valid for `valid_for` seconds, so it never needs
    refreshing inside the jail.
    """
    entry = _read_login()
    left = float(entry["expiresAt"]) / 1000 - time.time()
    if left < valid_for:
        minutes = int(left // 60)
        raise LoginError(
            f"the Claude login in {login_file()} is valid for {minutes} min and the agent "
            f"needs {int(valid_for // 60)}: open `claude` once to renew it"
        )
    shutil.rmtree(home, ignore_errors=True)
    (home / ".claude").mkdir(parents=True, mode=0o700)
    (home / ".npm").mkdir(mode=0o700)
    target = home / ".claude" / ".credentials.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"claudeAiOauth": entry}, handle)


def home_token(home: Path) -> str | None:
    """Returns the access token copied into the agent's HOME, None when there is none."""
    file = home / ".claude" / ".credentials.json"
    if not file.is_file():
        return None
    return json.loads(file.read_text())["claudeAiOauth"].get("accessToken")


def signal_limit(text: str, *, clock: Callable[[], float] = time.time) -> None:
    """Tells the account rotator an account hit its limit, the one way it learns of it.

    The rotator reads limit errors from the transcripts under the live config dir and never
    from the jail's HOME, so one synthetic error entry is appended to a transcript there.
    """
    file = live_config_dir() / LIMIT_TRANSCRIPT
    file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": text or DEFAULT_LIMIT_TEXT}],
        },
    }
    with file.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def wait_for_new_login(
    token: str | None,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Waits up to `timeout` seconds for the live login's access token to differ from `token`.

    Returns True once the rotator has swapped the account in, False when it did not in time.
    """
    end = clock() + timeout
    while True:
        try:
            if _read_login().get("accessToken") != token:
                return True
        except JailError:
            pass
        if clock() >= end:
            return False
        sleep(SWITCH_POLL_SECONDS)


def discard_home(home: Path) -> None:
    """Removes the borrowed login from the agent's HOME once the agent is gone."""
    (home / ".claude" / ".credentials.json").unlink(missing_ok=True)


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    """Tells whether `path` is one of `roots` or lies below one."""
    return any(path == root or path.is_relative_to(root) for root in roots)


def command(
    inner: list[str],
    *,
    workdir: Path,
    writable: list[Path],
    workdir_writable: bool = True,
    home: Path,
    tools: Tools,
    claude: Path,
    npm_cache: Path,
    readable: tuple[Path, ...] = (),
) -> list[str]:
    """Returns the bwrap argv that runs `inner` with nothing of the host visible but this.

    Read-only: /usr, /etc, /opt, the links /bin /lib /lib64, the directory of the claude
    binary, the node install, Playwright's browser cache, the resolver socket directory and the
    `readable` directories.
    Read-write: the agent's working directory (read-only when `workdir_writable` is false) and the `writable` directories, which must exist, a fresh HOME (its .claude read-only)
    and an npm cache mounted as $HOME/.npm. A new /tmp, /proc and /dev; the network is shared;
    pid and ipc are not.
    """
    args = [BWRAP, "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--share-net"]
    for root in READ_ONLY:
        if root.exists():
            args += ["--ro-bind", str(root), str(root)]
    for name in TOP_LEVEL_LINKS:
        host = Path("/") / name
        if host.is_symlink():
            args += ["--symlink", os.readlink(host), str(host)]
        elif host.is_dir():
            args += ["--ro-bind", str(host), str(host)]
    if RESOLVER.is_dir():
        args += ["--ro-bind", str(RESOLVER), str(RESOLVER)]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    cache = playwright_cache()
    shown = [claude.parent, *tools.roots, *([cache] if cache.is_dir() else []), *readable]
    for path in dict.fromkeys(shown):
        if not _under(path, READ_ONLY):
            args += ["--ro-bind", str(path), str(path)]
    if not workdir_writable:
        args += ["--ro-bind", str(workdir), str(workdir)]
    for path in dict.fromkeys([*([workdir] if workdir_writable else []), *writable, home]):
        if not path.is_dir():
            raise JailError(f"{path} does not exist: the jail will not start without it")
        args += ["--bind", str(path), str(path)]
    config = home / ".claude"
    if not config.is_dir():
        raise JailError(f"{config} does not exist: the jail will not start without it")
    args += ["--ro-bind", str(config), str(config)]
    args += ["--bind", str(npm_cache), str(home / ".npm")]
    environment = {
        "HOME": str(home),
        "PATH": ":".join([*tools.bin_dirs, *SAFE_PATH]),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
        "DISABLE_AUTOUPDATER": "1",
    }
    if cache.is_dir():
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
    args.append("--clearenv")
    for name, value in environment.items():
        args += ["--setenv", name, value]
    return [*args, "--chdir", str(workdir), "--", *inner]
