"""Per-run sandbox: the worktree in place or a detached one, dependencies, ports, Postgres."""

import errno
import fcntl
import hashlib
import itertools
import os
import shlex
import shutil
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from qg import git, inplace, paths, proc, queue, stack
from qg.config import DEFAULT_LANE_LIMITS, Config, parse, split_limit
from qg.queue import Request, RunState
from qg.util import die, fill

PORT_NAMES = ("backend", "frontend", "pg", "qa_backend", "qa_frontend", "qa_pg")
QA_PORTS = {"backend": "qa_backend", "frontend": "qa_frontend", "pg": "qa_pg"}
SHARD_PORTS = ("backend", "frontend", "pg")
PG_READY_SECONDS = 60
PROBE_TIMEOUT = 30
SLOW_TIMEOUT = 600
ENV_ALLOWED = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "TERM",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "NVM_DIR",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "YARN_CACHE_FOLDER",
    "PLAYWRIGHT_BROWSERS_PATH",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
)


def base_env() -> dict[str, str]:
    """Returns the allowlisted part of the runner's environment: no database, model or e2e keys."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in ENV_ALLOWED or key.startswith("LC_")
    }


@dataclass(frozen=True)
class Sandbox:
    """Everything a stage needs against one commit: paths, ports, database and environment."""

    run_id: str
    repo: str
    sha: str
    src: Path
    run_dir: Path
    venv: Path
    db: str
    ports: dict[str, int]
    notes: list[str] = field(default_factory=list)
    limits: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_LANE_LIMITS))
    lane: str = "tests"
    shard: int = 0
    shards: int = 1
    serial: "itertools.count[int]" = field(default_factory=itertools.count)

    def wrap(self, scope: str, argv: list[str]) -> list[str]:
        """Returns `argv` run inside its own systemd scope with the limits of `scope`.

        The scopes are the run's cgroups: setup, static, tests, qa_stack and qa_agent.
        """
        cpu, memory = self.limits[scope]
        return [*proc.scoped(self.run_id, scope, cpu, memory, next(self.serial)), *argv]

    def for_qa(self) -> "Sandbox":
        """Returns the same checkout with the QA lane's own ports and Postgres.

        The QA stack runs while the tests do, so it must share neither a port nor a database
        with them: the stack may drop and recreate the database `postgres.db`.
        """
        ports = {name: self.ports[key] for name, key in QA_PORTS.items()}
        return replace(self, ports=ports, lane="qa")

    def for_shard(self, index: int, shards: int) -> "Sandbox":
        """Returns the same checkout as Playwright shard `index` of `shards`.

        Shard 0 keeps the tests lane's ports and Postgres; every other shard has its own
        backend and frontend ports, its own Postgres container and its own TMPDIR, so the
        shards share no port, database or temp file. The tests lane's limits are split among them.
        """
        ports = self.ports
        if index:
            ports = {name: self.ports[f"shard{index}_{name}"] for name in SHARD_PORTS}
        limits = {**self.limits, "tests": split_limit(self.limits["tests"], shards)}
        return replace(
            self, ports=ports, limits=limits, lane=f"shard{index}", shard=index, shards=shards
        )

    @property
    def shard_dir(self) -> Path:
        """Returns the shard's own scratch directory under the run's directory."""
        return self.run_dir / "shards" / str(self.shard)

    @property
    def pg_container(self) -> str:
        """Returns the name of this lane's Postgres container."""
        if self.lane == "qa":
            return f"qg-{self.run_id}-qapg"
        if self.shard:
            return f"qg-{self.run_id}-pg{self.shard}"
        return f"qg-{self.run_id}-pg"

    @property
    def values(self) -> dict[str, str]:
        """Returns the placeholder values every command may use."""
        pg = self.ports["pg"]
        return {
            "run_id": self.run_id,
            "src": str(self.src),
            "run_dir": str(self.run_dir),
            "backend_port": str(self.ports["backend"]),
            "frontend_port": str(self.ports["frontend"]),
            "pg_port": str(pg),
            "db": self.db,
            "shard": str(self.shard),
            "shards": str(self.shards),
            "shard_dir": str(self.shard_dir),
            "pg_url_async": f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{pg}/{self.db}",
            "pg_url_sync": f"postgresql://postgres:postgres@127.0.0.1:{pg}/{self.db}",
        }

    @property
    def env(self) -> dict[str, str]:
        """Returns the command environment: the allowlist, the run's variables and UV_FROZEN=1."""
        env = base_env()
        env["UV_PROJECT_ENVIRONMENT"] = str(self.venv)
        env["UV_FROZEN"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        for name, value in self.values.items():
            env[f"QG_{name.upper()}"] = value
        if self.shards > 1:
            env["TMPDIR"] = str(self.shard_dir / "tmp")
        return env


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Holds an exclusive file lock on `path` for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _digest(path: Path, what: str) -> str:
    """Returns the short sha256 of a lock file, exiting when the commit lacks it."""
    if not path.exists():
        die(f"{what} is missing in the commit; its hash keys the dependency cache")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _run(
    args: list[str], *, cwd: Path | str, env: dict[str, str], log: Path, limit: int, what: str
) -> None:
    """Runs one setup step logged to `log`, exiting when it fails or exceeds `limit` seconds."""
    with log.open("a") as handle:
        handle.write(f"$ {shlex.join(args)}\n")
    result = proc.run_logged(args, cwd=cwd, env=env, log=log, timeout=limit)
    if result.timed_out:
        die(f"{what} timed out after {limit // 60} min; see {log}")
    if result.code != 0:
        die(f"{what} failed with exit {result.code}; see {log}")


def port_free(port: int) -> bool:
    """Tells whether `port` binds on 127.0.0.1, ::1 and 0.0.0.0; ::1 is skipped without IPv6."""
    for family, host in (
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::1"),
        (socket.AF_INET, "0.0.0.0"),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue
            return False
    return True


def port_names(shards: int) -> tuple[str, ...]:
    """Returns the port names a run reserves: the six of its lanes and three per extra shard."""
    extra = tuple(f"shard{i}_{name}" for i in range(1, shards) for name in SHARD_PORTS)
    return (*PORT_NAMES, *extra)


def shard_count(ports: dict[str, int]) -> int:
    """Returns how many Playwright shards a run's reserved ports serve."""
    return 1 + sum(1 for name in ports if name.startswith("shard") and name.endswith("_pg"))


def allocate_ports(run_id: str, shards: int = 1) -> dict[str, int]:
    """Reserves free ports of 21000-21999 in the run's state, away from other running runs."""
    names = port_names(shards)
    with queue.locked():
        reserved = {
            port
            for other in queue.list_runs()
            if other.id != run_id and other.status == "running"
            for port in other.ports.values()
        }
        free: list[int] = []
        for port in paths.PORT_RANGE:
            if port not in reserved and port_free(port):
                free.append(port)
                if len(free) == len(names):
                    break
        if len(free) < len(names):
            die(
                f"fewer than {len(names)} free ports in "
                f"{paths.PORT_RANGE.start}-{paths.PORT_RANGE.stop - 1}"
            )
        ports = dict(zip(names, free, strict=True))
        queue.update(run_id, lambda state: state.ports.update(ports))
        return ports


def _mark_created(state: RunState) -> None:
    """Records that the run owns resources a teardown must remove."""
    state.sandbox_created = True


def _python(sandbox: Sandbox, cfg: Config, log: Path, limit: int) -> None:
    """Syncs the Python environment once per uv.lock hash, under a lock shared by every run."""
    key = sandbox.venv.name
    marker = sandbox.venv / ".qg-synced"
    with _flock(paths.VENVS / f"{key}.lock"):
        if marker.exists():
            os.utime(sandbox.venv)
            sandbox.notes.append(f"python env {key} cached")
            return
        _run(
            sandbox.wrap("setup", proc.shell(fill(cfg.deps.python_sync, sandbox.values))),
            cwd=sandbox.src,
            env=sandbox.env,
            log=log,
            limit=limit,
            what="deps.python_sync",
        )
        sandbox.venv.mkdir(parents=True, exist_ok=True)
        marker.touch()
        sandbox.notes.append(f"python env {key} built")


def _node(sandbox: Sandbox, cfg: Config, package: str, log: Path, limit: int) -> None:
    """Installs a package's node_modules once per yarn.lock hash, hard-linked into the worktree."""
    package_dir = sandbox.src / package
    if not (package_dir / "package.json").exists():
        die(f"{package}/package.json is missing in the commit (deps.node_packages)")
    key = _digest(package_dir / "yarn.lock", f"{package}/yarn.lock")
    name = f"{package.strip('/').replace('/', '_')}-{key}"
    cached = paths.NODE / name / "node_modules"
    with _flock(paths.NODE / f"{name}.lock"):
        if cached.exists():
            os.utime(cached.parent)
            sandbox.notes.append(f"node {package} cached")
        else:
            scratch = paths.NODE / f".tmp-{sandbox.run_id}-{name}"
            copy = scratch / "pkg"
            copy.mkdir(parents=True)
            for item in (
                package_dir / "package.json",
                package_dir / "yarn.lock",
                *package_dir.glob(".yarnrc*"),
            ):
                shutil.copy2(item, copy / item.name)
            install = fill(cfg.deps.node_install, {**sandbox.values, "pkg": str(copy)})
            _run(
                sandbox.wrap("setup", proc.shell(install)),
                cwd=sandbox.src,
                env=sandbox.env,
                log=log,
                limit=limit,
                what=f"deps.node_install for {package}",
            )
            if not (copy / "node_modules").is_dir():
                die(f"deps.node_install for {package} produced no node_modules in {copy}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            (copy / "node_modules").rename(cached)
            shutil.rmtree(scratch)
            _read_only(cached)
            sandbox.notes.append(f"node {package} built")
    record = inplace.load(sandbox.run_id)
    if record is not None:
        inplace.link_node_modules(record, sandbox.run_id, package_dir, cached)
        return
    _run(
        ["cp", "-al", str(cached), str(package_dir / "node_modules")],
        cwd=sandbox.src,
        env=sandbox.env,
        log=log,
        limit=limit,
        what=f"linking node_modules of {package}",
    )


def _read_only(tree: Path) -> None:
    """Removes the write bit of every file under `tree`, so a run cannot mutate the shared cache."""
    done = proc.run(
        ["find", str(tree), "-type", "f", "-exec", "chmod", "a-w", "{}", "+"], timeout=SLOW_TIMEOUT
    )
    if done.returncode != 0:
        die(f"making {tree} read-only failed: {done.stderr.strip()}")


def start_postgres(sandbox: Sandbox, cfg: Config, log: Path, limit: int) -> None:
    """Starts this lane's Postgres container on tmpfs and waits until it accepts connections.

    The container is capped at 1 GB and 2 cores: docker runs outside the lane scopes.
    """
    name = sandbox.pg_container
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--tmpfs",
            "/var/lib/postgresql/data",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            f"POSTGRES_DB={cfg.postgres.db}",
            "-p",
            f"127.0.0.1:{sandbox.ports['pg']}:5432",
            cfg.postgres.image,
        ],
        cwd=sandbox.src,
        env=sandbox.env,
        log=log,
        limit=limit,
        what="docker run postgres",
    )
    deadline = time.monotonic() + PG_READY_SECONDS
    ready = ["docker", "exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"]
    while proc.run(ready, timeout=PROBE_TIMEOUT).returncode != 0:
        if time.monotonic() > deadline:
            logs = proc.run(["docker", "logs", "--tail", "40", name], timeout=PROBE_TIMEOUT)
            die(f"postgres {name} not ready after {PG_READY_SECONDS}s: {logs.stdout}{logs.stderr}")
        time.sleep(1)
    sandbox.notes.append(f"postgres {cfg.postgres.image} ready on port {sandbox.ports['pg']}")


def _worktree(request: Request, cfg: Config, log: Path, limit: int) -> Path:
    """Returns the directory the run works in: the given worktree, else a detached worktree.

    The given worktree is used in place, under its lock, when its HEAD is the requested commit
    and it is clean once the lock is held; otherwise (`--ref`, nightly, a moved HEAD) the run
    gets its own detached worktree at the commit.
    """
    run_id = request.id
    if request.worktree:
        inplace.acquire(run_id, request.worktree)
        if inplace.eligible(request.worktree, request.sha):
            inplace.save(run_id, inplace.Record(request.worktree, request.sha))
            return Path(request.worktree)
        inplace.release(run_id)
    src = paths.src_dir(run_id)
    _run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            request.repo,
            "worktree",
            "add",
            "--detach",
            str(src),
            request.sha,
        ],
        cwd=request.repo,
        env=base_env(),
        log=log,
        limit=limit,
        what="git worktree add",
    )
    return src


def create(request: Request, cfg: Config) -> Sandbox:
    """Builds the run's sandbox for `request` and returns it, ready for the stages."""
    run_id = request.id
    log = paths.logs_dir(run_id) / "setup.log"
    limit = cfg.timeouts["setup"] * 60
    queue.update(run_id, _mark_created)
    src = _worktree(request, cfg, log, limit)
    venv = paths.VENVS / _digest(src / "uv.lock", "uv.lock")
    sandbox = Sandbox(
        run_id=run_id,
        repo=request.repo,
        sha=request.sha,
        src=src,
        run_dir=paths.run_dir(run_id),
        venv=venv,
        db=cfg.postgres.db,
        ports=allocate_ports(run_id, cfg.playwright.shards if cfg.playwright else 1),
        limits=cfg.resources.lanes,
    )
    _python(sandbox, cfg, log, limit)
    for package in cfg.deps.node_packages:
        _node(sandbox, cfg, package, log, limit)
    start_postgres(sandbox, cfg, log, limit)
    shards = shard_count(sandbox.ports)
    for index in range(1, shards):
        start_postgres(sandbox.for_shard(index, shards), cfg, log, limit)
    return sandbox


def rebuild(run_id: str) -> tuple[Sandbox, Config] | None:
    """Rebuilds the run's Sandbox and Config from disk for teardown; None when nothing is left."""
    state = queue.load(run_id)
    record = inplace.load(run_id)
    src = Path(record.worktree) if record else paths.src_dir(run_id)
    if not src.exists() or not state.ports:
        return None
    request = queue.load_request(run_id)
    cfg = parse(request.config, f"request of run {run_id}")
    lock = src / "uv.lock"
    key = hashlib.sha256(lock.read_bytes()).hexdigest()[:16] if lock.exists() else "missing"
    box = Sandbox(
        run_id=run_id,
        repo=request.repo,
        sha=request.sha,
        src=src,
        run_dir=paths.run_dir(run_id),
        venv=paths.VENVS / key,
        db=cfg.postgres.db,
        ports=state.ports,
        limits=cfg.resources.lanes,
    )
    return box, cfg


def run_teardown_commands(run_id: str) -> None:
    """Runs verify.yml's teardown commands once per lane while Postgres is up; never fatal.

    The tests lane, the QA lane and each extra Playwright shard have their own ports and
    database, so each gets its own run of the commands (for example to drop that lane's
    vector-store collections).
    """
    rebuilt = rebuild(run_id)
    if rebuilt is None or not rebuilt[1].teardown:
        return
    box, cfg = rebuilt
    log = paths.logs_dir(run_id) / "teardown.log"
    shards = shard_count(box.ports)
    both = [box, box.for_qa()] if "qa_pg" in box.ports else [box]
    both += [box.for_shard(index, shards) for index in range(1, shards)]
    lanes = [lane for lane in both if _container_alive(lane)]
    for lane in lanes:
        for template in cfg.teardown:
            command = fill(template, lane.values)
            with log.open("a") as handle:
                handle.write(f"$ {command}\n")
            result = proc.run_logged(
                proc.shell(command),
                cwd=lane.src,
                env=lane.env,
                log=log,
                timeout=cfg.timeouts["teardown"] * 60,
            )
            with log.open("a") as handle:
                handle.write(f"[exit {result.code}{', timed out' if result.timed_out else ''}]\n")


def _container_alive(lane: Sandbox) -> bool:
    """Tells whether this lane's Postgres container still exists, running or not."""
    named = proc.run(
        ["docker", "ps", "-aq", "--filter", f"name=^{lane.pg_container}$"], timeout=PROBE_TIMEOUT
    )
    return bool(named.stdout.split())


def containers(run_id: str) -> list[str]:
    """Returns the ids of the docker containers named qg-<run id>-*, running or not."""
    listing = proc.run(
        ["docker", "ps", "-aq", "--filter", f"name=qg-{run_id}-"], timeout=PROBE_TIMEOUT
    )
    return listing.stdout.split()


def teardown(run_id: str, repo: str) -> bool:
    """Stops stacks, runs teardown commands, removes containers and the run's own worktree.

    A worktree worked on in place is never removed: only what the run added to it is.
    """
    stack.stop_all()
    run_teardown_commands(run_id)
    src = paths.src_dir(run_id)
    found = containers(run_id)
    if found:
        proc.run(["docker", "rm", "-f", *found], timeout=SLOW_TIMEOUT)
    if inplace.load(run_id) is not None:
        return inplace.cleanup(run_id) and not containers(run_id)
    proc.run(["git", "-C", repo, "worktree", "remove", "--force", str(src)], timeout=SLOW_TIMEOUT)
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    git.remove_worktree_entry(repo, str(src))
    return not src.exists() and not containers(run_id)
