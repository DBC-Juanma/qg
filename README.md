# qg

Feature verification runner. `qg verify` queues the verification of one commit; a runner (a systemd user
service) builds an isolated environment for it (the worktree it was given, in place, or a detached worktree; cached dependencies, its own ports and a
throwaway Postgres), runs the stages in the background and writes a report with a verdict for the commit's
**tree**. Deploy scripts call `qg check` and refuse a tree without a passing report. Every run of one feature
joins a **cycle**, and each cycle has one HTML page that a Claude session publishes as a single artifact.

Python 3.12, `uv`, no dependency but PyYAML.
This tool has no tests of its own, and nobody runs a repository's tests by hand: they run here, in the runner,
once the operator activates it.

## What is built

| Stage | What | State |
|---|---|---|
| setup | the given worktree in place (or a detached worktree of the sha), Python and node dependency caches, ports, Postgres | built |
| S0 simplify | **optional (`qg verify --simplify`), not part of the gate**: headless `/simplify` over `base..sha`; its fixes become one commit on `refs/heads/qg/simplified/<run-id>` and every later stage verifies that commit (see *S0, simplify*) | built |
| S1 static | commands from `stages.static` | built |
| S2 backend | commands from `stages.backend` (alembic and the `-m pg` tests are just commands) plus the **affected tests** of `affected.backend` | built |
| S3 frontend | commands from `stages.frontend` plus `vitest related` per changed package from `affected.frontend` | built |
| S4 playwright | critical specs + specs the surface map ties to the files the feature touched | built |
| S5 qa | headless Claude Code QA agent with a Playwright MCP browser (see *S5, the manual QA pass*) | built, **never run** |

What a run executes is what verify.yml's `deploy.<env>.stages` lists; `simplify` is dropped from a policy and runs only with
`--simplify`. A normal deploy takes about 15 minutes of wall clock because the stages run in **lanes** (see *The fast gate*);
the manual QA pass (S5) is the real gate.

## The fast gate

**Stage graph.** After setup (worktree, dependency caches, the tests lane's Postgres) the static and tests lanes start
together, each in its own systemd scope, and the QA lane starts when the tests lane has ended (operator, 2026-11-12: a run
never holds the Playwright stack and the QA stack, two app stacks and two browsers, at once):

```
setup -> static                                                                   (lane static: S1)
      -> backend -> frontend -> playwright -> QA Postgres -> stack boot -> agent   (lane tests: S2, S3, S4, then lane qa: S5)
```

The QA lane has its own ports and its own Postgres container (`qg-<id>-qapg`), so its stack never shares a database with the
tests, and its stack boot runs while the tests do. A failed stage never stops another lane or another stage of its lane: one run
reports everything that is wrong. `--simplify`, when asked for, runs alone before the lanes.

**Cgroups.** Commands run as `systemd-run --user --scope -p CPUQuota -p MemoryMax -p MemorySwapMax=0` per lane
(`resources.lanes` in verify.yml). Defaults, summing to 20 cores and 16 GB while the lanes run: executor 100% / 1G, static 200% /
2G, tests 1000% / 6G, qa_stack 500% / 5G, qa_agent 200% / 2G; setup (before the lanes) 1600% / 8G. Docker Postgres containers
are capped at 1 GB and 2 cores each outside those scopes. `qg cancel` and the janitor stop `qg-<id>.scope` and `qg-<id>-*.scope`.

**Affected tests.** `affected` in verify.yml (read from the protected master, never from the branch) maps the changed paths of
`base..sha` (else against master) to tests: `affected.backend.map` is a list of `{paths: [globs], tests: [pytest paths]}`; the
`always` list (the core: tenant and workspace isolation, billing, auth and permissions, webhook signatures) runs
whatever changed; every changed test module runs; a path in `whole_when` (fixtures, root conftest, lockfile) selects `all`; a
changed path that maps to nothing is named in the stage summary. `command` must contain `{tests}`. The Postgres `-m pg` lane
stays in `stages.backend`. `affected.frontend` lists packages with `related` (`{files}`: vitest's module graph, the tests that
import a changed file), `all` (the whole suite, used when a `whole_when` path of the package or a `shared` path changed). A
package with no change runs nothing. The selected tests are part of the stage fingerprint, so `resume` reuses a pass only for
the same selection.

**Nightly.** `qg nightly [--ref REF]` queues every test of a commit (default: master, no QA, no base). It is never a deploy
gate (`qg check` and reuse ignore it), never supersedes a verification, runs only when no verification waits and is cancelled
when one arrives.

**Playwright shards.** `stages.playwright.shards: N` (default 1, nothing changes) runs the selected specs as at most N shards
at once, dealt round-robin. Shard 0 keeps the tests lane's ports and Postgres; every other shard has its own backend, frontend
and Postgres ports, its own container (`qg-<id>-pg<k>`) and its own `TMPDIR` (`<run>/shards/<k>/tmp`), and runs the `run`
command with its own `{backend_port}`, `{frontend_port}`, `{pg_url_*}`, plus `{shard}`, `{shards}` and `{shard_dir}` (a scratch
directory of the shard: point Playwright's `--output` there, or the shards wipe each other's `test-results`). The tests lane's
cpu and memory limits are divided among the shards, so raise `resources.lanes.tests` when sharding. Each shard logs to
`playwright-shard<k>.log`, joined into `playwright.log`. The stage fails when any shard fails; its failures are the union,
its commands are every shard's, its specs the whole selection.

**Release gate.** `qg verify --release --repo <candidate> --phases F01:P1,F02:P1,... --plan-dir <dir> [--ref] [--env]
[--base]` queues one *release*: a release run (mode `release`, in the cycle `RELEASE-<sha12>`) with static, backend, frontend
and the whole Playwright, and one QA-only run per phase (mode `phase`, the plan being `<dir>/F01-P1.md`), each an attempt of
its own phase cycle with its own QA, ACs and findings. The QA runs queue behind the release run, so under the memory
admission they run as the machine allows. When the release run finishes, its shared stages are copied onto every phase run as
`reused from <release run>`. There is **one verdict**: the release run's, judged on its stages plus the QA of every phase and
every finding of the tree; a phase run is never accepted by `qg check` on its own, and `qg check` of the release closes every
phase cycle. A failing Playwright test belongs to the phase whose tag its scenario carries: an AC tag `@F01-P1-AC03` names its
phase, a PRD tag `@F01-D10` names the PRD's one phase in the release. The release report says "1 test failed, owned by F01·P1"
and lists each failing test with its owner (`sin dueño` for a test with none); the other phases' pages say "bloqueada por
F01·P1" instead of repeating the failure. `qg cancel` of the release run cancels its phase runs.

**Flake hunt.** When the Playwright stage of a release run fails, only the failed tests are run again 3 times with the stage's
own `run` command (one `file::test` node id for pytest lines, `file:line` for Playwright lines; each rerun is capped at 10
minutes, and the run's deadline grows by 30). Each failed test is labelled `falla siempre` (failed in all 3) or `intermitente
N/3` (failed in N of the 3 reruns); the labels show beside the test and its owner in the report and as a tally in the verdict
line. When every failed test is `intermitente 0/3` (it failed once and passed all its reruns) the stage passes and the report
lists the tests under "Avisos" as intermittent, to be fixed; any other label keeps the stage failed and blocking (operator,
2026-10-21). The reruns use the run's own ports and database, not the stack of the first pass (a repo whose run command boots
the stack boots it again).

**Stress pass.** `qg verify --stress N` (any verification, release or not; 1 to 20) runs the whole Playwright stage N times on the
same tree, never reusing an earlier pass, and reports every test that failed at least once, labelled `falla siempre` (failed in
all N) or `intermitente k/N`. It finds every intermittent test in one run instead of one per cycle. The stage still fails when
any pass failed, and the run's deadline grows by (N-1) Playwright timeouts. A release run with `--stress` skips the flake hunt
(the stress labels replace it).

**QA only at phase close or release** (operator, 2026-11-13). `qg verify` runs the QA stage only when it is given both
`--prd` and `--phase` (the phase is being closed), and `qg verify --release` runs one QA pass per phase. A verification
without them is a run between closes: it is queued without QA, prints `QA left out: the QA pass runs only when a phase is
closed (--prd <id> --phase <id>) or at release (--release); this run checks the other stages`, and joins the cycle of its
`--feature` (opening it when there is none). Its verdict is judged on the stages it ran, so it ends READY when they pass,
and its first warning says `QA not run: ...` with the stages it covered; `qg check` still judges every run under the whole
`deploy.<env>` policy, so a run without QA never satisfies a deploy, and the cycle page's verdicts per env stay NOT READY
for it. `--no-qa` leaves QA out of a phase close the same way. `qg retest` and `qg resume` keep the QA setting of the run
they repeat; `qg nightly` never runs QA.

**Findings: what the session does with them.** P0 and P1 block (`report.blocking`: an open P0, an open P1 the operator has
not acknowledged): the session fixes them and the next run of the cycle (`qg retest <run>`) re-verifies them. P2 and P3
never block and never need a run (operator, 2026-11-13: "as they are not important, we assume the fix is not important
either"): the session fixes them in the working tree, commits, and closes each with `qg fixed <run> <finding> --commit
<sha> --note <text>`. A later QA pass of the cycle is never asked to re-check an earlier P2 or P3: the charter lists them
apart as known, not to be re-checked nor filed again, and the split pass's retest agent gets only the P0 and P1.

**QA is the gate.** The pass is split (operator, 2026-11-12): one short Sonnet agent per plan topic and one more for the retests of earlier findings, run one after another on the same stack, each with its share of the turns and minutes and a note naming only its own topics and ids; their `findings.json` are merged before judging. A long walk costs turns × context re-read, so several short contexts cost a fraction of one long one. With a generated plan the QA agent walks only its topics (the generic journeys and surface-map flows are
Playwright's) with 7 minutes per topic plus 7 for the retests, at least 14 and at most 70 minutes, its turn cap and the stage timeout growing in the
same proportion (`qa.time_limit_minutes` and `max_turns` are the minimum budget and set the turns per minute). `qg verify`
prints the estimate up front. The QA stage fails only when the run did not complete (stack down, agent crash, no findings file,
no flow walked, no screenshot). Running out of time, turns or money, a topic not reached, a missing locale or width, or a journey
left out is a P2 finding `not walked: <why>` on that topic (`G-` ids; a plan with no plan flow at all is a P1), re-judged on every
run. A walked plan flow needs 1280 and 390 px in at least one locale. A command killed by the OOM killer fails its stage with
"out of memory in lane <lane>". `qg resume <run>` re-runs only the failed stages: the others are reused under fail-closed
fingerprints, and QA evidence is copied refusing symlinks and any path that resolves outside its run directory.

## Requirements

qg runs on **Linux only**: every stage runs in a `systemd-run` scope of the user session and every agent in a
`bubblewrap` jail. macOS and Windows are not supported (WSL2 with systemd enabled is untested).

| Needed for | Program | Notes |
|---|---|---|
| everything | Python 3.12, [`uv`](https://docs.astral.sh/uv/) | `uv` installs qg and the right Python |
| everything | `git`, `curl` | |
| everything | systemd user session (`systemd-run`, `systemctl --user`), cgroup v2 | the default on current distributions |
| everything | Docker Engine, usable without `sudo` | runs the Postgres container of each run |
| agent stages | `bubblewrap` at `/usr/bin/bwrap` | the jail of every agent |
| agent stages | [Claude Code](https://code.claude.com/docs) (`claude` on `PATH`), logged in | |
| QA stage | Node.js 18 or newer with `npx` | runs `@playwright/mcp` |
| audit of the simplify stage | `ruff`, `semgrep`, `gitleaks`, `osv-scanner` on `PATH` | then `qg audit-rules` downloads the semgrep packs |
| cycle page thumbnails (optional) | `ffmpeg` or ImageMagick (`magick` / `convert`) | without it the page links the full images |

System packages:

```bash
# Debian / Ubuntu (24.04 or newer; older releases ship Node < 18: install Node from nodesource.com instead)
sudo apt install git curl bubblewrap docker.io nodejs npm gitleaks ffmpeg
sudo usermod -aG docker "$USER"        # log out and in again

# Fedora
sudo dnf install git curl bubblewrap nodejs npm
# Docker Engine: https://docs.docker.com/engine/install/fedora/ ; gitleaks and ffmpeg: see below

# Arch
sudo pacman -S git curl bubblewrap docker nodejs npm ffmpeg
sudo systemctl enable --now docker && sudo usermod -aG docker "$USER"
```

The same on every distribution:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv
curl -fsSL https://claude.ai/install.sh | bash         # Claude Code, then run `claude` once to log in
uv tool install ruff && uv tool install semgrep        # audit tools
```

`gitleaks` (where the distribution has no package) and `osv-scanner` are single binaries: download them from
<https://github.com/gitleaks/gitleaks/releases> and <https://github.com/google/osv-scanner/releases> into a directory
on `PATH`. After installing qg, `qg doctor` checks docker, systemd-run, uv, git, claude, curl, disk, ports and images.

## Install and enable

```bash
uv tool install --editable --python 3.12 /path/to/qg   # puts qg in ~/.local/bin
qg doctor                                              # docker, systemd-run, uv, git, claude, curl, disk, ports, images
qg install-service                                     # copies the unit to ~/.config/systemd/user, daemon-reload only
systemctl --user enable --now qg-runner.service        # starts the daemon; add `loginctl enable-linger $USER` to keep it without a session
qg enable                                              # the operator's switch: until this file exists the runner executes nothing
```

`qg disable` turns the switch off again (a run in flight finishes). `qg status` shows `paused: the operator has
not activated qg` while the switch is off. `qg doctor` prints ok, warn or fail per item; `warn` is the optional
image tool (see the cycle page below) and does not change the exit status.

State lives under `~/.local/state/qg` (override with `QG_STATE_ROOT`):

```
enabled  queue.lock  overrides.log  board.html
runs/<id>/      request.json state.json report.md report.json findings.json qa.json logs/ executor.log src/
cycles/<id>/    cycle.json page.html attempts/<run-id>.json thumbs/
cache/venvs/<sha256(uv.lock)[:16]>   cache/node/<package>-<sha256(yarn.lock)[:16]>
```

Run ids are `YYYYMMDD-HHMMSS-xxxx`; every command that takes an id accepts a unique prefix.

## Commands

| Command | What it does |
|---|---|
| `qg verify --feature TEXT [--prd ID --phase ID] [--repo PATH] [--ref HEAD] [--base SHA] [--plan PATH] [--env prod\|staging] [--no-qa] [--simplify] [--force] [--dry-run]` | Runs the QA stage only with both `--prd` and `--phase` (a phase close) and without `--no-qa`; without them it prints the `QA left out` line and queues the other stages (see *QA only at phase close or release*). Resolves the canonical repository (the real path of the git common dir's parent, so every worktree of one repository is the same repo), branch (`detached` when none), sha and tree; refuses a repository whose `master` (else `origin/master`) has no `.claude/verify.yml`. Prints the existing run and queues nothing when the tree already passed every asked stage (unless `--force`). `--force` queues anyway and reuses nothing. Otherwise queues the run (superseding a queued run of the same repo+branch), appends it to the feature's cycle and prints run id, queue position and an ETA from the last five runs. A plain verification (no `--force`, `--stress`, retest or resume, on a branch) is held 120 s in the queue before it may start (`queue.HOLD_SECONDS`), so a newer commit of the same cycle supersedes it before it boots anything; `qg verify` and `qg status` print `starts in Ns: waiting for a newer commit of this cycle to supersede it` and the ETA counts the hold. Nightly, release and phase runs and automatic retries are never held. `--feature` is required. `--base` is the commit the change is measured from (default: the `deployed.json` record of the repo and env); with a base qg writes `runs/<id>/plan.md` (see *The base and the plan*). **`--env prod` with no base and no `--plan` exits non-zero: "pass --base <sha> or --plan PATH"; it never runs with an empty plan.** `--dry-run` prints the base, the generated plan, the stages (S0 included), the commands with example values, the specs S4 would run and the teardown commands, and creates nothing. |
| `qg lite [--repo PATH] [--ref HEAD]` | Checks a commit now, in the foreground, with static, rules, the affected tests that need no database and the mutant hunt; no Postgres, stack or agent. Exit 0 green, 1 red. Never queued and never a deploy verdict (see *Lite verification*). |
| `qg wait <id> [--timeout S]` | Polls `state.json` every 5 s, one line per stage transition. A run that ended `error` and was queued again (see *Infrastructure failures*) is followed to its retry. Exit 0 READY, 1 NOT READY, 2 cancelled, interrupted, superseded or error, 3 timeout, 4 while qg is paused (the run is queued and qg is not enabled: it prints that once and exits). Cheap; if it dies the run goes on. |
| `qg status [--all]` | Enabled or paused, service state, the four slots, the queue, the last-deployed record per repo and env, and the last run per repo+branch (id, sha, env, verdict, open P0/P1, when, feature). |
| `qg deployed <sha> --env prod\|staging [--repo PATH]` | Sets the last-deployed record by hand. |
| `qg report <id>` | Prints `report.md`. |
| `qg retest <id>` | Queues a new run for the same repo, branch tip and feature with `retest_of=<id>`. Never deduplicated, and **nothing is reused**: every stage runs again. |
| `qg resume <id>` | Re-queues a cancelled, failed, interrupted or errored run for the **same requested commit** and the same base, plan and feature (`retest_of=<id>`), reusing every stage that passed (see *Stage reuse*). |
| `qg check <sha> --env prod\|staging [--repo PATH] [--unverified REASON]` | Exit 0 only when a finished run of that sha's tree satisfies the env policy; on OK it records the sha in `deployed.json` (the base of the next plan). A run that simplified verified another tree, so the requested sha is NOT VERIFIED until `qg/simplified/<run-id>` is fast-forwarded into the branch; the output names the ref. `--unverified` skips the check, appends `{when, repo, sha, tree, env, reason, last_run_id, open_findings}` to `overrides.log`, prints a loud warning and exits 0. |
| `qg ack <id> <finding-id> --note TEXT` | Acknowledges a P1 finding (who `operator`, when, note, stored in the run's `findings.json`): the operator accepts it, and it stops blocking READY. An open P1 nobody acknowledged makes the run NOT READY. P0 cannot be acknowledged. |
| `qg fixed <id> <finding-id> --commit SHA --note TEXT` | Closes a P2 or P3 finding the session fixed, without a run: status `fixed` and `closed` `{who: session, when, commit, note}` in the run's `findings.json`, then the report, cycle page and topic pages are rebuilt, so the page says "cerrado sin run" and later runs of the cycle no longer carry it. Refused for a P0 or P1 ("a P0 or P1 is closed only by a run of its cycle that re-verifies it"), for a finding that is not open, and on a run when a later finished run of the same cycle holds the same finding (close it there). |
| `qg probe-review <id> <finding-id> [--probe accepted\|rejected] [--flip accepted\|rejected] --why TEXT` | The operator's review of a P0's red probe and its flip patch: it replaces the verifier's review on the finding (reason `operador: …`; only an accepted probe is rerun by retests) and is kept with the probe file, its output and the patch in `calibration/<run>-<finding>/`, outside the run so `qg prune` never removes it, as an example for later verifiers. |
| `qg verify-p0 <id>` | Lets the P0 verifier judge a finished run's open P0 findings that no verifier has judged, on a temporary worktree of its commit; prints the tally and rewrites the run's report (and its release's). |
| `qg cancel <id>` | Queued: cancelled at once. Running: stops the run's systemd scope (SIGTERM to everything in it), tears the sandbox down, status `cancelled`. |
| `qg topics <run-id>` | Rebuilds the topic pages of the run's cycle and prints, per topic, `topic:` (slug), `name:`, `verdict:`, `page:` and `artifact:`. These are what gets published. |
| `qg topic-artifact <cycle-id> <slug> <url>` | Stores the claude.ai artifact URL of one topic. One topic keeps one URL: a different one is refused. |
| `qg cycle-page <run-id>` | Rebuilds the local cycle page (debugging only, not published) and the topic pages, and prints `cycle:`, `feature:`, `page:` and `artifact:`. |
| `qg cycle-artifact <cycle-id> <url>` | Stores the artifact URL of a cycle; kept for old records, the cycle page is no longer published. |
| `qg doctor` | Checks docker, systemd-run (a user scope with `CPUQuota`), uv, git, claude, curl, the state root, free disk (20 GiB), free ports (21000-21999) and the image tool. |
| `qg enable` / `qg disable` | Create or remove `~/.local/state/qg/enabled`. |
| `qg runner` | The daemon loop, in the foreground. systemd runs it. |
| `qg board` | Writes `~/.local/state/qg/board.html`, a static table of the last run per repo+branch with verdict, findings, stage times and links, and prints its path. |
| `qg rules check --base SHA --head SHA [--repo PATH] [--config FILE]` | Prints the architecture-rule violations `--head` adds over `--base` (see *Architecture rules*), then one summary line. Exit 0 when it adds none, 1 otherwise. |
| `qg rules baseline [--repo PATH] [--config FILE]` | Counts every violation in the whole tree at `HEAD`, per rule (with its change since the repository's previous snapshot) and for the 20 files that hold most, and stores the snapshot (see *Architecture rules*). Reads git objects only. |
| `qg init [--repo PATH] [--profile python-layered] [--write] [--no-baseline]` | Onboards or reforms a repository: detects its layout at the protected ref, prints the proposed verify.yml (a whole minimal file, or the optional sections it lacks appended) as a diff, counts the proposed rules, and prints the next steps (see *Onboarding a repository*). Dry run unless `--write`; never commits. |
| `qg install-service` | Copies `systemd/qg-runner.service` to `~/.config/systemd/user/` and runs `systemctl --user daemon-reload`. Enables and starts nothing. |

## verify.yml

Read from the repository's **protected ref**: the local `master` if it exists, else `origin/master` (the operator keeps master local and pushes nothing without an OK, so `origin/master` lags)
(`git show <ref>:.claude/verify.yml`), never from the commit being verified, so a branch cannot rewrite its own
stage commands or poison the shared dependency caches. The ref's sha is recorded in the run state and report. When
the verified commit's `verify.yml` differs, the report and `verify` say "verify.yml changed in this branch: not
used". If the protected ref has no `verify.yml`, `verify` and `check` refuse: merge the configuration first.
The schema is closed: an unknown key is an error that names its path. The YAML is parsed strictly, so what qg
runs is exactly what the protected ref holds: a duplicate key, a `<<` merge key, an alias, an anchor or an explicit tag
is refused, and every command must be a string. The ref is resolved as the fully qualified `refs/heads/master`
(else `refs/remotes/origin/master`) with `git rev-parse --verify --end-of-options`, so a branch or tag named like a ref cannot
stand in, and the file is read by `<sha>:.claude/verify.yml` of that resolved sha. The surface map is parsed the same
way.

```yaml
version: 1
deps:
  python_sync: "uv sync --frozen"
  node_packages: ["frontend", "frontend-widget", "frontend-app"]
  node_install: "bash scripts/with-node.sh yarn --cwd {pkg} install --frozen-lockfile"
postgres: {image: "postgres:15-alpine", db: "app_verify"}
resources:
  cpu_quota: "100%"            # the executor
  memory_max: "1G"
  lanes: {tests: {cpu_quota: "1000%", memory_max: "6G"}}   # setup, static, tests, qa_stack, qa_agent
timeouts_minutes: {simplify: 30, static: 5, backend: 15, frontend: 8, playwright: 15, qa: 30}
typical_minutes: {backend: 6, qa: 12}   # the estimate when there is no history
affected: {backend: {command: "... {tests}", always: [], map: [], all: [], whole_when: []}, frontend: [{package: frontend, related: "... {files}", all: "...", whole_when: [], shared: []}]}
stages:
  static: ["<cmd>", ...]
  backend: ["<cmd>", ...]
  frontend: ["<cmd>", ...]
  simplify:                      # optional: S0's limits and the directories it may edit
    paths: ["src/", "frontend/src/", ...]   # default: src/, frontend/src/, frontend-widget/src/, frontend-app/src/, frontend-booking/src/
    time_limit_minutes: 25
    max_turns: 150
    max_cost_usd: 10
  playwright:
    critical: ["frontend/e2e/<spec>.spec.ts", ...]
    surface_map: "docs/smoke/surface-map.yml"
    run: "<cmd with {specs}>"
    shards: 1                    # optional: N > 1 runs the specs as N shards at once (see below)
  qa:
    boot: "<cmd that boots the app stack and stays in the foreground>"
    ready_url: "http://localhost:{frontend_port}"
    app_url: "http://localhost:{frontend_port}"
    api_url: "http://localhost:{backend_port}"
    secrets_env_file: "/abs/path/.env"
    secrets_keys: ["OPENROUTER_API_KEY", ...]
    accounts: [{role: "admin", name: "...", email: "...", password: "..."}]
    time_limit_minutes: 40
    max_turns: 250
    max_cost_usd: 15
    notes_file: ".claude/quality-gates-notes.md"
probe:                           # optional: the P0 probe and flip (needs affected.backend)
  dir: "tests/qg_probes/"        # where the probe agent writes test_qg_<finding>.py
  max_p0: 5
  max_cost_usd: 3                # per P0 and agent (probe, flip), across its attempts
  time_limit_minutes: 40         # the whole probe pass of a run
mutants:                         # optional: the mutant hunt after a passing backend stage (needs affected.backend)
  max: 12                        # mutants per run, one changed line at a time across the changed files
  time_limit_minutes: 20         # added to the deadline of a run with the backend stage
lite:                            # optional: the tests `qg lite` runs (needs affected.backend; see Lite verification)
  command: "uv run pytest -q {tests}"   # runs the selected tests that need no database
  tests: ["tests/unit/**"]       # globs of the tests that need no database (default: every selected test)
rules:                           # optional: architecture rules in the static stage (see Architecture rules)
  profile: python-layered
  layers: {components: ["app/api/**"], managers: ["app/managers/**"], services: ["app/services/**"], repositories: ["app/repositories/**"]}
  tests: ["tests/**"]
  markdown_allow: ["docs/**"]
  disable: []
teardown: ["<cmd>", ...]
services: ["<unit>.service", ...]  # optional: systemd user units the app stacks need (see Services)
deploy:
  prod: {stages: [static, backend, frontend, playwright, qa], max_p0: 0, p1_acknowledged: true}
  staging: {stages: [static, backend, frontend, playwright]}
```

Required: `version` (must be 1), `deps.python_sync`, `postgres.image` and `.db`, `stages`, `deploy` (at least one of
`prod`, `staging`), `deploy.<env>.stages`, `stages.playwright.run`, and in `stages.qa` the keys `boot`,
`ready_url`, `app_url`, `api_url`. `deps.node_install` is required when `node_packages` is not empty. In
`qa.accounts` entries `role`, `email` and `password` are required and `name` is optional.

Defaults: `resources` 100% and 1G for the executor and the lane limits above, `timeouts_minutes` as above plus two optional keys, `setup` (30 minutes per
sandbox step) and `teardown` (5 minutes per teardown command), `deploy.<env>.max_p0` 0, `p1_acknowledged` false,
`qa.time_limit_minutes` 15, `qa.max_turns` 150, `qa.max_cost_usd` 15 (a number > 0; `secrets_keys` needs `secrets_env_file`), `playwright.critical` empty, `teardown` empty, `services` empty. Every stage a deploy
policy lists must be configured under `stages`. A stage list may be empty (the stage passes with `no commands`).
`{specs}` must appear in `playwright.run`.

### Placeholders and environment

Every command, run with `bash -c` in the worktree, may use these placeholders; a `{name}` is replaced unless a
`$` precedes it, so `${X}` and other braces (`{a,b}`) are untouched:

`{run_id}` `{src}` `{run_dir}` `{backend_port}` `{frontend_port}` `{pg_port}` `{db}` `{pg_url_async}`
(`postgresql+asyncpg://postgres:postgres@127.0.0.1:{pg_port}/{db}`) `{pg_url_sync}`
(`postgresql://postgres:postgres@127.0.0.1:{pg_port}/{db}`), plus `{specs}` in `playwright.run` (the selected
specs, space-separated and shell-quoted) and `{pkg}` in `deps.node_install` (the absolute path of a temporary copy
of the package directory, holding `package.json`, `yarn.lock` and any `.yarnrc*`; the command itself runs in the
worktree root).

The environment of every command is an allowlist of the runner's (`PATH` `HOME` `USER` `LOGNAME` `LANG` `LC_*` `TERM`
`TMPDIR` `XDG_RUNTIME_DIR` `XDG_CACHE_HOME` `XDG_DATA_HOME` `XDG_CONFIG_HOME` `NVM_DIR` `UV_CACHE_DIR`
`UV_PYTHON_INSTALL_DIR` `YARN_CACHE_FOLDER` `PLAYWRIGHT_BROWSERS_PATH` `DOCKER_*`), so `DATABASE_URL`, `QDRANT_*`, `E2E_*`
and model keys are never inherited; plus `UV_PROJECT_ENVIRONMENT` (the cached venv), `UV_FROZEN=1`,
`PYTHONUNBUFFERED=1` and the placeholders as variables:
`QG_RUN_ID` `QG_SRC` `QG_RUN_DIR` `QG_BACKEND_PORT` `QG_FRONTEND_PORT` `QG_PG_PORT` `QG_DB`
`QG_PG_URL_ASYNC` `QG_PG_URL_SYNC`.

### How stages behave

- Commands of a stage run in order; the first non-zero exit fails the stage, a stage timeout kills the command's
  process group, and a failed stage skips the rest. Output goes to `runs/<id>/logs/<stage>.log`. Background
  processes a command leaves behind are not killed until the run ends.
- Summaries are read from the output: pytest's final line, vitest's `Tests` line, Playwright's `N passed` and
  `N failed` lines, plus the first 30 `FAILED` lines (and `ERROR`, vitest `FAIL`, Playwright failures).
  pytest commands containing `-m pg` count as the Postgres suite on the cycle page.
- S4 runs the `critical` specs plus the `specs` of every surface-map entry whose `paths` globs (`fnmatch`) match a
  file in `git diff --name-only <merge-base(sha, master or origin/master)>..<sha>`. Specs that do not exist in the
  commit are dropped, the rest are deduplicated. The surface map is a YAML list of `{paths, flows, specs}`. Every
  listed spec is handed to `playwright.run`, so keep non-Playwright files out of `specs`. With no spec at all the
  stage is skipped with `no specs`, which is not a pass. **S4 boots its own stack**: the repository's Playwright
  `webServer` does it inside `playwright.run`, on the run's ports and Postgres. The runner starts no stack for S4.
- S5 is described in *S5, the manual QA pass* below.
- Dependencies: the Python environment is `cache/venvs/<sha256(uv.lock)[:16]>`, synced once per key under a file
  lock; each node package's `node_modules` is installed once per `yarn.lock` hash in a temporary copy and then
  hard-linked into the worktree; the cached node files are made read-only (`chmod a-w`), so a run cannot mutate
  the shared cache. Installs use the protected config's commands only. Postgres is `docker run -d --name qg-<id>-pg --tmpfs /var/lib/postgresql/data`
  on a free port of 21000-21999, and the run waits for `pg_isready`. The container's `postgres` user is a
  superuser (`POSTGRES_PASSWORD=postgres`), so `CREATEDB` and `DROP DATABASE` work: a stack may drop and recreate
  the database `postgres.db`.
- `teardown` commands run last, in the worktree with the placeholders and the environment above, after the stacks
  stopped and **before the Postgres container is removed**, so a command can still read the run's database (for
  example to drop the run's vector-store collections). Their output is `runs/<id>/logs/teardown.log`. A failing or
  timed-out teardown command is logged and never fails the run; they run only while the run's container exists.
- `services` are the systemd user units the app stacks need and nothing else keeps running (a shared vector store, a
  model server). qg starts what it uses and stops it when done: just before the Playwright or QA stage runs (never
  for a reused stage), the run starts a transient unit `qg-<id>-services.service` that `Requires` and is ordered
  `After` each listed unit, and waits for it (a unit that waits for its own readiness in `ExecStartPost` is ready
  when the stage starts). A unit that does not start ends the run `error`. Teardown stops the transient unit after
  the teardown commands, so the units must declare `StopWhenUnneeded=yes`: they stop when the last run holding them
  releases them, and keep running while another run still holds them. Nothing in `services` is enabled at boot.

## The deployed record, the base and the plan

- A successful `qg check <sha> --env <env>` is the moment a deploy script proceeds. On OK it writes
  `state/deployed.json`: `{<canonical repo>: {<env>: {sha, tree, at}}}`. `qg status` shows it; `qg deployed` sets it by hand.
- `qg verify --base <sha>`; absent, the base is that record for the repo and env. With a base and no `--plan`, qg writes
  `runs/<id>/plan.md` and records it as the run's plan: `git log --no-merges --format='%h %s' base..sha`, then the paths of
  `git diff --name-only base..sha` grouped by the `surface_map` entries of verify.yml (paths matching no entry under
  "other"). An explicit `--plan` wins but is still prefixed with the commit log. Staging may run with no base: S0 and the
  generated plan are skipped, the report says so, and such a run cannot be READY because a policy that lists `simplify`
  needs it `passed`. Production refuses (see `qg verify`).
- A long plan grows S5's limits: the stage timeout, the agent's time limit and turn cap are multiplied by 1 + 0.5 per 3000
  characters of plan, at most 4 times (reached at 18000 characters); the cost cap stays. A plan of 32 KB gets 180 min,
  160 min and 1000 turns with the default 45/40/250, and `stages.qa.max_cost_usd` is then the limit that binds first:
  raise it in verify.yml for a large release.
- Without `--plan`, the generated plan opens with **Topics to walk**: the user-facing commits grouped, by a heuristic over
  the files each touches against the surface map (no model call), into at most 12 topics; commits of type test, docs,
  style, chore, ci, build, reverts and commits that touch only tests, documents or tooling are left out. The section is
  first in the plan, so a truncated plan never loses it, and the charter tells the agent to walk those topics as its
  `plan:` flows.

## Onboarding a repository

`qg init [--repo PATH] [--profile python-layered] [--write] [--no-baseline]` reads the git tree of the protected ref
(git objects only, so a bare clone works too) and detects:

- the Python project (`uv.lock` at the root, ruff in `pyproject.toml`);
- the node packages: every `package.json` directory with a `yarn.lock` (the others are listed as skipped: qg installs
  yarn packages only);
- the test directories and the unit-test directories (`unit`) inside them;
- the profile's layers: the profile's `detect:` mapping gives, per layer and for `tests`, the directory names that
  betray it (`python-layered`: components `api`, `routers`, `routes`, `endpoints`, `websockets`, `cli`; managers
  `managers`; services `services`; repositories `repositories`, `repos`, `dao`; tests `tests`, `test`). A directory
  counts only when it holds Python files, lies outside every test directory and inside no other match of the same
  layer; each becomes a `<dir>/**` glob. A layer with no match is left out and reported.

It then proposes the configuration and prints it as a unified diff:

- **verify.yml at the protected ref**: only the optional sections it lacks, appended to its text, which survives byte
  for byte (comments and formatting included): `rules:` with the detected layers and tests and `enforce: false`, so
  the first rollout is advisory, and `lite:` when `affected.backend` exists, its command copied from
  `affected.backend.command` without the pytest-xdist options after its `pytest` word (`-n`, `--numprocesses`,
  `--dist`, with their values; a command with an unclosed quote or a `$(…)` or backtick substitution is copied as is), so the tests run in
  one process: for a small selection, workers each importing the application cost more time and memory than they
  save. Its `tests` are each unit-test directory and its `/**` glob, since an affected map may list
  the directory itself (left out, and said so, when there is no unit-test directory). With both present it proposes nothing: already configured.
- **no verify.yml**: a whole minimal file (`uv sync --frozen`, the yarn packages, a Postgres image and database, ruff
  as the static stage when the project uses it, pytest on the test directories as the backend stage, prod and
  staging gating those two, and `rules:`). `uv.lock` is required.

Every proposal is parsed with the verify.yml schema and its rules resolved against the profile before it is shown;
the values to review (database name, commands, deploy policies, frontend stage, a lite command that uses a database
placeholder) are listed as `review` lines. Then it counts the proposed rules over the whole tree at the protected
ref and stores the snapshot (see *The burn-down*); `--no-baseline` skips it.

`--write` writes `.claude/verify.yml` into the working tree of `--repo`, and only when the file there is absent or
equal to the protected ref's; qg never commits it. Commit it to the protected branch: qg reads it from nowhere else.
**The runner must run code that knows the new keys before that commit lands**: a `qg runner` daemon started before
the upgrade rejects unknown keys in memory and stalls the queue. `qg init` compares the service's start time with
the last change of the installed `qg/config.py` and prints a `WARNING` when the service is older. Then `qg lite`
checks a commit in the foreground, `qg rules baseline` tracks the burn-down, and `rules.enforce: true` on the
protected branch makes a new violation fail the static stage.

## Architecture rules

A profile is data bundled with qg: `qg/profiles/<name>.yml` names the layers of an architecture, the imports
forbidden between them, banned patterns per layer and two hygiene rules, each with an id and a message naming the
fix. `python-layered` describes a layered Python backend (components → managers → services → repositories): a
component may not import a service or a repository, a service may not import a manager or a component, a repository
may not import a service, manager or component; `.model_dump()` and `.dict()` are banned in managers and services,
swallowed exceptions (`except Exception: pass`) and bare `except:` in every layer, `TestClient` in tests, SQLite URLs
and imports outside tests; no new code comment (docstrings, `# noqa`, `# type:`, `# pragma`, shebang and encoding
lines are not comments here) and no new `.md` file outside the allow-list (`README.md`, `CLAUDE.md`, `CHANGELOG.md`,
`LICENSE.md` at any depth, plus `markdown_allow`).

A repository opts in with the top-level `rules:` of its verify.yml: `profile`, `layers` (each profile layer it has,
mapped to path globs), `tests` (globs), `markdown_allow` (globs), `disable` (rule ids) and `enforce` (default `true`). Globs are `fnmatch`, so `*`
crosses `/`. A forbidden import matches any import whose dotted module has, as one of its parts, the last directory
(or module) the target layer's globs name: `app/services/**` stands for any `…services…` module, `from app import
services` included. A layer the repository leaves out has no rules. Without `rules:` the static stage is exactly
its commands.

**The ratchet.** No baseline is stored. The check lists the files `base..head` adds, modifies or renames, reads each
one from git at the base and at the head into a temporary directory, runs the profile's generated semgrep rules on
both sides in one semgrep call, and fails only where the head holds more matches of one rule in one file than the
base held (a new file starts at 0, a renamed one is compared with its old path); it never keys on line numbers. For a
regression it prints every head-side match of that rule in that file as `path:line rule-id message`. Comments count
only on the lines the change adds and markdown only for added files. Code moved from one file into a new one counts
as new in the new file. Nothing is written into the repository. The repository's contents are untrusted: files are
read by blob id (`git diff --raw -z`, `git ls-tree -z`, `git cat-file --batch`, each answer checked against the id
asked for), only regular files are written (a symlink or submodule entry is skipped), and a path that is absolute,
holds an empty, `.` or `..` part, a backslash, NUL or newline fails the check instead of being written.

In a run, the check follows the static stage's commands: its base is the run's `--base`, else the merge-base of the
commit with the base branch; semgrep runs in the static lane's scope, its lines go to `logs/static.log` and the
stage's failures, and a regression fails the stage. The rules and their profile are part of the static stage's reuse
fingerprint. `qg rules check` runs the same check by hand; `--config FILE` takes a YAML file holding only a `rules:`
mapping instead of the protected verify.yml, to try a repository that has not opted in. Both need `semgrep` on
`PATH` (the audit's pinned 1.183.0).

`enforce: false` is the rollout mode: in a run the static stage still checks, logs every regression and the summary
(marked `(advisory)`) to `static.log` and records the `qg rules check` command, but the check never fails the stage;
`qg rules check` still exits 1 on a regression.

**The burn-down.** `qg rules baseline` (and `qg init`) count the whole tree: every Python file read from git by blob
id, semgrep in a systemd scope with the static lane's default limits (`qg-rules-<id>-static-<n>`), the process at
nice 10 in the idle I/O class. Each count is stored as
`~/.local/state/qg/rules/<repository>-<hash>/<UTC time>-<sha12>.json` (the commit, the `rules:` it counted with, and
every rule, file and count), and the output gives the total and each rule's change since the previous snapshot of
the repository, noting when the rules configuration changed in between. The ratchet never reads these snapshots.

## Mutant hunt

With `mutants` in verify.yml, a passing backend stage is followed by the mutant hunt (`qg/mutants.py`, no LLM). For
every changed Python file under `stages.simplify.paths` (no test or tooling file), the lines the commit adds over
the merge-base get simple mutations: a comparison swapped (`==`/`!=`, `<`/`>=`, `>`/`<=`, `in`/`not in`,
`is`/`is not`), `and`/`or`, `True`/`False`, an integer +1, `return x` to `return None`. At most `mutants.max` of
them, one line at a time, run on a scratch worktree (`runs/<id>/mutants/src`) against the tests
`affected.backend.map` ties to that file (not the `always` list: its tests would kill every mutant without the
file's own tests saying anything). Only the operator or literal is rewritten in place, so format and comments stay;
an operator next to a parenthesis or a comment is left alone. Each set of tests runs once unmutated first; a mutant is killed only by a `FAILED` or `ERROR` line that run did not have, so a test that already fails or flakes kills nothing. A mutant those tests still pass is a gap in them. A
changed file that does not parse is named and skipped, and a crash of the hunt becomes its note. The hunt only reports: the
stage summary ends with "mutantes: 2 vivos de 12 (src/x.py:41 `>=` -> `<`; …)" and `runs/<id>/mutants.json` lists
each mutant with killed, survived or undecided; the stage's status never changes.

## Lite verification

`qg lite [--repo PATH] [--ref HEAD]` is the inner loop a coding agent runs between iterations: a foreground check of
one commit, typically seconds to a couple of minutes. It runs only the deterministic stages, one after the other:

- **static**: the commands of `stages.static`, stopping at the first that fails and printing its output tail;
- **rules**: the architecture rules ratchet of the commit against its merge-base with the base branch, when
  verify.yml has `rules:` (`path:line rule-id message` per new violation; with `enforce: false` they are shown as
  `info` and never fail);
- **tests**: the affected tests of the change (`affected.backend`) narrowed to the `lite.tests` globs and run with
  `lite.command`, when verify.yml has `lite:`; without it the line says the tests are not configured for lite;
- **mutants**: the mutant hunt on the added lines with the same narrowed tests, after the tests passed, when
  verify.yml has `mutants:` and `lite:`, bounded to 2 minutes; it only informs.

There is no Postgres container, no app stack, no agent (simplify, QA, audit, probe) and no Playwright. The commit is
checked out in a detached worktree under `~/.local/state/qg/lite/<id>/src` with the cached Python and node
dependencies (the same caches as the runner, built on a miss), and removed afterwards; uncommitted changes are never
checked, and the output says so when the worktree has some. verify.yml is read from the protected ref, as for a run.
Commands run in systemd scopes like the runner's (`qg-lite-<id>-<lane>-<n>`) with the lane limits exactly as
verify.yml configures them: lite adds no limit and no memory wait of its own. At most 2 lite checks run at once on the
machine (the others print `waiting for a lite slot` and wait), and lite runs at nice 10 in the idle I/O class. It never takes a runner slot, so a queued verification is never held behind it.

The output is one line per step (passed, failed, skipped or info, with its time), the lines to fix under a failed
step, and one summary line: `GREEN` (exit 0) or `RED: <steps> failed` (exit 1). Logs stay in
`~/.local/state/qg/lite/<id>/logs` for a day. Lite writes nothing under `runs/`: it is no verification, `qg check`
never accepts it and no run reuses it. `qg verify` remains the deploy gate.

## Nightly simplify

`qg nightly --simplify [--since REF]` adds S0 to a nightly run: /simplify reviews what reached the commit since
`--since` (default: the commit the repository's previous nightly simplify ran on, else the last commit older than 7
days) and publishes its fixes as `qg/simplified/<run>`, never merged, for the developers to take or leave: garbage
collection of the drift agents leave on master. It runs only when no verification waits, like every nightly run.
The suite of that nightly run verifies the simplified commit. Its report lists each change with why (the agent's
`summary.json`, redacted, kept as `simplify/explained.json`), the full diff, the findings the agent left alone, and how
to take it: `git cherry-pick <sha>`. A systemd user timer queues it every night (`qg-nightly-<repo>.timer`).

**The simplify queue.** Every S0 pass (nightly or `--simplify`) queues each `left_alone` finding that starts with a file of
the checkout in `STATE_ROOT/simplify-queue/<repo>.json`, out of `qg prune`'s reach; a note that names no file (how much was
reviewed) is not queued. `qg simplify-queue [--repo] [--all]` lists it and `qg simplify-decide <S-id> accepted|rejected
--why` decides an item. The next pass's prompt carries the queue: accepted items are fixed first (their file is in scope
even outside the diff) and cited by id, which marks them `done` once the commit is published; pending items are not
listed again; rejected ones are not proposed again. **Coverage**: the agent must not finish while a changed file under
`stages.simplify.paths` is unread and turns remain, and lists the rest in `unreviewed`; the report shows them, and the
stage summary says how many, how many items it queued, and when the agent wrote no `summary.json`.

**The nightly audit.** With a top-level `audit:` in verify.yml (`lenses: [security]`, `time_limit_minutes` 240), a
nightly S0 audits the night before the simplify agent runs, commit by commit and with no cap on cost, turns or
findings; its minutes are added to the simplify stage's timeout and are only a guard against a hung audit: reaching
them, or the 30 minutes one agent may take, fails the audit. First the deterministic tools, on the files the night
adds or changes under `stages.simplify.paths` (tests and tooling left out): ruff `S` and `PERF` (the repository's own
ruff config, extended) and semgrep with the registry packs `qg audit-rules` downloads into `STATE_ROOT/audit/semgrep`,
both kept only on lines the diff adds; gitleaks over the commits `base..sha` with `--redact`; osv-scanner over the
tracked lockfiles. Their hits are leads for the lens, never findings. Then, per lens, one confined agent per commit
(merges left out, a commit of more than 25 reviewed files split in parts of 25, a commit with none listed in the report
as without code), on Sonnet (`LENS_MODEL` in `qg/audit.py`), with read-only access to the checkout at the night's tip
(`qg/audit_<lens>_prompt.md`): it reads the commit's message and diff and every file of it in full as it stands at the
tip, and gets the tool hits on those files (the first part also those on the files the commit changes outside the
review, such as a lockfile). A file a lens
leaves unread is given to it again, up to 3 runs in all. A part still unread, or whose findings file is malformed, is
left out of the ledger and the lens goes on with the next parts; after the last one the audit fails, naming each part
left unfinished and why, and the next night reads those parts again. The
findings carry file, line, symbol, rule, severity, the evidence path and a one-line fix. As each part ends, its findings
deduplicated by file, rule and symbol, less those already in the queue or judged earlier that night, go to a refuter,
another confined agent on the default model that sees the claims but not the lens's reasoning
(`qg/audit_refute_prompt.md`) and tries to disprove them in batches of 8. What it confirms goes to the simplify queue
at once with its lens, severity, evidence and fix, identified by lens, file, rule and symbol, so a rejected finding is
never queued again however it is reworded; what it refutes, and what it left unjudged, goes only to the report. A part
read to its last line whose findings were all judged is then recorded in the audit ledger
(`STATE_ROOT/audit/ledger/<repo>-<hash>.json`: lens, commit, part, files, run, date) and no later night reads it again,
even when a later part, a later stage or the whole run fails or is cancelled; since a failed night keeps its base, the
next one starts at the first part the ledger lacks, so a normal night costs only the new commits' agents. Each agent
writes only its own fresh directory under `runs/<id>/audit/` and its file tools reach only that, the checkout and the
directory it reads (`--add-dir` each); qg's own files there (tool hits, commit patches, claims, `audit.json`) are
read-only to every agent and out of the simplify agent's reach, and the refuter is shown only the claims. Each agent
renews the Claude login for its own time. `qg simplify-queue --lens security --severity P0,P1` narrows the list. An
accepted audit item is fixed by the next simplify pass like any other. A part left unfinished, a malformed verdicts file, a missing
tool or rule pack, or the time guard fails the simplify stage. The audit only informs: no finding blocks a deploy.
Tools, pinned: semgrep 1.183.0, ruff 0.17.3, gitleaks 8.30.1, osv-scanner 2.7.0 (and, for later lenses, vulture 2.16,
radon 6.0.1, pip-audit 2.10.1, jscpd 5.4.2, knip 6.44.1), in `~/.local/bin`.

## S0, simplify

Runs first, in the sandbox checkout of the requested sha, only when there is a base. The headless Claude launcher, the
bubblewrap jail, the fresh HOME and the systemd scope are S5's; the agent has only Read, Edit, Write, Glob and Grep (no shell,
no browser, no MCP), the working directory is the checkout, `--add-dir` is `runs/<id>/simplify/` (the full
`git diff base..sha`, its summary, its output) and the jail binds nothing else read-write. S0 never takes a host `flock`
and never reaches the host checkout. The prompt is the `/simplify` review (reuse, simplification, efficiency, altitude)
limited to the diff, plus the repo's CLAUDE.md. The `simplify` skill is not assumed to exist in a jailed, settings-less
session, so its four lenses are embedded in `qg/simplify_prompt.md`. The agent runs no tests: the later stages run the suites on
its result, and a commit that breaks one fails that stage like any other.

What S0 may change is enforced in code after the agent ends, never by the prompt alone, and a violation fails the stage
(nothing is dropped silently, nothing is committed, `simplify/rejected.diff` keeps the attempt and the report shows it):

- a file must be in `git diff base..sha` and under `stages.simplify.paths` (default the app source directories);
- never a test, `tests/`, `__tests__/`, `e2e/`, `*.test.*`, `*.spec.*`, snapshot, fixture, `conftest.py`, `quality-gates-notes.md`, `.claude/`, `.github/`, `.git*`, lockfile;
- no new file, no file the diff deleted, no symlink (nor a path through one), no submodule, no mode change, no added, removed or
  changed ignored file, no rewrite of the sandbox's `.git` file; the sandbox must be clean before the agent runs;
- path rules compare NFKC-casefolded components (`Tests/`, fullwidth spellings, `Test_x.py`, `Makefile` are protected too);
  the commit must be the requested sha plus exactly the checked files and leave the sandbox clean; the executor asserts at
  the end that the sandbox's HEAD tree is the tree the run recorded as verified; `stages.simplify.paths` comes from the
  protected master `verify.yml`, never the branch's.

Outcomes: no change passes with "nothing to simplify" (sha and tree unchanged). Changes become one commit on top of the
requested sha (message `simplify: ...`, the agent's one-line changes and the files, last line `qg run <id>`), created in the
canonical repository as the **new** ref `refs/heads/qg/simplified/<run-id>` (an `update-ref` with the null old value from the
sandbox, which is a worktree of that repository, so it can never move an existing ref; qg writes no other ref, and never a
branch checked out in a worktree). `state.json` keeps `requested_sha`/`requested_tree` and sets `sha`/`tree` to the
verified commit; report.md and report.json carry `requested_sha`, `verified_sha` and `verified_tree`, the report says "merge
`qg/simplified/<run-id>` into `<branch>` (fast-forward) before deploying" and shows the S0 diff. `qg check` gates on the
verified tree, so it passes only after that fast-forward. If the agent fails or times out the stage fails and the run is NOT
READY. The deduplication of `qg verify` is by the requested tree and the stage set: a tree whose READY run found nothing to
simplify is not queued again, and after the fast-forward the new tip's tree is the verified one of that run.

## Stage reuse

A queued run does not repeat what an earlier run of the same repository and env already proved. The executor decides,
stage by stage, just before the stage would run (so a source that finished while the run waited counts). A source is
any earlier run with the hardened provenance (see *Verdicts*) whose status is passed, failed, cancelled, interrupted or
error, for the same repo and env, the same **requested tree** and the same **base**. Newest first; the first one that
proves out lends the stage. `qg verify --force` and `qg retest` reuse nothing; `qg resume` and a plain `qg verify` do.

- **S0 simplify** is reused only if the source's S0 passed and, when it committed, its `qg/simplified/<id>` ref still points
  at the recorded sha, that commit's tree is the recorded one and its parent is the requested sha (the requested sha
  itself, not only its tree). With nothing simplified the verified commit must be the requested one. The sandbox then
  checks out the verified commit and every later stage verifies that same tree. If the checkout fails S0 runs.
- **static, backend, frontend, playwright** are reused only if the source stage passed **and** its fingerprint equals the
  one the stage would have now: the verified tree, the stage's commands from the protected config, the dependency and
  Postgres configuration, the env allowlist and, for Playwright, the selected specs and the run command. A fingerprint
  is stored on every stage a run executes; a stage of an older run has it rebuilt from the run's own request and state
  (not possible for qa, which is then never reused).
- **qa** is reused only with the same fingerprint, which adds the whole QA configuration (scaled limits, accounts,
  boot) and the **plan text**, and only from a run of the same cycle with its `qa.json` on disk. The walked flows,
  findings and evidence are copied into the new run (paths rewritten to its directory).
- A reused stage keeps the source's summary, counts, specs, flows, cost and turns, and records `reused_from=<run>` and
  its fingerprint. `state.json`, `report.md`, `qg wait`, the cycle page and a topic page show it as
  `passed (reused from <run>)`.
- Anything uncertain re-runs the stage: no source, an unreadable source, a ref that moved, another tree, base or
  config, a fingerprint that cannot be rebuilt.
- `qg check` (and every verdict) accepts a run built from reused stages only while each source still exists as a trusted
  finished run of the repository, passed that very stage itself (or by a reuse that stands, recursively) and verified
  exactly the same tree; otherwise the reason is listed and the run is NOT READY. Retention never deletes a run another
  run reuses.

## Verdicts

A run is judged against a policy, never against a stored flag. `deploy.<env>` demands its command stages `passed` (a
`skipped` stage never counts), a finished QA stage when it lists `qa`, and at most `max_p0` open P0 findings.
READY means the policy is satisfied; otherwise NOT READY, with the reasons in the report. Operator decisions of
2026-10-21 (decisions D7-D11):

- A P0 blocks until the P0 verifier refutes it (see S5); one it confirmed or could not judge keeps blocking, and the
  reason names the ones still `sin verificar`.
- An open P1 blocks until the operator acknowledges it (`qg ack`). `p1_acknowledged` is still read from old configs and
  gates nothing on its own. P2 and P3 never block; the session closes them with `qg fixed`, without a run.
- A QA that walked nothing blocks (operator, 2026-10-21, D12): a run, or a phase of a release, whose QA ended without
  walking a single flow of its plan (stack not ready, agent dead, account, cancelled) is NOT READY with `sin QA:` and
  the reason. A phase run of a release that ends so is retried once automatically (`release.retry`: a fresh walk of
  the same plan, `retest_of` the first run; the release waits for it and the newest run of the chain stands for the
  phase). A QA that walked part of its plan and then failed only warns: `QA incompleta` under "Avisos". A QA stage
  still pending or running blocks until it ends.
- A release still blocks on its integrity: no phases declared, a declared phase with no run or several runs that are
  not one retry chain, a run of an undeclared phase, a phase run on another tree, untrusted, or whose reuse does not
  stand.
- Warnings (`report.json` key `warnings`, section "Avisos" of `report.md`, the cycle page header and `qg check`'s
  output) never change the verdict.

- `qg verify --env X` asks for the stages of `deploy.X` (without `qa` unless `--prd` and `--phase` are given, and never under
  `--no-qa`). A verification that left `qa` out is judged on the other stages of `deploy.X`: READY when they pass, with
  the warning `QA not run: ...`; `qg check` keeps judging it under the whole policy. It prints the existing run
  and queues nothing when a finished run of the same repo and tree already satisfies that stage set.
- `qg check <sha> --env X` evaluates the policy of X on the stage results of the runs of that tree, so a run
  requested for prod whose S1-S4 passed satisfies `--env staging` even though its own prod verdict is NOT READY.
- A run whose state lacks the provenance fields of the hardened runner (a 40-hex `config_sha`, `config_ref`, tree and sha,
  a canonical absolute repo path) is legacy: `qg check` and the reuse in `qg verify` ignore it, so it never satisfies a
  deploy.
- A run's own verdict (`qg wait`, `report.md`) is the one under the env it was requested for.

## Cycles and the published pages

A **cycle** groups every run of one feature: the key is repo + branch + feature name (whitespace collapsed, case
ignored). The first `qg verify` of a feature opens it; every later `verify` or `retest` of the same key appends a
run (attempt 2, 3, ...), also after it reached READY. `qg retest <id>` inherits the feature. A run superseded
before it started stays in the cycle's list but is not an attempt.

`cycles/<cycle-id>/cycle.json`: `{id, repo, branch, feature, plan, runs: [run ids in order], status: open | ready,
artifact_url: null | "https://claude.ai/artifact/...", created, updated}`. The status is `ready` when the latest
finished attempt satisfies its own env policy and no run of the cycle is queued or running.

**What is published is one page per topic, not the cycle page** (see *Topic pages*). The cycle page below stays a local file for
debugging.

After every run finishes, qg freezes it as an attempt (`attempts/<run-id>.json`, so run retention never erases
history) and regenerates `cycles/<cycle-id>/page.html`, one self-contained page in Spanish: header with the
feature, repo and branch, latest sha and tree, the verdicts for prod and staging, attempts and total time; figure
tiles (attempts, wall time, tests per suite, Playwright specs, findings open and fixed); the attempts timeline
(what failed, the first failing tests or P0 titles, `git log --oneline` between attempts, duration); the stages of
the latest attempt; "Pruebas" (the S4 specs and flows, the S5 flows walked with widths and locales); "Hallazgos"
(P0 and P1 with status open, fixed in attempt n, or acknowledged with the operator's note, steps, expected,
actual and evidence); "Mejoras" (P2 and P3); and the report and log paths.

Evidence screenshots are embedded as JPEG data URIs at most 1280 px wide, 8 MB per page, converted with `ffmpeg`
or ImageMagick (`magick` or `convert`); without one of them, or beyond the budget, they are listed by path
(`qg doctor` reports which tool it found). The page is an artifact fragment: it starts at `<title>` (the feature
name) and has no doctype, `html`, `head` or `body`, because the Artifact tool wraps it. It follows the artifact
page contract: colours as tokens on `:root` with the two dark-mode blocks, an explicit body background, Google
Fonts as the only external resource, and no horizontal scroll at 390 px.

### What the page, report.md and report.json may contain

Every text that reaches them goes through `qg/redact.py`: the run's secret values (the `secrets_keys` read from
`secrets_env_file` and every `stages.qa.accounts` password) become `[redacted]`, as do `Authorization`/`Bearer`
values, `password`/`secret`/`api_key`/token assignments (`password=x`, `"password": "x"`), `sk_`/`pk_`/`whsec_`/
`polar_`/`xox` style keys, JWTs, `enc:v1:`/`enc:v2:` blobs and URL userinfo (`scheme://user:pass@`); this
includes the Postgres password in connection URLs and `POSTGRES_PASSWORD=` assignments (the bare word `postgres`
is not blanket-replaced). Emails are masked to their first letter (`t***@example.com`). The oracle's backend-log
excerpts (`evidence/backend-<n>.txt`) and the findings' title, area, steps, expected and actual are redacted when
written, and everything is redacted again at render (the page, via its escape function) and in report.json and
report.md. The page never renders account emails, roles or any other `stages.qa.accounts` value, nor config values.
Not covered: text inside screenshots, and a secret in a form no pattern or configured value matches.

### Topic pages

The operator reads only manual-test results, one page per topic like a ticket. A topic is one user-facing change of the plan:
each `plan:<name>` flow S5 walked. qg writes `cycles/<id>/topics/<slug>/page.html` for each, an `index.html` of them and
`cycles/<id>/topics.json` (slug, name, verdict, `artifact_url`). **No page exists until S5 has walked the topic**: a run that
stops before S5 writes none. A page holds: what the change is (one paragraph), the verdict (works, works with improvements,
broken, blocked and why; derived from the walk and the open findings), the walk as a sequence of screenshots, one per step at
1280 px and at 390 px, each with a caption, and the topic's findings P0-P3 with their screenshot (an improvement says what
should change, anchored to its screenshot). It never lists failing tests, test counts, commit logs, stage tables or durations;
an attempt that stopped before S5 adds one line, "attempt n stopped at <stage>: <one-sentence reason>". The 8 MB image budget
applies per page; the redaction of the cycle page applies to every text.

### Publishing procedure for a Claude session

qg cannot reach claude.ai, so the session that asked for the verification publishes the pages. One topic, one URL, forever.

1. Start the run: `qg verify --feature "<feature name>" [--base SHA] [--plan PATH] [--env staging]`. Keep the feature name
   identical on every attempt. Follow it with `qg wait <run-id>` in the background.
2. When the run ends (READY or not, also `error`), run `qg topics <run-id>`. For each topic it prints:
   ```
   topic: <slug>
   name: <topic name>
   verdict: works | works with improvements | broken | blocked
   page: /home/<user>/.local/state/qg/cycles/<cycle-id>/topics/<slug>/page.html
   artifact: none | https://claude.ai/artifact/<id>
   ```
   With no topic it says so: no page exists.
3. Publish each `page:` file with the Artifact tool, unchanged:
   - `artifact: none`: publish with `file_path` set to the page, an `icon` such as `checklist`, and a one-sentence
     `description` of the topic; then store the URL the tool returns: `qg topic-artifact <cycle-id> <slug> <url>`.
   - `artifact: <url>`: read that artifact first (`action: "read"`), then publish the same page with `url` set to the stored
     URL. Omit `icon`. Nothing needs storing.
4. After each later attempt, repeat steps 2 and 3. A page is rebuilt from the frozen attempts; `qg topics <run-id>` regenerates
   a stale one.

### Files the QA stage writes (read by `qg ack`, `qg check` and the page)

`runs/<id>/findings.json`:

```json
{"findings": [{"id": "F-001", "severity": "P0", "title": "...", "area": "bookings / calendar",
  "steps": ["..."], "expected": "...", "actual": "...", "evidence": ["/abs/shot.png"],
  "status": "open", "acknowledged": null, "verification": null}]}
```

`severity` is P0 to P3; `status` is `open` or `fixed` (a retest marks a finding `fixed` when it no longer
reproduces, `qg fixed` a P2 or P3 the session fixed, recording `closed`: `null` or `{"who": "session", "when": "...",
"commit": "<sha>", "note": "..."}`); `acknowledged` is `null` or `{"who": "...", "when": "...", "note": "..."}`; `verification` is `null` or
`{"verdict": "confirmed|refuted|error", "reason": "...", "category": "...", "filed": "P0", "when": "..."}`
(see the P0 verifier in S5); evidence paths are
absolute or relative to the run directory, and only a regular image file (`.jpg` `.jpeg` `.png` `.webp`, at most
10 MB) whose real path is inside `runs/<id>/evidence/` is read; any other path (`..`, absolute, symlink out) is listed
as `rejected evidence path` and never opened. Across attempts findings are matched by `id`: the first mention gives
the description, every mention adds evidence, the latest decides severity, status, acknowledgement and verification.

`runs/<id>/qa.json`: `{"flows": [{"name": "...", "widths": [1280, 390], "locales": ["es"], "result": "...",
"summary": "", "steps": [{"caption": "...", "width": 1280, "locale": "en", "screenshot": "/abs/evidence/x.jpg"}]}]}`.
A plan flow (`plan:<name>`) has `summary` and `steps`; a finding may carry `flow` to name its topic.
Evidence may also be a `.txt` file (the backend-log excerpts), shown on the page as a `<pre>` block under the same
realpath restriction.

## S5, the manual QA pass

Runs in the `qa` slot after S4 passed. The executor waits until no other run holds the slot, takes it (which frees the
`auto` slot for the next queued run) and then:

1. **Boot.** `stages.qa.boot` (placeholders filled) starts in the worktree in its own process group, with the stage
   environment plus **only** the `secrets_keys` read from `secrets_env_file` (KEY=VALUE lines, no shell). Output goes to
   `logs/qa-stack.log`. Ready when the log prints `VERIFY_STACK_READY` or `ready_url` answers; 10 minutes at most,
   counted once the run holds the machine-wide boot lock (`qa-boot.lock`): no two QA stacks boot, seed or build at the
   same time (2026-10-21: concurrent seeds timed out on the shared Qdrant and `pnpm`/vite builds starved each other).
   While it waits, `qg status` shows `qa boot lock`; the wait is bounded by what the stage has left.
2. **Charter.** `runs/<id>/qa/charter.md`: feature, "Plan changes to walk" (the agent derives every user-facing change from the
   plan, walks each at 1280 and 390 px in at least two locales, and records each as its own flow `plan:<short name>` with
   `walked`, `blocked` or `not reachable`, a summary and a screenshot of every step at both widths, in addition to the configured
   flows; a run that records no `plan:` flow fails the stage; a `blocked` or `not reachable` flow, plan or configured, with
   its reason in `notes`, never fails it (a blocked plan topic is kept as a P2 coverage note `not walked: blocked or not
   reachable`, and a plan whose only flow is `plan:no user-facing change` is complete); the charter
   also states the turn, time and cost budget and asks the agent to rewrite `findings.json` after every topic, so a run
   stopped by a cap keeps its walk), the plan file (`--plan`, read from the sandbox when inside the
   repository), `git diff --stat` and the changed files since the merge-base, the surface-map flows the diff touches, the
   critical specs as journeys to walk by hand, the accounts and URLs, widths 1280 and 390 and locales en/es/vi, the
   project notes file, and the findings still open or acknowledged in earlier attempts of the cycle (re-check first, with
   the next free `F-nnn` id).
3. **Agent.** `claude -p <qg/qa_prompt.md> --model opus --max-turns N --max-budget-usd CAP --output-format json
   --restricted --strict-mcp-config --mcp-config=qa/mcp.json --tools=Read,Write
   --allowedTools=mcp__playwright,Read,Write(findings.json)` with cwd `runs/<id>/qa`, no user or project settings, no
   hooks, no shell. The MCP is `npx -y @playwright/mcp@0.0.41 --isolated --headless --viewport-size 1280x800
   --output-dir runs/<id>/evidence` (`--isolated`: the shared `~/.cache/ms-playwright-mcp` profile is never touched).
   Time limit: the smaller of `time_limit_minutes` and what is left of the stage timeout; SIGTERM, 30 s, SIGKILL for the
   whole process group, which also ends the browser. The agent is a tester: it records findings P0 (broken, 5xx, wrong
   data, dead end), P1 (misleading, stuck), P2 (unclear, needless step), P3 (cosmetic) and writes `qa/findings.json`:

   ```json
   {"walked": [{"flow": "...", "widths": [1280, 390], "locales": ["en"], "result": "walked", "notes": "..."}],
    "findings": [{"id": "F-001", "severity": "P1", "area": "...", "title": "...", "steps": ["..."],
                  "expected": "...", "actual": "...", "evidence": ["evidence/F-001-x.jpg"]}],
    "improvements": [{"id": "I-001", "severity": "P3", "area": "...", "title": "...", "steps": [], "expected": "",
                      "actual": "", "evidence": []}]}
   ```
4. **Oracles** (added by qg, not the agent), over the stack log written while the agent ran: every distinct Python
   traceback (chained ones as one), every distinct ` ERROR ` line and every HTTP 5xx access-log line becomes one P0
   finding, area `backend log`, id `L-<hash of the signature>` (stable across attempts), evidence
   `evidence/backend-<n>.txt`. An ERROR line that heads a traceback is part of it. The P0 verifier (step 7) marks the
   ones that repeat another finding's root cause as `duplicate`.
5. **Validation.** Plain checks; a missing file fails the stage with `qa agent produced no findings file`. An invalid
   file gets up to 3 repair turns; one still invalid is read leniently: every valid `walked`, `findings` and
   `improvements` entry is kept and only the invalid ones are dropped, the stage problem saying `findings.json: N
   invalid entries dropped: <first error>` (a file that is not JSON, or not an object with its lists, still fails).
   An account the CLI reports as limited or with subscription access disabled is handed to the rotator and the agent
   retried once on the next account; a login file caught mid-rotation is re-read for up to 150 s before failing. Evidence names become absolute paths inside `evidence/`. The merged list is written to
   `runs/<id>/findings.json` (improvements are P2/P3 findings) and the walked flows to `qa.json`. A finding of an earlier
   attempt of the cycle that is open or acknowledged and is not reported again is written as `fixed` (the page says
   "corregido en el intento n"); an acknowledged P1 that is reported again keeps its acknowledgement.
6. **P0 probe and flip** (`qg/probe.py`, prompts `qg/probe_prompt.md` and `qg/flip_prompt.md`; only when verify.yml
   has `probe`, D13). After the stack is stopped, first every P0 carried from an earlier run with an accepted probe is
   re-run 3 times on this commit (a scratch worktree with the earlier run's probe file copied in): green in all 3
   (its one test passed) marks it `fixed` though the QA agent never saw it again, the same failing test in all 3
   keeps it open whatever the agent said; runs that disagree (a flaky probe) or an error (the probe no longer runs
   cleanly) leave what the QA stage recorded. Then each open P0 without a probe, at most `probe.max_p0`, gets a
   scratch worktree of the commit (`runs/<id>/probes/<finding>/src`, removed afterwards) where:
   - a **probe agent** (`claude -p --model opus --tools=Read,Edit,Write,Glob,Grep`, no shell, the same jail, the
     finding's card, the plan and its evidence read-only in `probes/<finding>/card/`) writes exactly one new pytest
     file, `<probe.dir>test_qg_<finding>.py`; any other change ends the probe. qg runs it with
     `affected.backend.command` on that file, on the QA lane's ports and Postgres in the `qa_stack` scope. It is
     **red** only with exit 1, exactly one `FAILED` line of that file, no `ERROR` line and no passed test, and only
     when two more runs fail the same test (a probe that does not repeat is not deterministic); anything else is an
     error, and the next agent (up to 3) is told why with the output. Each agent's time leaves room for those 3 runs;
   - on red, a **flip agent** edits product files under `stages.simplify.paths` (never tests, fixtures, conftest,
     tooling, lockfiles, seeds or the probe) until the probe passes and then the tests `affected.backend` maps to
     the changed files pass (up to 3 attempts). A flip with which the probe passes but existing tests fail, and no
     attempt reaches green, is kept as `contradicts` with those tests named: they may assert the defect itself
     (the verifier decides). A green or contradicts flip's diff is `probes/<finding>/flip.patch`; it is never
     committed, merged or published as a ref. Each agent's .git file and ignored files are compared right before
     and after it runs, so the caches qg's own test runs write are never held against it;
   - for a green or contradicts flip, a **held-out variant**: a third agent, on a fresh scratch worktree
     (`probes/<finding>/variant-src`), writes `<probe>_variant.py`, a second test of the same defect with other
     data, seeing the first probe but never the flip; it must be red 3/3 like the probe. qg then applies
     `flip.patch` there (dropping the changed files' bytecode) and runs it: a flip with which the variant still
     fails is `special-case` ("flip que solo arregla el caso del probe"), since it fixes the probe's data rather
     than the defect. It only informs: the P0 blocks either way. No clean variant leaves the flip as it was, with
     the reason.
   Each agent gets at most `probe.max_cost_usd` over its attempts and the whole pass `probe.time_limit_minutes`
   (added to the run's deadline); a P0 left out gets "sin reproducción" with the reason. The finding's `probe`
   records status (red, error, none), reason, file, flip and the reviews; a P0 without a red, accepted probe keeps
   blocking (D13). To turn it on in a repository: `probe.dir` must be a directory the repository's
   `affected.backend.command` really runs (a command that drops arguments under `tests/` needs its probes
   elsewhere, for example `src/tests/qg_probes/`), and `__pycache__` and `.pytest_cache` there must be
   gitignored, or the probe agent's next attempt is refused for the cache files pytest wrote. Then add `probe:` to
   the master `verify.yml`, merge qg and restart `qg-runner`. The stage summary adds `probes: 1 rojos de 1 P0; flips: 0 en verde, 1 que contradicen tests existentes`.
7. **P0 verifier** (`qg/verifier.py`, prompt `qg/verify_prompt.md`). After the stack is stopped, every open P0 of
   the run that no verifier has judged (the agent's, the oracles', the `W-` flow failures and the unjudged P0s carried
   from earlier attempts) goes to one independent agent: `claude -p --model opus --tools=Read,Grep,Glob
   --add-dir <worktree>`, in the same bubblewrap jail with a fresh copy of the login, its working directory
   `runs/<id>/verify/` (read-only: `p0.json`, `plan.md`, `charter.md` and the evidence files of this run the findings
   cite; nothing outside the run's own `evidence/` is ever copied), the worktree of the commit read-only, 15 minutes
   (`config.VERIFY_MINUTES`, added to the run's deadline), 80 turns, $5. It tries to refute each P0 and answers
   `confirmed`, or `refuted` with a category (`harness`, `designed`, `duplicate`, `unreachable`, `minor`,
   `unsupported`) and the severity it deserves (P1-P3). A refuted P0 takes that severity; the finding's
   `verification` keeps the verdict, category, reason with its file:line references, the severity filed and when. A
   confirmed P0 keeps blocking; so does one it could not judge (it timed out, could not start, gave no or an invalid
   answer), with that reason. The stage summary ends with the tally (`verificador: 1 P0 confirmados; 3 refutados
   (duplicate 3)`). A P0 with a red probe also carries the probe file, its output and the flip patch: the verifier
   accepts or rejects the probe (it must fail because of exactly this defect, through the product as shipped) and
   the flip (no special case of the probe's data, no weakened check, the cause rather than the symptom; for
   `contradicts`, every failing test must assert the defect itself); only an
   accepted probe is rerun by later retests, and the report and the cycle page say "reproducido por …", "sin
   reproducción: …", "flip propuesto en …/flip.patch" or why not. Up to 3 earlier operator reviews (`qg
   probe-review`; those that overruled a verifier first, then the newest) are copied to `verify/examples/` so the
   verifier holds itself to the operator's standard. `qg verify-p0 <run>` does the same for a
   finished run on a temporary worktree of its commit; it makes no probes.
8. **Teardown.** The stack is stopped (SIGTERM to the group, 30 s, SIGKILL) before the run's teardown commands.

The stage **fails** when the agent timed out, hit the turn cap, exited with an error, or left no valid findings file,
or the **cost cap** was exceeded: `stages.qa.max_cost_usd` (default 15) is passed to claude
as `--max-budget-usd` and checked against `total_cost_usd` in its JSON output; the summary then says
`cost cap exceeded ($x > $y)`. Cost and turns are stored on the stage (`cost_usd`, `turns` in `report.json`) and shown in
the summary and on the cycle page. Findings found by a failed stage are still recorded. A failed QA stage that walked
part of its plan is a warning (D9); one that walked nothing blocks (D12). The rest of the verdict comes from the
findings: no open P0 that the verifier did not refute (D7).

**Infrastructure failures** are not a verdict on the code. They are typed where they happen (`qg/proc.py`) and end
the run `error` (never `failed`), its error reading `infrastructure, not the code: <cause>`:

- `StackFailure`: the QA stack was not ready after 10 min or exited, died while the agent ran, or the agent left no
  readable JSON result. A plain verification that ends so is queued again once, at once (no hold), with the same
  request as a retest of the failed run (`retest_of`), so the stages that passed are reused (*Stage reuse*); its error
  ends `; queued again as run <id>` and only the retry's end is announced to antfarm. A run that is itself a retest,
  resume or retry is not queued again, nor one with a newer run of its line (it would supersede that run).
- `AccountFailure`: the claude CLI said the account cannot run the agent (a usage limit, with its reset time as the
  CLI prints it, or an organisation that disabled subscription access) after the one switch to another account, or
  the login is missing or too short and did not renew. Never queued again; it ends S0 and the nightly audit too, and
  a phase run that ends so is not retried by `release.retry`.

When a stage of the run already failed on the code the run ends `failed` as before, and it is not queued again.

Looking at the evidence: `runs/<id>/evidence/` holds the JPEG screenshots and the `backend-<n>.txt` excerpts; the cycle
page embeds them (screenshots as thumbnails, excerpts as `<pre>`). Also under the run: `qa/charter.md`, `qa/mcp.json`,
`qa/findings.json` (the agent's own file), `qa/claude-output.json`, `logs/qa-stack.log`, `logs/qa-agent.log`.

## Resources and cleanup

- Four slots machine-wide, `auto` to `auto4`: a run holds one from setup to its end. Admission (`qg/admission.py`): a run
  starts only when a slot is free, the last run started has had 20 s to take its memory, and MemAvailable
  (`/proc/meminfo`) is at least its need. With a measured peak (the biggest recent one of its lane) the need is peak x 1.25
  + 1 GiB, never above `memory_max` + 1 GiB; with no history it is min(`memory_max`, 4 GiB) + 1.5 GiB (`infinity`
  counts as 4 GiB). The need is recomputed on every pass, so queued runs
  see a changed formula or new peaks once the runner restarts. Queue order is FIFO, except that when the head does not
  fit, a later run whose setup fits starts first; a nightly run starts only when no verification waits, a held one
  included (see `qg verify`), and is never preempted for a run still held. A run alone on the
  machine always starts (limits are caps, not use). Once two runs run, the younger run's stages (`static`, `tests` and
  `qa_stack` lane limits) wait for the same MemAvailable need; the oldest running run never waits, so there is no
  deadlock. Two runs never run their Playwright stage together: a stage takes `playwright.lock` first. Nothing is killed by
  qg's choice; the reasons show in `qg status`. The run's deadline keeps counting while it waits.
- The executor of a run starts as `systemd-run --user --scope --unit qg-<id> --collect -p CPUQuota=<cfg>
  -p MemoryMax=<cfg> -p TimeoutStopSec=300 nice -n 10 ionice -c2 -n7 qg _execute <id>`. The executor stores its pid in the run's state; `qg cancel` stops the
  scope, and a runner restart does not kill a run in flight (the new runner re-adopts it while that pid is alive).
  A run whose executor pid is gone is finished and its scope stopped.
- **In place.** `qg verify` run from a worktree (`--repo`, default the current directory) whose HEAD is the requested commit works on that path directly: no `git worktree add`. At `qg verify` time a dirty path (tracked changes, or untracked files outside gitignored paths) is refused ("commit or stash first; qg verifies a commit"). When the run starts, under the path's lock (`qg-inplace.lock` in the worktree's own git dir; two runs on one path serialise, the run's QA and Playwright share it), HEAD must still be the commit and the tree clean, else the run gets a detached worktree. `--ref` of another commit, `--simplify`, `nightly`, retest and release runs always use the detached worktree. Dependencies link `node_modules` into the worktree (it must be gitignored; one already there is moved aside under `runs/<id>/aside` and put back); at the end HEAD and `git status` are compared with the start, and a change makes the run `failed` ("el worktree cambió durante la verificación"), never READY. Teardown removes only what qg added and never the worktree. Gitignored output the stages write (caches, `test-results`) stays.
- Teardown always runs in a `finally` (its subprocesses get default signal handlers): the `teardown` commands, then
  containers named `qg-<id>-*`, the run's own worktree (`git worktree remove --force`; if the directory is already gone, only
  that run's entry under `.git/worktrees` is removed, never `git worktree prune`) and stack processes. The janitor, at runner start and every 10
  minutes, finishes runs whose executor is gone (`interrupted`, or `cancelled` after `qg cancel`), tears down
  leftovers, stops the scopes of finished runs and applies retention: the newest five finished runs per repo+branch
  (superseded and cancelled runs do not count toward the five), nothing older than 14 days, never the newest READY
  run of a repo+branch, and cycles that are idle for 14 days with no runs left and no artifact URL. A failure while
  handling one run marks that run `error` and the daemon goes on. The janitor also deletes venv and node caches
  unused for 14 days and `.tmp-*` install directories older than an hour.
- Ports are free only if `127.0.0.1`, `::1` and `0.0.0.0` all bind.
- Its own lock (`queue.lock`); it never touches `/run/user/1000/gpu-heavy.lock`.

## License

MIT. See [LICENSE](LICENSE).
