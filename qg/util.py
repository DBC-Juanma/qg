"""Helpers shared by every module: fatal errors, timestamps, atomic writes, placeholders."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn


def die(message: str) -> NoReturn:
    """Raises SystemExit carrying `message`, so the process exits 1 and finally blocks still run."""
    raise SystemExit(f"qg: {message}")


def now_iso() -> str:
    """Returns the current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    """Parses a timestamp written by `now_iso`."""
    return datetime.fromisoformat(text)


def elapsed(start: str | None, end: str | None) -> float | None:
    """Returns the seconds from `start` to `end` (now when `end` is None), None without `start`."""
    if start is None:
        return None
    finish = parse_iso(end) if end else datetime.now(UTC)
    return (finish - parse_iso(start)).total_seconds()


def human(seconds: float | None) -> str:
    """Formats a duration as 12s, 4m07s or 1h03m; '-' when unknown."""
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def ago(text: str | None) -> str:
    """Formats the time since the timestamp `text` as '3m07s ago'; '-' when unknown."""
    seconds = elapsed(text, None)
    return "-" if seconds is None else f"{human(seconds)} ago"


def atomic_write(path: Path, text: str) -> None:
    """Writes `text` to `path` so readers see either the old or the new content, never a mix."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def fill(template: str, values: dict[str, str]) -> str:
    """Replaces every {name} of `values` not preceded by `$`; other braces stay untouched."""
    for name, value in values.items():
        template = re.sub(rf"(?<!\$)\{{{re.escape(name)}\}}", lambda _, v=value: v, template)
    return template


def read_tail(path: Path, start: int = 0, limit: int = 1 << 20) -> str:
    """Returns the last `limit` bytes of `path` at or after offset `start`, decoded leniently."""
    size = path.stat().st_size
    begin = max(start, size - limit)
    with path.open("rb") as handle:
        handle.seek(begin)
        return handle.read().decode("utf-8", errors="replace")
