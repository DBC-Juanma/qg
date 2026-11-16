"""verify.yml: the closed schema, its validation and the resolved configuration of a run."""

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

import yaml

from qg import git
from qg.util import die

VERIFY_PATH = ".claude/verify.yml"
PROTECTED_REFS = ("refs/heads/master", "refs/remotes/origin/master")
STAGES = ("simplify", "static", "backend", "frontend", "playwright", "qa")
ENVS = ("prod", "staging")
COMMAND_STAGES = ("static", "backend", "frontend")
LANES = {"static": ("static",), "tests": ("backend", "frontend", "playwright"), "qa": ("qa",)}
DEFAULT_STAGES = tuple(name for name in STAGES if name != "simplify")
DEFAULT_LANE_LIMITS = {
    "setup": ("1600%", "8G"),
    "static": ("200%", "2G"),
    "tests": ("1000%", "11G"),
    "qa_stack": ("500%", "5G"),
    "qa_agent": ("200%", "2G"),
}
DEFAULT_TYPICAL_MINUTES = {
    "setup": 3,
    "static": 3,
    "backend": 6,
    "frontend": 3,
    "playwright": 6,
    "qa": 12,
}
QA_BOOT_MINUTES = 3
HUNT_RERUNS = 3
HUNT_RERUN_MINUTES = 10
QA_BOOT_MINUTES_CAP = 13
TIMEOUT_KEYS = (*STAGES, "setup", "teardown")
DEFAULT_TIMEOUTS = {
    "simplify": 30,
    "static": 5,
    "backend": 15,
    "frontend": 8,
    "playwright": 15,
    "qa": 30,
    "setup": 30,
    "teardown": 5,
}
DEFAULT_MAX_COST_USD = 15
DEFAULT_SIMPLIFY_TURNS = 150
DEFAULT_SIMPLIFY_COST_USD = 10
DEFAULT_SIMPLIFY_PATHS = (
    "src/",
    "frontend/src/",
    "frontend-widget/src/",
    "frontend-app/src/",
    "frontend-booking/src/",
)
DEADLINE_MARGIN_MINUTES = 10
VERIFY_MINUTES = 15
DEFAULT_PROBE_MAX_P0 = 5
DEFAULT_PROBE_COST_USD = 3
DEFAULT_PROBE_MINUTES = 40
DEFAULT_MUTANTS = 12
DEFAULT_MUTANTS_MINUTES = 20
AUDIT_LENSES = ("security",)
DEFAULT_AUDIT_MINUTES = 240
DEFAULT_CPU_QUOTA = "100%"
DEFAULT_MEMORY_MAX = "1G"
CPU_QUOTA = re.compile(r"^\d+%$")
MEMORY_MAX = re.compile(r"^(\d+[KMGT]?|infinity)$")
SIZE_UNITS = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
PROFILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys, merge keys, aliases, anchors and explicit tags."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Rejects aliases, anchors and explicit tags before the node is composed."""
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent):
            raise yaml.YAMLError(f"YAML aliases are not allowed (line {event.start_mark.line + 1})")
        if getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError(f"YAML anchors are not allowed (line {event.start_mark.line + 1})")
        if getattr(event, "tag", None) is not None:
            raise yaml.YAMLError(
                f"explicit YAML tags are not allowed (line {event.start_mark.line + 1})"
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        """Builds a mapping, refusing a repeated key and the `<<` merge key."""
        if not isinstance(node, yaml.MappingNode):
            raise yaml.YAMLError("expected a mapping")
        found: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key == "<<":
                raise yaml.YAMLError(
                    f"merge key '<<' is not allowed (line {key_node.start_mark.line + 1})"
                )
            if key in found:
                raise yaml.YAMLError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
            found[key] = self.construct_object(value_node, deep=deep)
        return found


def load_yaml(text: str, source: str) -> Any:
    """Parses `text` strictly (see StrictLoader); exits naming `source` on any error."""
    try:
        return yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        die(f"{source}: {exc}")


def memory_bytes(size: str) -> int | None:
    """Returns a systemd memory size such as 11G in bytes; None for `infinity`."""
    if size == "infinity":
        return None
    return int(size.rstrip("KMGT") or 0) * SIZE_UNITS[size[-1] if size[-1] in "KMGT" else ""]


def split_limit(limit: tuple[str, str], parts: int) -> tuple[str, str]:
    """Returns one part's share of a lane's (cpu_quota, memory_max): both divided by `parts`."""
    cpu, memory = limit
    share = int(cpu.rstrip("%")) // parts
    size = memory_bytes(memory)
    return f"{max(share, 1)}%", "infinity" if size is None else str(size // parts)


@dataclass(frozen=True)
class Deps:
    """How the sandbox installs dependencies."""

    python_sync: str
    node_packages: tuple[str, ...]
    node_install: str


@dataclass(frozen=True)
class Postgres:
    """The throwaway Postgres container of a run."""

    image: str
    db: str


@dataclass(frozen=True)
class Resources:
    """systemd limits: the executor's own scope and one scope per lane of work.

    The lanes run side by side, so their limits together are the machine budget of a run
    (defaults: 20 cores and 16 GB across the executor, static, tests, qa_stack and qa_agent).
    """

    cpu_quota: str
    memory_max: str
    lanes: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_LANE_LIMITS))


@dataclass(frozen=True)
class Playwright:
    """Stage S4: critical specs, the surface map file and the run command."""

    critical: tuple[str, ...]
    surface_map: str | None
    run: str
    shards: int = 1


@dataclass(frozen=True)
class Account:
    """A login the QA agent may use."""

    role: str
    email: str
    password: str
    name: str = ""


@dataclass(frozen=True)
class QaSurface:
    """An extra page of the QA stack the agent may visit: a name, a URL and how to use it."""

    name: str
    url: str
    notes: str = ""


@dataclass(frozen=True)
class Qa:
    """Stage S5: how to boot the stack and what the QA agent may use."""

    boot: str
    ready_url: str
    app_url: str
    api_url: str
    secrets_env_file: str | None
    secrets_keys: tuple[str, ...]
    accounts: tuple[Account, ...]
    time_limit_minutes: int
    max_turns: int
    notes_file: str | None
    max_cost_usd: float
    ready_marker: str | None = None
    surfaces: tuple[QaSurface, ...] = ()


@dataclass(frozen=True)
class MapEntry:
    """Changed paths (globs) and the pytest paths they need."""

    paths: tuple[str, ...]
    tests: tuple[str, ...]


@dataclass(frozen=True)
class BackendTests:
    """Affected pytest selection: the command, the always list, the map and the whole-suite set."""

    command: str
    always: tuple[str, ...]
    map: tuple[MapEntry, ...]
    all_tests: tuple[str, ...]
    whole_when: tuple[str, ...]


@dataclass(frozen=True)
class FrontendPackage:
    """Affected vitest selection of one package: related and whole-suite commands."""

    package: str
    related: str
    all_command: str
    whole_when: tuple[str, ...]
    shared: tuple[str, ...] = ()


@dataclass(frozen=True)
class Affected:
    """The protected mapping from a change to the tests it needs; empty means whole stages."""

    backend: BackendTests | None = None
    frontend: tuple[FrontendPackage, ...] = ()


@dataclass(frozen=True)
class Simplify:
    """Stage S0: the limits of the /simplify agent; the stage needs no other configuration."""

    time_limit_minutes: int = 25
    max_turns: int = DEFAULT_SIMPLIFY_TURNS
    max_cost_usd: float = DEFAULT_SIMPLIFY_COST_USD
    paths: tuple[str, ...] = DEFAULT_SIMPLIFY_PATHS


@dataclass(frozen=True)
class Probe:
    """The P0 probe: where its agent writes the pytest reproduction, and its limits.

    `max_cost_usd` is per P0 and agent, across all its attempts; `time_limit_minutes` is the
    whole probe pass of one run; at most `max_p0` P0 get a probe.
    """

    dir: str
    max_p0: int = DEFAULT_PROBE_MAX_P0
    max_cost_usd: float = DEFAULT_PROBE_COST_USD
    time_limit_minutes: int = DEFAULT_PROBE_MINUTES


@dataclass(frozen=True)
class Mutants:
    """The mutant hunt after a passing backend stage: at most `max` mutants of the lines the
    change adds, within `time_limit_minutes`. It only reports; it never fails the stage."""

    max: int = DEFAULT_MUTANTS
    time_limit_minutes: int = DEFAULT_MUTANTS_MINUTES


@dataclass(frozen=True)
class Audit:
    """The nightly audit inside S0: the lenses it runs before the simplify agent, commit by commit
    and uncapped, and the minutes after which a hung audit fails."""

    lenses: tuple[str, ...] = AUDIT_LENSES
    time_limit_minutes: int = DEFAULT_AUDIT_MINUTES


@dataclass(frozen=True)
class Rules:
    """The architecture rules of the static stage: a bundled profile and this repo's paths."""

    profile: str
    layers: dict[str, tuple[str, ...]]
    tests: tuple[str, ...] = ()
    markdown_allow: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    enforce: bool = True


@dataclass(frozen=True)
class Policy:
    """What an environment demands of a run: stages and the findings it tolerates.

    `p1_acknowledged` is still read, since frozen configurations of old runs carry it, but it
    gates nothing on its own: every environment blocks on an open P1 the operator has not
    acknowledged (`report.blocking`).
    """

    stages: tuple[str, ...]
    max_p0: int
    p1_acknowledged: bool


@dataclass(frozen=True)
class Config:
    """A validated verify.yml."""

    deps: Deps
    postgres: Postgres
    resources: Resources
    timeouts: dict[str, int]
    command_stages: dict[str, tuple[str, ...]]
    playwright: Playwright | None
    qa: Qa | None
    deploy: dict[str, Policy]
    teardown: tuple[str, ...] = ()
    simplify: Simplify = Simplify()
    affected: Affected = Affected()
    typical_minutes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TYPICAL_MINUTES))
    probe: Probe | None = None
    mutants: Mutants | None = None
    audit: Audit | None = None
    rules: Rules | None = None

    def configured(self, stage: str) -> bool:
        """Tells whether verify.yml configures `stage`; simplify needs no configuration."""
        if stage == "simplify":
            return True
        if stage in COMMAND_STAGES:
            return stage in self.command_stages
        if stage == "playwright":
            return self.playwright is not None
        return self.qa is not None

    def commands(self, stage: str) -> tuple[str, ...]:
        """Returns the commands of one of the command stages (static, backend, frontend)."""
        return self.command_stages[stage]

    def policy(self, env: str) -> Policy:
        """Returns the policy of `env`, exiting when verify.yml does not define it."""
        if env not in self.deploy:
            die(f"{VERIFY_PATH}: deploy.{env} is not defined")
        return self.deploy[env]

    def stage_minutes(self, stage: str, qa_scale: float = 1.0) -> int:
        """Returns a stage's timeout in minutes; the qa stage grows with the plan's scale and
        simplify with the audit it runs first."""
        if stage == "simplify" and self.audit is not None:
            return self.timeouts[stage] + self.audit.time_limit_minutes
        if stage == "qa" and self.qa is not None:
            agent = math.ceil(round(self.qa.time_limit_minutes * qa_scale, 6))
            return max(self.timeouts["qa"], agent + QA_BOOT_MINUTES_CAP)
        return self.timeouts[stage]

    def qa_scaled(self, qa_scale: float) -> Qa:
        """Returns the qa section with its time limit and turn cap grown by the plan's scale."""
        assert self.qa is not None
        return replace(
            self.qa,
            time_limit_minutes=math.ceil(round(self.qa.time_limit_minutes * qa_scale, 6)),
            max_turns=math.ceil(round(self.qa.max_turns * qa_scale, 6)),
        )

    def lane_stages(self, stage_set: list[str]) -> dict[str, list[str]]:
        """Returns the stages of `stage_set` per lane, in order; empty lanes are left out."""
        lanes = {lane: [n for n in names if n in stage_set] for lane, names in LANES.items()}
        return {lane: names for lane, names in lanes.items() if names}

    def lane_minutes(self, names: list[str], qa_scale: float = 1.0) -> int:
        """Returns the worst case of one lane: its stages one after the other, at their timeouts."""
        return sum(self.stage_minutes(name, qa_scale) for name in names)

    def deadline_minutes(self, stage_set: list[str], qa_scale: float = 1.0, extra: int = 0) -> int:
        """Returns the run's overall budget: setup, the slowest lane, teardown and a margin.

        The QA lane runs after the tests lane, so the two count as one. Simplify, when asked
        for, runs alone before the lanes. `extra` is what a release adds
        (see `extra_minutes`); a run with QA adds the P0 verifier's VERIFY_MINUTES and, when
        verify.yml configures `probe`, the probe pass's time limit; a run with the backend stage
        adds the mutant hunt's time limit when verify.yml configures `mutants`.
        """
        lanes = self.lane_stages([n for n in stage_set if n != "simplify"])
        chain = lanes.pop("tests", []) + lanes.pop("qa", [])
        slowest = max(
            (self.lane_minutes(names, qa_scale) for names in (chain, *lanes.values())), default=0
        )
        alone = self.stage_minutes("simplify") if "simplify" in stage_set else 0
        minutes = alone + slowest + self.timeouts["setup"] + self.timeouts["teardown"]
        verify = VERIFY_MINUTES if "qa" in stage_set else 0
        if "qa" in stage_set and self.probe is not None:
            verify += self.probe.time_limit_minutes
        if "backend" in stage_set and self.mutants is not None:
            verify += self.mutants.time_limit_minutes
        return minutes + extra + verify + DEADLINE_MARGIN_MINUTES

    def extra_minutes(self, mode: str, stress: int) -> int:
        """Returns the time a run adds to its deadline: extra stress passes, or a release's hunt."""
        if stress > 1:
            return (stress - 1) * self.timeouts["playwright"]
        return HUNT_RERUNS * HUNT_RERUN_MINUTES if mode == "release" else 0

    def stage_set(self, env: str, qa: bool, simplify: bool = False) -> tuple[str, ...]:
        """Returns the stages a run for `env` executes, in STAGES order.

        qa is left out when it is off; simplify only when asked for (`--simplify`): it is no
        longer part of the gate, and a policy that still lists it does not bring it back.
        """
        wanted = set(self.policy(env).stages) - {"simplify"}
        if simplify:
            wanted.add("simplify")
        return tuple(s for s in STAGES if s in wanted and (qa or s != "qa"))


class Reader:
    """Type-checking accessors that exit with a message naming the key path of a mistake."""

    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, path: str, message: str) -> NoReturn:
        """Exits with `message` about the key at `path`."""
        die(f"{self.source}: {path}: {message}")

    def mapping(
        self,
        value: Any,
        path: str,
        required: tuple[str, ...] = (),
        optional: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Checks `value` is a mapping with no unknown key and every required key present."""
        if not isinstance(value, dict):
            self.fail(path, "expected a mapping")
        allowed = (*required, *optional)
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            self.fail(
                path,
                f"unknown key(s) {', '.join(repr(k) for k in unknown)}; "
                f"allowed: {', '.join(allowed)}",
            )
        missing = [key for key in required if key not in value]
        if missing:
            self.fail(path, f"missing required key(s) {', '.join(missing)}")
        return value

    def text(self, value: Any, path: str) -> str:
        """Checks `value` is a non-empty string."""
        if not isinstance(value, str) or not value.strip():
            self.fail(path, "expected a non-empty string")
        return value

    def texts(self, value: Any, path: str) -> tuple[str, ...]:
        """Checks `value` is a list of non-empty strings."""
        if not isinstance(value, list):
            self.fail(path, "expected a list of strings")
        return tuple(self.text(item, f"{path}[{index}]") for index, item in enumerate(value))

    def integer(self, value: Any, path: str, minimum: int) -> int:
        """Checks `value` is an integer of at least `minimum`."""
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            self.fail(path, f"expected an integer >= {minimum}")
        return value

    def flag(self, value: Any, path: str) -> bool:
        """Checks `value` is a boolean."""
        if not isinstance(value, bool):
            self.fail(path, "expected true or false")
        return value


def _qa(reader: Reader, raw: Any) -> Qa:
    """Validates stages.qa."""
    section = reader.mapping(
        raw,
        "stages.qa",
        required=("boot", "ready_url", "app_url", "api_url"),
        optional=(
            "secrets_env_file",
            "secrets_keys",
            "accounts",
            "time_limit_minutes",
            "max_turns",
            "notes_file",
            "max_cost_usd",
            "ready_marker",
            "surfaces",
        ),
    )
    accounts = section.get("accounts", [])
    if not isinstance(accounts, list):
        reader.fail("stages.qa.accounts", "expected a list of {role, name, email, password}")
    parsed = []
    for index, item in enumerate(accounts):
        path = f"stages.qa.accounts[{index}]"
        entry = reader.mapping(
            item, path, required=("role", "email", "password"), optional=("name",)
        )
        parsed.append(
            Account(
                role=reader.text(entry["role"], f"{path}.role"),
                email=reader.text(entry["email"], f"{path}.email"),
                password=reader.text(entry["password"], f"{path}.password"),
                name=reader.text(entry["name"], f"{path}.name") if "name" in entry else "",
            )
        )
    env_file = section.get("secrets_env_file")
    notes = section.get("notes_file")
    keys = reader.texts(section.get("secrets_keys", []), "stages.qa.secrets_keys")
    if keys and env_file is None:
        reader.fail("stages.qa.secrets_keys", "needs stages.qa.secrets_env_file")
    cap = section.get("max_cost_usd", DEFAULT_MAX_COST_USD)
    if isinstance(cap, bool) or not isinstance(cap, int | float) or cap <= 0:
        reader.fail("stages.qa.max_cost_usd", "expected a number of dollars > 0")
    marker = section.get("ready_marker")
    raw_surfaces = section.get("surfaces", [])
    if not isinstance(raw_surfaces, list):
        reader.fail("stages.qa.surfaces", "expected a list of {name, url, notes}")
    surfaces = []
    for index, item in enumerate(raw_surfaces):
        where = f"stages.qa.surfaces[{index}]"
        entry = reader.mapping(item, where, required=("name", "url"), optional=("notes",))
        surfaces.append(
            QaSurface(
                name=reader.text(entry["name"], f"{where}.name"),
                url=reader.text(entry["url"], f"{where}.url"),
                notes=reader.text(entry["notes"], f"{where}.notes") if "notes" in entry else "",
            )
        )
    return Qa(
        boot=reader.text(section["boot"], "stages.qa.boot"),
        ready_url=reader.text(section["ready_url"], "stages.qa.ready_url"),
        app_url=reader.text(section["app_url"], "stages.qa.app_url"),
        api_url=reader.text(section["api_url"], "stages.qa.api_url"),
        secrets_env_file=None
        if env_file is None
        else reader.text(env_file, "stages.qa.secrets_env_file"),
        secrets_keys=keys,
        accounts=tuple(parsed),
        time_limit_minutes=reader.integer(
            section.get("time_limit_minutes", 15), "stages.qa.time_limit_minutes", 1
        ),
        max_turns=reader.integer(section.get("max_turns", 150), "stages.qa.max_turns", 1),
        notes_file=None if notes is None else reader.text(notes, "stages.qa.notes_file"),
        max_cost_usd=float(cap),
        ready_marker=None if marker is None else reader.text(marker, "stages.qa.ready_marker"),
        surfaces=tuple(surfaces),
    )


def _simplify(reader: Reader, raw: Any) -> Simplify:
    """Validates stages.simplify."""
    section = reader.mapping(
        raw,
        "stages.simplify",
        optional=("time_limit_minutes", "max_turns", "max_cost_usd", "paths"),
    )
    source_dirs = reader.texts(
        section.get("paths", list(DEFAULT_SIMPLIFY_PATHS)), "stages.simplify.paths"
    )
    for name in source_dirs:
        if not name.endswith("/") or name.startswith(("/", ".")):
            reader.fail(
                "stages.simplify.paths", f"{name!r} must be a relative directory ending in /"
            )
    cap = section.get("max_cost_usd", DEFAULT_SIMPLIFY_COST_USD)
    if isinstance(cap, bool) or not isinstance(cap, int | float) or cap <= 0:
        reader.fail("stages.simplify.max_cost_usd", "expected a number of dollars > 0")
    return Simplify(
        time_limit_minutes=reader.integer(
            section.get("time_limit_minutes", 25), "stages.simplify.time_limit_minutes", 1
        ),
        max_turns=reader.integer(
            section.get("max_turns", DEFAULT_SIMPLIFY_TURNS), "stages.simplify.max_turns", 1
        ),
        max_cost_usd=float(cap),
        paths=source_dirs,
    )


def _playwright(reader: Reader, raw: Any) -> Playwright:
    """Validates stages.playwright."""
    section = reader.mapping(
        raw, "stages.playwright", required=("run",), optional=("critical", "surface_map", "shards")
    )
    run = reader.text(section["run"], "stages.playwright.run")
    if "{specs}" not in run:
        reader.fail("stages.playwright.run", "must contain the {specs} placeholder")
    surface = section.get("surface_map")
    return Playwright(
        critical=reader.texts(section.get("critical", []), "stages.playwright.critical"),
        surface_map=None
        if surface is None
        else reader.text(surface, "stages.playwright.surface_map"),
        run=run,
        shards=reader.integer(section.get("shards", 1), "stages.playwright.shards", 1),
    )


def _affected(reader: Reader, raw: Any) -> Affected:
    """Validates the top-level `affected` section."""
    section = reader.mapping(raw, "affected", optional=("backend", "frontend"))
    backend = None
    if "backend" in section:
        body = reader.mapping(
            section["backend"],
            "affected.backend",
            required=("command", "all"),
            optional=("always", "map", "whole_when"),
        )
        command = reader.text(body["command"], "affected.backend.command")
        if "{tests}" not in command:
            reader.fail("affected.backend.command", "must contain the {tests} placeholder")
        entries = body.get("map", [])
        if not isinstance(entries, list):
            reader.fail("affected.backend.map", "expected a list of {paths, tests}")
        mapped = []
        for index, item in enumerate(entries):
            where = f"affected.backend.map[{index}]"
            entry = reader.mapping(item, where, required=("paths", "tests"))
            mapped.append(
                MapEntry(
                    reader.texts(entry["paths"], f"{where}.paths"),
                    reader.texts(entry["tests"], f"{where}.tests"),
                )
            )
        backend = BackendTests(
            command=command,
            always=reader.texts(body.get("always", []), "affected.backend.always"),
            map=tuple(mapped),
            all_tests=reader.texts(body["all"], "affected.backend.all"),
            whole_when=reader.texts(body.get("whole_when", []), "affected.backend.whole_when"),
        )
    frontend = []
    raw_packages = section.get("frontend", [])
    if not isinstance(raw_packages, list):
        reader.fail("affected.frontend", "expected a list of packages")
    for index, item in enumerate(raw_packages):
        where = f"affected.frontend[{index}]"
        body = reader.mapping(
            item,
            where,
            required=("package", "related", "all"),
            optional=("whole_when", "shared"),
        )
        related = reader.text(body["related"], f"{where}.related")
        if "{files}" not in related:
            reader.fail(f"{where}.related", "must contain the {files} placeholder")
        frontend.append(
            FrontendPackage(
                package=reader.text(body["package"], f"{where}.package"),
                related=related,
                all_command=reader.text(body["all"], f"{where}.all"),
                whole_when=reader.texts(body.get("whole_when", []), f"{where}.whole_when"),
                shared=reader.texts(body.get("shared", []), f"{where}.shared"),
            )
        )
    return Affected(backend, tuple(frontend))


def _probe(reader: Reader, raw: Any, affected: Affected) -> Probe:
    """Validates the top-level `probe` section; it needs `affected.backend` to run a probe."""
    section = reader.mapping(
        raw,
        "probe",
        required=("dir",),
        optional=("max_p0", "max_cost_usd", "time_limit_minutes"),
    )
    if affected.backend is None:
        reader.fail("probe", "needs affected.backend: its command runs the probe")
    directory = reader.text(section["dir"], "probe.dir")
    if not directory.endswith("/") or directory.startswith(("/", ".")) or ".." in directory:
        reader.fail("probe.dir", f"{directory!r} must be a relative directory ending in /")
    cap = section.get("max_cost_usd", DEFAULT_PROBE_COST_USD)
    if isinstance(cap, bool) or not isinstance(cap, int | float) or cap <= 0:
        reader.fail("probe.max_cost_usd", "expected a number of dollars > 0")
    return Probe(
        dir=directory,
        max_p0=reader.integer(section.get("max_p0", DEFAULT_PROBE_MAX_P0), "probe.max_p0", 1),
        max_cost_usd=float(cap),
        time_limit_minutes=reader.integer(
            section.get("time_limit_minutes", DEFAULT_PROBE_MINUTES),
            "probe.time_limit_minutes",
            1,
        ),
    )


def _mutants(reader: Reader, raw: Any, affected: Affected) -> Mutants:
    """Validates the top-level `mutants` section; it needs `affected.backend` to pick the tests."""
    section = reader.mapping(raw, "mutants", required=(), optional=("max", "time_limit_minutes"))
    if affected.backend is None:
        reader.fail("mutants", "needs affected.backend: it picks the tests each mutant runs")
    return Mutants(
        max=reader.integer(section.get("max", DEFAULT_MUTANTS), "mutants.max", 1),
        time_limit_minutes=reader.integer(
            section.get("time_limit_minutes", DEFAULT_MUTANTS_MINUTES),
            "mutants.time_limit_minutes",
            1,
        ),
    )


def _audit(reader: Reader, raw: Any) -> Audit:
    """Validates the top-level `audit` section."""
    section = reader.mapping(raw, "audit", required=(), optional=("lenses", "time_limit_minutes"))
    lenses = reader.texts(section.get("lenses", list(AUDIT_LENSES)), "audit.lenses")
    unknown = [name for name in lenses if name not in AUDIT_LENSES]
    if not lenses or unknown:
        reader.fail("audit.lenses", f"expected some of {', '.join(AUDIT_LENSES)}")
    return Audit(
        lenses=lenses,
        time_limit_minutes=reader.integer(
            section.get("time_limit_minutes", DEFAULT_AUDIT_MINUTES), "audit.time_limit_minutes", 1
        ),
    )


def _rules(reader: Reader, raw: Any) -> Rules:
    """Validates the top-level `rules` section; the profile checks its names when it runs."""
    section = reader.mapping(
        raw,
        "rules",
        required=("profile", "layers"),
        optional=("tests", "markdown_allow", "disable", "enforce"),
    )
    profile = reader.text(section["profile"], "rules.profile")
    if not PROFILE.match(profile):
        reader.fail("rules.profile", f"{profile!r} is not a profile name")
    layers = section["layers"]
    if not isinstance(layers, dict) or not layers:
        reader.fail("rules.layers", "expected a mapping of layer names to lists of path globs")
    return Rules(
        profile=profile,
        layers={
            reader.text(name, "rules.layers"): reader.texts(globs, f"rules.layers.{name}")
            for name, globs in layers.items()
        },
        tests=reader.texts(section.get("tests", []), "rules.tests"),
        markdown_allow=reader.texts(section.get("markdown_allow", []), "rules.markdown_allow"),
        disable=reader.texts(section.get("disable", []), "rules.disable"),
        enforce=reader.flag(section.get("enforce", True), "rules.enforce"),
    )


def parse_rules(raw: Any, source: str) -> Rules:
    """Validates a document holding only a top-level `rules` mapping; returns its Rules."""
    reader = Reader(source)
    return _rules(reader, reader.mapping(raw, "<root>", required=("rules",))["rules"])


def _policy(reader: Reader, env: str, raw: Any) -> Policy:
    """Validates deploy.<env>."""
    path = f"deploy.{env}"
    section = reader.mapping(
        raw, path, required=("stages",), optional=("max_p0", "p1_acknowledged")
    )
    names = tuple(n for n in reader.texts(section["stages"], f"{path}.stages") if n != "simplify")
    if not names:
        reader.fail(f"{path}.stages", "must list at least one stage")
    for name in names:
        if name not in STAGES:
            reader.fail(f"{path}.stages", f"unknown stage {name!r}; allowed: {', '.join(STAGES)}")
    if len(set(names)) != len(names):
        reader.fail(f"{path}.stages", "lists a stage twice")
    return Policy(
        stages=names,
        max_p0=reader.integer(section.get("max_p0", 0), f"{path}.max_p0", 0),
        p1_acknowledged=reader.flag(
            section.get("p1_acknowledged", False), f"{path}.p1_acknowledged"
        ),
    )


def parse(raw: Any, source: str) -> Config:
    """Validates a loaded verify.yml and returns its Config; `source` names it in errors."""
    reader = Reader(source)
    top = reader.mapping(
        raw,
        "<root>",
        required=("version", "deps", "postgres", "stages", "deploy"),
        optional=(
            "resources",
            "timeouts_minutes",
            "teardown",
            "affected",
            "typical_minutes",
            "probe",
            "mutants",
            "audit",
            "rules",
        ),
    )
    if top["version"] != 1:
        reader.fail("version", "must be 1")

    deps_raw = reader.mapping(
        top["deps"], "deps", required=("python_sync",), optional=("node_packages", "node_install")
    )
    packages = reader.texts(deps_raw.get("node_packages", []), "deps.node_packages")
    if packages and "node_install" not in deps_raw:
        reader.fail("deps", "node_install is required when node_packages is not empty")
    deps = Deps(
        python_sync=reader.text(deps_raw["python_sync"], "deps.python_sync"),
        node_packages=packages,
        node_install=reader.text(deps_raw["node_install"], "deps.node_install") if packages else "",
    )

    pg_raw = reader.mapping(top["postgres"], "postgres", required=("image", "db"))
    postgres = Postgres(
        image=reader.text(pg_raw["image"], "postgres.image"),
        db=reader.text(pg_raw["db"], "postgres.db"),
    )

    res_raw = reader.mapping(
        top.get("resources", {}), "resources", optional=("cpu_quota", "memory_max", "lanes")
    )
    lanes_raw = reader.mapping(
        res_raw.get("lanes", {}), "resources.lanes", optional=tuple(DEFAULT_LANE_LIMITS)
    )
    lanes = dict(DEFAULT_LANE_LIMITS)
    for lane, body in lanes_raw.items():
        got = reader.mapping(body, f"resources.lanes.{lane}", optional=("cpu_quota", "memory_max"))
        cpu_l = reader.text(
            got.get("cpu_quota", lanes[lane][0]), f"resources.lanes.{lane}.cpu_quota"
        )
        mem_l = reader.text(
            got.get("memory_max", lanes[lane][1]), f"resources.lanes.{lane}.memory_max"
        )
        if not CPU_QUOTA.match(cpu_l) or not MEMORY_MAX.match(mem_l):
            reader.fail(
                f"resources.lanes.{lane}", "expected cpu_quota like 400% and memory_max like 4G"
            )
        lanes[lane] = (cpu_l, mem_l)
    cpu = reader.text(res_raw.get("cpu_quota", DEFAULT_CPU_QUOTA), "resources.cpu_quota")
    memory = reader.text(res_raw.get("memory_max", DEFAULT_MEMORY_MAX), "resources.memory_max")
    if not CPU_QUOTA.match(cpu):
        reader.fail("resources.cpu_quota", "expected a percentage such as 1600%")
    if not MEMORY_MAX.match(memory):
        reader.fail("resources.memory_max", "expected a size such as 12G, or infinity")

    timeouts_raw = reader.mapping(
        top.get("timeouts_minutes", {}), "timeouts_minutes", optional=TIMEOUT_KEYS
    )
    timeouts = dict(DEFAULT_TIMEOUTS)
    for key, value in timeouts_raw.items():
        timeouts[key] = reader.integer(value, f"timeouts_minutes.{key}", 1)

    typical_raw = reader.mapping(
        top.get("typical_minutes", {}), "typical_minutes", optional=tuple(DEFAULT_TYPICAL_MINUTES)
    )
    typical = dict(DEFAULT_TYPICAL_MINUTES)
    for key, value in typical_raw.items():
        typical[key] = reader.integer(value, f"typical_minutes.{key}", 1)

    stages_raw = reader.mapping(top["stages"], "stages", optional=STAGES)
    command_stages = {
        name: reader.texts(stages_raw[name], f"stages.{name}")
        for name in COMMAND_STAGES
        if name in stages_raw
    }
    playwright = (
        _playwright(reader, stages_raw["playwright"]) if "playwright" in stages_raw else None
    )
    qa = _qa(reader, stages_raw["qa"]) if "qa" in stages_raw else None
    simplify = _simplify(reader, stages_raw["simplify"]) if "simplify" in stages_raw else Simplify()

    deploy_raw = reader.mapping(top["deploy"], "deploy", optional=ENVS)
    if not deploy_raw:
        reader.fail("deploy", "define at least one of prod, staging")
    deploy = {env: _policy(reader, env, body) for env, body in deploy_raw.items()}

    affected = _affected(reader, top["affected"]) if "affected" in top else Affected()
    config = Config(
        deps=deps,
        postgres=postgres,
        resources=Resources(cpu_quota=cpu, memory_max=memory, lanes=lanes),
        timeouts=timeouts,
        command_stages=command_stages,
        playwright=playwright,
        qa=qa,
        deploy=deploy,
        teardown=reader.texts(top.get("teardown", []), "teardown"),
        simplify=simplify,
        affected=affected,
        typical_minutes=typical,
        probe=_probe(reader, top["probe"], affected) if "probe" in top else None,
        mutants=_mutants(reader, top["mutants"], affected) if "mutants" in top else None,
        audit=_audit(reader, top["audit"]) if "audit" in top else None,
        rules=_rules(reader, top["rules"]) if "rules" in top else None,
    )
    for env, policy in deploy.items():
        for stage in policy.stages:
            if not config.configured(stage):
                reader.fail(
                    f"deploy.{env}.stages",
                    f"lists {stage!r} but stages.{stage} is not configured",
                )
    return config


@dataclass(frozen=True)
class Loaded:
    """The protected verify.yml of a repository: document, config and where it came from."""

    raw: dict[str, Any]
    config: Config
    ref: str
    ref_sha: str
    branch_differs: bool


def protected_ref(repo: str) -> str:
    """Returns the local master when it exists, else origin/master; exits when neither exists."""
    for candidate in PROTECTED_REFS:
        if git.ref_exists(repo, candidate):
            return candidate
    die(f"{repo} has neither {' nor '.join(PROTECTED_REFS)}: no protected ref for verify.yml")


def load(repo: str, sha: str) -> Loaded:
    """Reads verify.yml from the protected ref, never from `sha`; notes when `sha` differs."""
    ref = protected_ref(repo)
    ref_sha = git.resolve_commit(repo, ref)
    text = git.show(repo, ref_sha, VERIFY_PATH)
    if text is None:
        die(
            f"{ref} ({ref_sha[:12]}) has no {VERIFY_PATH}: merge the verify configuration into "
            f"{ref} first; qg never reads it from the commit being verified"
        )
    source = f"{VERIFY_PATH}@{ref}:{ref_sha[:8]}"
    raw = load_yaml(text, source)
    config = parse(raw, source)
    return Loaded(raw, config, ref, ref_sha, git.show(repo, sha, VERIFY_PATH) != text)
