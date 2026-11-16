"""Acceptance criteria of a PRD phase: the `@<prd>-<phase>-ACnn` tags of the repo's features."""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from qg import git

FEATURES_DIR = "tests/e2e/features"
OK = "ok"
FAILED = "failed"
UNTESTED = "untested"
UNKNOWN = "unknown"
NOT_RUN = "not_run"
LANGUAGE = re.compile(r"^\s*#\s*language\s*:\s*([A-Za-z-]+)", re.IGNORECASE)
FEATURE_WORDS = {
    "en": ("Feature", "Business Need", "Ability"),
    "es": ("Característica", "Funcionalidad", "Necesidad del negocio", "Habilidad"),
}
SCENARIO_WORDS = {
    "en": ("Scenario Outline", "Scenario Template", "Scenario", "Example"),
    "es": ("Esquema del escenario", "Plantilla del escenario", "Escenario", "Ejemplo"),
}


@dataclass(frozen=True)
class Covering:
    """One scenario that carries an AC tag: its feature file and its name."""

    file: str
    scenario: str


@dataclass(frozen=True)
class Criterion:
    """One AC of the phase: its id, the scenarios that cover it and the result they earned."""

    id: str
    tests: tuple[Covering, ...]
    status: str


def tag_pattern(prd: str, phase: str) -> re.Pattern[str]:
    """Returns the pattern of the AC tags of one PRD phase, capturing the AC id."""
    return re.compile(rf"@({re.escape(prd)}-{re.escape(phase)}-AC\d+)\b", re.IGNORECASE)


def fold(text: str) -> str:
    """Returns `text` lower-cased with its accents removed, so `Característica` matches any case."""
    plain = unicodedata.normalize("NFD", text)
    return "".join(c for c in plain if not unicodedata.combining(c)).casefold()


def keyword_pattern(words: dict[str, tuple[str, ...]], language: str) -> re.Pattern[str]:
    """Returns the pattern of a line that opens with one of the keywords and a colon.

    English keywords always count; the declared language's are added when qg knows them.
    """
    chosen = [*words["en"], *words.get(language, ())]
    alternatives = "|".join(re.escape(fold(word)) for word in chosen)
    return re.compile(rf"(?:{alternatives}):")


def walk(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Returns every scenario of one feature file as (name, tags), in file order.

    The tags are every `@tag` of the lines above the feature line (they apply to every scenario
    of the file) followed by those above the scenario or outline itself. The keywords are those
    of the file's `# language:` header (English and Spanish are known), English by default.
    """
    language = next((m.group(1).lower() for m in map(LANGUAGE.match, text.splitlines()) if m), "en")
    feature_line = keyword_pattern(FEATURE_WORDS, language.split("-")[0])
    scenario_line = keyword_pattern(SCENARIO_WORDS, language.split("-")[0])
    found: list[tuple[str, tuple[str, ...]]] = []
    pending: list[str] = []
    inherited: list[str] = []
    in_feature = False
    for raw in text.splitlines():
        line = raw.strip()
        folded = fold(line)
        if line.startswith("@"):
            pending += re.findall(r"@[^\s@]+", line)
        elif feature_line.match(folded):
            inherited, pending, in_feature = pending, [], True
        elif scenario_line.match(folded):
            name = line.split(":", 1)[1].strip()
            found.append((name, tuple(dict.fromkeys([*inherited, *pending]))))
            pending = []
        elif line and not line.startswith("#") and in_feature:
            pending = []
    return found


def parse(text: str, file: str, pattern: re.Pattern[str]) -> dict[str, list[Covering]]:
    """Returns, per AC id, the scenarios of one feature file tagged with it."""
    found: dict[str, list[Covering]] = {}
    for name, tags in walk(text):
        for ident in dict.fromkeys(m.upper() for m in pattern.findall(" ".join(tags))):
            found.setdefault(ident, []).append(Covering(file, name))
    return found


def scan(repo: str, sha: str, prd: str, phase: str) -> dict[str, list[Covering]]:
    """Reads every feature file of the repo at `sha` and returns the covered AC ids."""
    listed = git.out(repo, "ls-tree", "-r", "--name-only", sha, "--", FEATURES_DIR)
    pattern = tag_pattern(prd, phase)
    found: dict[str, list[Covering]] = {}
    for file in (name for name in listed.splitlines() if name.endswith(".feature")):
        text = git.out(repo, "show", f"{sha}:{file}")
        for ident, tests in parse(text, file, pattern).items():
            found.setdefault(ident, []).extend(tests)
    return found


def slug(text: str) -> str:
    """Returns `text` folded into words joined by `_`, the way a test function is named."""
    return re.sub(r"\W+", "_", fold(text)).strip("_")


def spec_texts(repo: str, sha: str, specs: list[str]) -> dict[str, str]:
    """Returns the content at `sha` of the selected spec files that exist there."""
    texts = {spec: git.show(repo, sha, spec) for spec in specs}
    return {spec: text for spec, text in texts.items() if text is not None}


def running_specs(feature: str, texts: dict[str, str], specs: list[str]) -> list[str] | None:
    """Returns the selected spec files that run `feature`, None when the selection leaves it out.

    A spec runs a feature when it is the feature file itself or its text names the feature file
    (`scenarios("features/x.feature")`). No selected specs at all means every spec was selected,
    so every feature counts as run, with no spec to name.
    """
    if not specs:
        return []
    name = feature.rsplit("/", 1)[-1]
    found = [spec for spec in specs if spec == feature or name in texts.get(spec, "")]
    return found or None


def _scenario_status(test: Covering, failures: list[str], specs: list[str]) -> str:
    """Returns OK or FAILED for a scenario of a run feature, from the stage's failure lines.

    A failure names the scenario when its line holds the scenario's name or its test-function
    slug; it names the whole feature when it holds the feature file, or a spec that runs it
    with no test name after `::`.
    """
    folded = [fold(line) for line in failures]
    slugs = [slug(line) for line in failures]
    wanted = slug(test.scenario)
    named = any(wanted in text for text in slugs) or any(
        fold(test.scenario) in text or fold(test.file.rsplit("/", 1)[-1]) in text for text in folded
    )
    whole = any(spec in line and "::" not in line for line in failures for spec in specs)
    return FAILED if named or whole else OK


def status_of(
    tests: list[Covering],
    stage_status: str | None,
    failures: list[str],
    selection: Callable[[str], list[str] | None] = lambda feature: [],
) -> str:
    """Returns an AC's result from the Playwright stage and the selection it ran with.

    `selection` maps a feature file to the selected specs that run it (see `running_specs`).
    Without a scenario the AC is untested. When the stage did not run, or the scenario's file was
    outside the selection, it is not run. A stage that ran proves a scenario passed unless a failure
    line names it; a stage that failed with no failure line at all (a crash, a timeout) proves
    nothing. Over several scenarios a failure wins, then unknown, then not run.
    """
    if not tests:
        return UNTESTED
    if stage_status not in ("passed", "failed"):
        return NOT_RUN
    if stage_status == "failed" and not failures:
        return UNKNOWN
    results = []
    for test in tests:
        specs = selection(test.file)
        results.append(NOT_RUN if specs is None else _scenario_status(test, failures, specs))
    for state in (FAILED, UNKNOWN, NOT_RUN):
        if state in results:
            return state
    return OK


def criteria(
    covered: dict[str, list[Covering]],
    stage_status: str | None,
    failures: list[str],
    expected: list[str],
    selection: Callable[[str], list[str] | None] = lambda feature: [],
) -> list[Criterion]:
    """Returns every AC of the phase, in id order: the tagged ones plus the `expected` ids."""
    ids = sorted({*covered, *(e.upper() for e in expected)})
    return [
        Criterion(
            i, tuple(covered.get(i, ())), status_of(covered.get(i, []), stage_status, failures)
        )
        for i in ids
    ]
