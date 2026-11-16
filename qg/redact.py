"""Redaction of anything that can reach the published page, report.md and report.json."""

import re
from dataclasses import replace
from pathlib import Path

from qg import config
from qg.config import Config

MARK = "[redacted]"
MIN_SECRET_LENGTH = 4
KEY_NAME = (
    r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"
    r"[A-Za-z0-9_.-]*"
)
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"',;]+"),
        rf"\1{MARK}",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MARK}"),
    (
        re.compile(rf"(?i)(([\"']?){KEY_NAME}\2\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"'}}\]]+)"),
        rf"\1{MARK}",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), MARK),
    (re.compile(r"\benc:v[12]:[A-Za-z0-9+/=_.-]+"), MARK),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"), MARK),
    (re.compile(r"\b(?:sk|whsec|polar|oat)[_-][A-Za-z0-9_-]{12,}"), MARK),
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{8,}"), MARK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MARK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MARK),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@"), rf"\1{MARK}@"),
)
EMAIL = re.compile(
    r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
)


def parse_env(text: str) -> dict[str, str]:
    """Parses KEY=VALUE lines (export, quotes and trailing comments handled) without a shell."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        found[name.strip()] = value
    return found


def run_secrets(cfg: Config) -> tuple[str, ...]:
    """Returns the secret values of a run: the QA env-file keys and every account password."""
    qa = cfg.qa
    if qa is None:
        return ()
    values = [account.password for account in qa.accounts]
    if qa.secrets_env_file and qa.secrets_keys:
        file = Path(qa.secrets_env_file).expanduser()
        if file.is_file():
            found = parse_env(file.read_text())
            values += [found[key] for key in qa.secrets_keys if found.get(key)]
    return tuple(values)


def secrets_of(request_configs: list[dict]) -> tuple[str, ...]:
    """Returns the secret values of every stored request configuration, parsed strictly."""
    found: list[str] = []
    for raw in request_configs:
        found += run_secrets(config.parse(raw, "a stored request"))
    return tuple(found)


def mask_emails(text: str) -> str:
    """Masks the local part of every email but its first letter: t***@example.com."""
    return EMAIL.sub(r"\1***@\2", text)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Replaces the run's secret values and every generic credential pattern with [redacted]."""
    for value in sorted({v for v in secrets if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True):
        text = text.replace(value, MARK)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Redacts credentials and masks emails: the form every published text takes."""
    return mask_emails(redact(text, secrets))


def scrub_tree(value, secrets: tuple[str, ...]):
    """Returns `value` with every string in it, at any depth, passed through `clean`."""
    if isinstance(value, str):
        return clean(value, secrets)
    if isinstance(value, list):
        return [scrub_tree(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: scrub_tree(item, secrets) for key, item in value.items()}
    return value


def finding(item, secrets: tuple[str, ...], emails: bool):
    """Returns a copy of a Finding whose free-text fields are redacted (emails masked if asked)."""
    scrub = (lambda text: clean(text, secrets)) if emails else (lambda text: redact(text, secrets))
    return replace(
        item,
        title=scrub(item.title),
        area=scrub(item.area),
        steps=[scrub(step) for step in item.steps],
        expected=scrub(item.expected),
        actual=scrub(item.actual),
    )
