"""One headless Claude Code run inside the jail, over a sandbox checkout, with its limits."""

from dataclasses import dataclass
from pathlib import Path

from qg import jail, paths, qa, settings
from qg.sandbox import Sandbox, base_env


@dataclass(frozen=True)
class AgentResult:
    """What one confined agent left: why its run is not trusted (empty when it is), cost, turns
    and the file holding its JSON output."""

    problems: list[str]
    cost_usd: float | None
    turns: int | None
    output: Path


def claude_command(
    claude: Path,
    prompt: str,
    max_turns: int | None,
    max_cost_usd: float | None,
    add_dir: Path,
    tools: str,
    readable: tuple[Path, ...] = (),
    verbose: bool = False,
    model: str = "opus",
) -> list[str]:
    """Returns the headless Claude Code command: file tools only, no shell, no MCP, no browser;
    its file tools reach the working directory, `add_dir` and each of `readable`. A cap given as
    None is not passed: the agent runs uncapped. `verbose` puts every message, tool calls and
    their results included, in the JSON output; `model` is the model it runs on."""
    return [
        str(claude),
        "-p",
        prompt,
        "--model",
        model,
        *(["--max-turns", str(max_turns)] if max_turns is not None else []),
        *(["--max-budget-usd", f"{max_cost_usd:g}"] if max_cost_usd is not None else []),
        "--output-format",
        "json",
        *(["--verbose"] if verbose else []),
        "--restricted",
        "--strict-mcp-config",
        f"--tools={tools}",
        f"--allowedTools={tools}",
        f"--add-dir={add_dir}",
        *[f"--add-dir={path}" for path in readable],
    ]


def confined(
    box: Sandbox,
    prompt: str,
    *,
    name: str,
    tools: str,
    edits: bool,
    max_turns: int | None,
    max_cost_usd: float | None,
    out_dir: Path,
    limit: float,
    needed: float,
    readable: tuple[Path, ...] = (),
    verbose: bool = False,
    retry_on_limit: bool = False,
    model: str = "opus",
) -> AgentResult:
    """Runs one headless Claude Code in the jail over the sandbox checkout, writable only when
    `edits`, on `model`, with `out_dir` its one other writable directory and `readable` shown
    read-only; its output lands in `logs/<name>-output.json` and its log in
    `logs/<name>-agent.log`, both out of the agent's reach. Raises AccountFailure when the CLI
    refused to run it (see `qa.usage_limit`), after the one retry on another account when
    `retry_on_limit`."""
    claude = jail.claude_binary()
    home = paths.simplify_home(box.run_id)
    err = paths.logs_dir(box.run_id) / f"{name}-agent.log"
    out = paths.logs_dir(box.run_id) / f"{name}-output.json"
    jail.prepare_home(home, needed)
    command = jail.command(
        claude_command(
            claude, prompt, max_turns, max_cost_usd, out_dir, tools, readable, verbose, model
        ),
        workdir=box.src,
        writable=[out_dir],
        workdir_writable=edits,
        home=home,
        tools=settings.load(),
        claude=claude,
        npm_cache=paths.NPM,
        readable=readable,
    )
    offset = err.stat().st_size if err.exists() else 0
    try:
        agent = (
            qa.retry_on_usage_limit(
                lambda left, _resumed: qa.run_agent(
                    command, cwd=box.src, env=base_env(), out=out, err=err, timeout=left
                ),
                home=home,
                budget=limit,
                out=out,
                err=err,
                renew=lambda valid: jail.ensure_login(claude, base_env(), valid),
            )
            if retry_on_limit
            else qa.run_agent(command, cwd=box.src, env=base_env(), out=out, err=err, timeout=limit)
        )
    finally:
        jail.discard_home(home)
    said = None if retry_on_limit else qa.usage_limit(out, err, offset)
    if said is not None:
        raise qa.blocked(said)
    result = qa.read_result(out)
    problems = qa.agent_problems(
        agent,
        result,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        limit=limit,
        err=err,
        who=f"{name} agent",
    )
    return AgentResult(problems, qa.cost_of(result), qa.turns_of(result), out)
