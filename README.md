# qg

qg verifies one commit in its own sandbox before it is deployed. `qg verify` queues the commit; a runner daemon
checks it out in an isolated worktree with cached dependencies, its own ports and a throwaway Postgres, runs the
deterministic checks first (static analysis, an architecture-rules ratchet, the affected tests, Playwright) and, when
a phase of work is closed, a headless QA agent that walks the change in a real browser. The run ends with a verdict,
READY or NOT READY, for the commit's **tree**. Deploy scripts call `qg check` and refuse a tree without a passing
run. qg is an operator-driven gate for code written by coding agents: the agents iterate, qg decides what may ship.

Python 3.12, `uv`, no dependency but PyYAML. Linux only. This tool has no tests of its own, and nobody runs a
repository's tests by hand: they run here, in the runner, once the operator activates it.

- [How it works](#how-it-works)
- [What you need to make it work](#what-you-need-to-make-it-work)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

## How it works

### The life of a run

```
qg verify ──> queue ──> admission ──> setup ──┬──> static lane: S1 static + rules ratchet
 (CLI)       (state    (slot, memory,  (worktree, │
              root)     disk)          deps,      └──> tests lane: S2 backend ─> S3 frontend ─> S4 playwright
                                       Postgres)                                                  │
                                                         qa lane (phase close or release only) <──┘
                                                         QA Postgres ─> stack boot ─> S5 QA agent ─> probe / verifier
                                                                                                    │
            teardown <── verdict READY / NOT READY (report.md, report.json, cycle) <────────────────┘
  (stacks, teardown commands, services released, containers, worktree)
```

1. **Queue.** `qg verify --feature "<name>"` resolves the repository, branch, sha and tree, reads
   `.claude/verify.yml` from the **protected ref** (`master`, else `origin/master`; never the commit being verified),
   computes the stages the env policy asks for, and writes the run under `~/.local/state/qg/runs/<id>/`. A newer
   commit of the same repo and branch supersedes a run still queued. A plain verification waits 120 s in the queue
   so a newer commit can supersede it before it boots anything.
2. **Admission.** The runner (`qg runner`, a systemd user service) starts a queued run when one of four slots is
   free, the run started last has had 20 s to take its memory, `MemAvailable` covers the run's measured need, and
   the state root has at least 10 GiB free. Nothing runs until the operator has run `qg enable`.
3. **Setup.** The executor starts in its own systemd scope. It uses the given worktree in place when it is clean at
   the commit, else a detached worktree of the sha. It links the cached Python venv and node packages, picks free
   ports in 21000-21999 and starts a Postgres container (`qg-<id>-pg`) on tmpfs.
4. **Lanes.** The static and tests lanes start together, each command in a systemd scope with the lane's CPU and
   memory caps. The static lane runs `stages.static` and then the rules ratchet. The tests lane runs backend
   (the affected pytest selection), frontend (vitest related), then Playwright (critical specs plus the specs the
   surface map ties to the changed files). The QA lane runs only when a phase is closed (`--prd` and `--phase`) or
   at release. It starts when the tests lane has ended, with its own ports and Postgres (`qg-<id>-qapg`): it boots
   the app stack, runs a jailed Claude Code agent with a Playwright MCP browser, then the P0 probe and the P0
   verifier. A failed stage never stops another stage or lane, so one run reports everything that is wrong.
5. **Verdict.** The run is judged against `deploy.<env>` of verify.yml: the listed stages passed, no blocking
   finding (an open P0 the verifier did not refute, an open P1 the operator did not acknowledge). The result goes to
   `report.md`, `report.json`, the run's cycle and the topic pages.
6. **Teardown.** Always, in a `finally`: the app stacks stop, the `teardown` commands run (the run's Postgres still
   exists), the run's hold on its `services` is released, the `qg-<id>-*` containers are removed, and the detached
   worktree is removed (a worktree used in place is kept; only what qg added is removed).
7. **Deploy.** The deploy script runs `qg check <sha> --env prod`. It exits 0 only when a finished run of that
   tree satisfies the whole policy, and then records the sha as the env's last deploy, the base of the next plan.

### Side loops

- **Lite, between iterations.** `qg lite` checks a commit in the foreground in seconds to minutes: static, the rules
  ratchet, the affected tests that need no database, the mutant hunt. No Postgres, stack, agent or queue slot, and
  never a deploy verdict. See [Lite verification](#lite-verification).
- **Rules ratchet.** With `rules:` in verify.yml, the static stage fails when the commit adds, per rule and file,
  more violations than its base held: forbidden imports between layers, banned patterns, new code comments, new
  markdown files. Existing debt never fails a run. See [Architecture rules](#architecture-rules).
- **Nightly audit and simplify.** `qg nightly` runs the whole suite of master when no verification waits. With
  `--simplify` a headless `/simplify` agent reviews what reached master since the last night, an optional `audit:`
  runs a security lens first, and the fixes land on an unmerged `qg/simplified/<run>` branch. See
  [Nightly simplify](#nightly-simplify).
- **Cycles and pages.** Every run of one feature or PRD phase joins a cycle. After each run qg rebuilds one HTML
  page per walked QA topic; the Claude session that asked for the run publishes them as artifacts. See
  [Cycles and the published pages](#cycles-and-the-published-pages).
- **Deploy check.** `qg check` is the only thing a deploy script needs. It judges the tree, never a stored flag,
  and on OK records the deploy and closes the cycles of the run. See [Verdicts](#verdicts).

## What you need to make it work

### 1. System requirements

qg runs on **Linux only**: every stage runs in a `systemd-run` scope of the user session and every agent in a
`bubblewrap` jail. macOS and Windows are not supported (WSL2 with systemd enabled is untested).

| Needed for | Program | Notes |
|---|---|---|
| everything | Python 3.12, [`uv`](https://docs.astral.sh/uv/) | `uv` installs qg and the right Python |
| everything | `git`, `curl` | |
| everything | systemd user session (`systemd-run`, `systemctl --user`), cgroup v2, `nice`, `ionice` | the default on current distributions |
| everything | Docker Engine, usable without `sudo` | runs the Postgres container of each run |
| agent stages | `bubblewrap` at `/usr/bin/bwrap` | the jail of every agent |
| agent stages | [Claude Code](https://code.claude.com/docs) (`claude` on `PATH`), logged in | |
| QA stage | Node.js 18 or newer with `npx` | runs `@playwright/mcp` |
| rules ratchet, nightly audit | `semgrep` 1.183.0 on `PATH` | |
| nightly audit | `ruff`, `gitleaks`, `osv-scanner` on `PATH` | then `qg audit-rules` downloads the semgrep packs |
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
uv tool install ruff && uv tool install semgrep        # rules and audit tools
```

`gitleaks` (where the distribution has no package) and `osv-scanner` are single binaries: download them from
<https://github.com/gitleaks/gitleaks/releases> and <https://github.com/google/osv-scanner/releases> into a directory
on `PATH`.

### 2. Install qg

```bash
uv tool install --editable --python 3.12 /path/to/qg   # puts qg in ~/.local/bin
qg doctor                                              # docker, systemd-run, uv, git, claude, curl, state root, disk, ports, images
```

`qg doctor` prints ok, warn or fail per item and exits 1 when any fails. It checks that a user scope with
`CPUQuota` can be created, that the state root is writable, that 10 GiB are free on it and that ports of
21000-21999 are free. `warn` is only the optional image tool.

With an editable install, a commit to the qg checkout is live for every new `qg` command at once, but the runner
daemon keeps the code it started with until it is restarted (see step 5).

### 3. Enable the runner

```bash
qg install-service                                     # copies systemd/qg-runner.service to ~/.config/systemd/user, daemon-reload
systemctl --user enable --now qg-runner.service        # starts the daemon
loginctl enable-linger "$USER"                         # optional: keep it running without a login session
qg enable                                              # the operator's switch: until it is on, the runner executes nothing
```

The unit runs `%h/.local/bin/qg runner` with `Restart=on-failure`. `qg install-service` also records the `node` and
`npx` the QA browser server uses (`settings.json` in the state root); it enables and starts nothing. `qg disable`
turns the switch off again (a run in flight finishes); `qg status` then shows
`paused: the operator has not activated qg`.

### 4. Onboard a repository with `qg init`

```bash
cd /path/to/repo
qg init                 # dry run: prints what it detected and the proposed verify.yml as a diff
qg init --write         # writes .claude/verify.yml into the working tree; never commits
```

`qg init` reads the git tree of the protected ref and detects the Python project (`uv.lock` is required), ruff, the
yarn packages, the test and unit-test directories, and the layers of the rules profile. For a repository without
verify.yml it proposes a whole minimal file; for one that has it, only the optional `rules:` and `lite:` sections it
lacks, appended byte for byte. Values to check are printed as `review` lines. See
[Onboarding a repository](#onboarding-a-repository).

### 5. Commit verify.yml to the protected ref

qg reads `.claude/verify.yml` **only** from `refs/heads/master`, else `refs/remotes/origin/master`, never from the
working tree or the branch being verified. Until the file is on master, `qg verify` and `qg check` refuse the
repository. A repository whose default branch is only `main` has no protected ref: create a local `master`.

Restart the runner before a verify.yml commit adds a key the running daemon does not know:

```bash
systemctl --user restart qg-runner.service     # when no run is in flight
```

`qg verify` runs the new code and accepts the key, but the daemon parses each queued run's configuration with the
code it started with. There the key is `unknown key(s)`: while another run is running the parse fails on every tick
and the queue stalls; on an idle machine the run ends `error` with `runner failed: ... unknown key(s) ...`. `qg init` prints a `WARNING` when the service started before the
last change of the installed `qg/config.py`. A run already in flight survives a restart: its executor lives in its
own scope and the new daemon re-adopts it.

### 6. Declare shared services

When the app stacks need a long-running dependency that nothing else keeps up (a vector store, a model server), list
its systemd **user** units under `services:`:

```yaml
services: ["vector-store.service"]
```

The contract:

- Before the Playwright or QA stage runs (never for a reused stage), the run starts a transient unit
  `qg-<id>-services.service` that `Requires` and is ordered `After` each listed unit, so the units start with it.
  A unit that waits for its own readiness in `ExecStartPost` is ready when the stage starts.
- A unit that does not start ends the run `error` (`services ... did not start`), not `failed`.
- Teardown stops the transient unit. Each listed unit must declare `StopWhenUnneeded=yes`: it stops when the last
  run holding it releases it, and keeps running while another run still holds it.
- Never enable these units at boot: qg starts what it uses and stops it when done.

### 7. Turn on the rules

`qg init` proposes `rules:` with `enforce: false`: in a run the regressions are logged as advisory and never fail
the static stage. `qg rules baseline` counts the whole tree and tracks the burn-down. When the team is ready, set
`enforce: true` (the default when the key is absent) on master: from then on a commit that adds a violation fails
the static stage. See [Architecture rules](#architecture-rules).

### 8. The daily flow

| When | Command | What it gives |
|---|---|---|
| between iterations | `qg lite` | static, rules, Postgres-free affected tests, mutants; exit 0 `GREEN` or 1 `RED` |
| an intermediate commit | `qg verify --feature "<name>" --no-qa` | every stage but QA; READY on those stages, never enough for a deploy |
| closing a phase | `qg verify --feature "<name>" --prd F01 --phase P1 --plan plan.md` | every stage, QA included |
| a release | `qg verify --release --phases F01:P1,F02:P1 --plan-dir plans/` | shared stages once, one QA per phase, one verdict |
| following a run | `qg wait <id>` | exit 0 READY, 1 NOT READY |
| before deploy | `qg check <sha> --env prod` | exit 0 only for a verified tree; records the deploy |

A verification without `--prd` and `--phase` leaves QA out on its own; `--no-qa` makes it explicit and also leaves
QA out of a phase close. P0 and P1 findings are fixed and re-verified with `qg retest <id>`; P2 and P3 are fixed
and closed with `qg fixed`.

### Minimal verify.yml

```yaml
version: 1
deps:
  python_sync: "uv sync --frozen"
postgres: {image: "postgres:16-alpine", db: "app_verify"}
stages:
  static: ["uv run ruff check .", "uv run ruff format --check ."]
  backend: ["DATABASE_URL={pg_url_async} uv run pytest -q tests"]
deploy:
  prod: {stages: [static, backend]}
  staging: {stages: [static, backend]}
```

This is close to what `qg init` proposes for a Python repository with ruff and a `tests/` directory; `qg init` leaves
passing the run's database (`{pg_url_async}`) to you as a `review` line, and adds `rules:` when it detects layers. Add `playwright` and
`qa` to `stages` and to `deploy.prod` once configured, `affected:` to run only the tests a change needs, then
`lite:` and `rules:`. The full schema is in [verify.yml](#verifyyml).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Runs stay queued and the runner's journal (`journalctl --user -u qg-runner`) repeats `qg runner: start failed` with `unknown key(s)`; or a run ends `error` with `runner failed: ... unknown key(s)` | the daemon started before qg learned a key that master's verify.yml now uses | `systemctl --user restart qg-runner.service`, when no run is in flight; then `qg resume` an errored run |
| `qg status` shows `paused: the operator has not activated qg`; `qg wait` exits 4 | the switch is off | `qg enable` |
| `qg verify` says `<ref> has no .claude/verify.yml: merge the verify configuration into <ref> first` | verify.yml is not on master | commit it to master; qg never reads it from the branch |
| `qg verify` says `has neither refs/heads/master nor refs/remotes/origin/master` | the repository has no master | create a local `master` |
| The report says `verify.yml changed in this branch: not used` | the branch edits verify.yml | expected: merge the change to master to make it count |
| Run ends `error`: `services <unit> did not start` | a `services` unit failed | `systemctl --user status <unit>` and `journalctl --user -u <unit>`; fix the unit, then `qg resume <id>` |
| Run ends `error`: `infrastructure, not the code: ...` stack not ready | the QA stack did not boot within 10 minutes, or died | read `logs/qa-stack.log`; a plain verification is queued again once automatically |
| Run ends `error` naming a usage limit or disabled access | the Claude account cannot run the agent | wait for the reset time it prints, or log in to another account; never retried automatically |
| Static fails with `new-comment` | the commit adds a code comment or a multi-line docstring | say it in a name, or in a one-line docstring |
| Static fails with `new-markdown` | the commit adds a `.md` file outside the allow-list | put usage in the README; keep plans and notes out of the repository, or extend `rules.markdown_allow` |
| Static fails with `components-import-services` or another import rule | a layer imports one it may not | follow the rule's message (for example, import the domain's manager) |
| `qg status` says `waiting for memory: needs N GiB available` | `MemAvailable` is below the run's measured need | wait; the oldest running run never waits, so the queue drains. Lower parallel load or the lane's `memory_max` |
| `waiting for a free slot (4 runs are running)` | four runs hold the slots | wait |
| `waiting for disk: N GiB free` | less than 10 GiB free on the state root | the runner prunes under pressure every 5 minutes; `qg prune --dry-run` shows what it would free |
| `waiting for another run's playwright stage` or `qa boot lock` | one Playwright stage and one QA stack boot at a time on the machine | wait |
| A stage fails with `out of memory in lane <lane>` | the kernel killed a command at the lane's `memory_max` | raise `resources.lanes.<lane>.memory_max` |
| `qg verify` refuses: `has uncommitted changes ... commit or stash first` | the worktree is dirty | commit; qg verifies a commit |
| `qg verify` refuses: `cycle ... was closed when <sha> was deployed` | the phase shipped | open a new phase with a new `--feature`; `--reopen "<reason>"` only after a rollback |
| `qg verify` refuses: plan `has topics the QA agent cannot walk` | plan topics marked manual, operator or real account | move them out of the plan, or pass `--allow-manual` |
| `qg verify --env prod` exits: `pass --base <sha> or --plan PATH` | no last-deploy record for prod | pass `--base` or `--plan`, or record one with `qg deployed` |
| `qg check` says NOT VERIFIED and names `qg/simplified/<run>` | the run verified a simplified commit | fast-forward the branch to that ref, then check the new tip |
| `qg lite` notes uncommitted changes | lite checks the commit, not the working tree | commit first |
| The probe agent's next attempt is refused for cache files | `__pycache__` or `.pytest_cache` under `probe.dir` are not gitignored | gitignore them |

## Reference

### Commands

Every command that takes a run id accepts a unique prefix. Run ids are `YYYYMMDD-HHMMSS-xxxx`.

**Verifying**

| Command | What it does |
|---|---|
| `qg verify --feature TEXT [options]` | Queues a verification of one commit; see below. |
| `qg verify --release --phases F01:P1,F02:P1 --plan-dir DIR [--ref] [--env] [--base]` | Queues one release: see [Release gate](#release-gate). `--feature` is not needed. |
| `qg lite [--repo PATH] [--ref HEAD]` | Checks a commit now, in the foreground, with static, rules, the affected tests that need no database and the mutant hunt; exit 0 green, 1 red. Never queued and never a deploy verdict (see [Lite verification](#lite-verification)). |
| `qg nightly [--repo] [--ref REF] [--feature nightly] [--simplify] [--since REF] [--dry-run]` | Queues every test of a commit (default: the protected master), no QA, no base. Never a deploy gate (`qg check` and reuse ignore it), never supersedes a verification, starts only when no verification waits and is cancelled when one arrives. `--simplify`: see [Nightly simplify](#nightly-simplify). |
| `qg wait <id> [--timeout S]` | Polls `state.json` every 5 s, one line per stage transition, and follows a run that ended `error` and was queued again to its retry. Exit 0 READY, 1 NOT READY, 2 cancelled, interrupted, superseded or error, 3 timeout, 4 while qg is paused. Cheap; if it dies the run goes on. |
| `qg retest <id> [--allow-manual]` | Queues a new run for the same repo, branch tip and feature with `retest_of=<id>`. Never deduplicated, and nothing is reused. Not for a nightly run. |
| `qg resume <id> [--allow-manual]` | Re-queues a cancelled, failed, interrupted or errored run for the same requested commit, base, plan and feature (`retest_of=<id>`), reusing every stage that passed (see [Stage reuse](#stage-reuse)). |
| `qg cancel <id>` | Queued: cancelled at once. Running: stops the run's systemd scopes (SIGTERM to everything in them), tears the sandbox down, status `cancelled`. A release run's cancel cancels its phase runs. |

`qg verify` options:

- `--repo PATH` (default `.`): the worktree. When it is clean at `--ref` the run works on it in place; a dirty one is
  refused. `--ref` (default `HEAD`) is the commit.
- `--feature TEXT`: the cycle's title; required except for `--release`.
- `--prd ID --phase ID`: a phase close. Together they key the cycle and turn QA on. Either one alone is refused.
- `--plan PATH`: the plan document. A plan with topics marked manual, operator or real account is refused unless
  `--allow-manual`.
- `--base SHA`: the commit the change is measured from (default: the `deployed.json` record of the repo and env). It
  must be an ancestor of the commit and not newer than the last deploy to the env. With a base qg writes
  `runs/<id>/plan.md` (see [The deployed record, the base and the plan](#the-deployed-record-the-base-and-the-plan)).
  **`--env prod` with no base and no `--plan` exits non-zero: "pass --base <sha> or --plan PATH".**
- `--env prod|staging` (default `prod`): the deploy policy.
- `--no-qa`: leave QA out even of a phase close. `--simplify`: run the optional S0 first.
- `--stress N` (1 to 20): run the whole Playwright stage N times (see [Stress pass](#stress-pass)).
- `--force`: queue even when the tree is verified; reuse nothing.
- `--reopen REASON`: reopen a cycle closed by a deploy that was rolled back; the reason is recorded.
- `--dry-run`: print the base, the generated plan, the stages (S0 included), the commands with example values, the
  specs S4 would run and the teardown commands; create nothing.

What `qg verify` does:

- Resolves the canonical repository (the real path of the git common dir's parent, so every worktree of one
  repository is the same repo), branch (`detached` when none), sha and tree. Refuses a repository whose master has
  no `.claude/verify.yml`.
- Prints the existing run and queues nothing when a finished run of the same repo and tree already satisfies the
  asked stage set (unless `--force`).
- Otherwise queues the run (superseding a queued run of the same repo and branch), appends it to its cycle and prints
  the run id, queue position and an ETA from the last five runs.
- Without `--prd` and `--phase` prints `QA left out: the QA pass runs only when a phase is closed (--prd <id>
  --phase <id>) or at release (--release); this run checks the other stages`.
- A plain verification (no `--force`, `--stress`, retest or resume, on a branch) is held 120 s
  (`queue.HOLD_SECONDS`) before it may start; `qg verify` and `qg status` print `starts in Ns: waiting for a newer
  commit of this cycle to supersede it`, and the ETA counts the hold. Nightly, release and phase runs and automatic
  retries are never held.

**Deploying**

| Command | What it does |
|---|---|
| `qg check <sha> --env prod\|staging [--repo PATH] [--dry-run] [--unverified REASON]` | Exit 0 only when a finished run of that sha's tree satisfies the env policy. On OK it records the sha in `deployed.json` (the base of the next plan) and closes the cycle of the run (of every phase run for a release); `--dry-run` records nothing. A run that simplified verified another tree, so the requested sha is NOT VERIFIED until `qg/simplified/<run-id>` is fast-forwarded into the branch; the output names the ref. `--unverified` skips the check, appends `{when, repo, sha, tree, env, reason, last_run_id, open_findings}` to `overrides.log`, prints a loud warning and exits 0. |
| `qg deployed <sha> --env prod\|staging [--repo PATH]` | Sets the last-deployed record by hand. |

**Findings**

| Command | What it does |
|---|---|
| `qg report <id>` | Prints `report.md`. |
| `qg ack <id> <finding-id> --note TEXT` | Acknowledges a P1 (who `operator`, when, note, in the run's `findings.json`): it stops blocking READY. A P0 cannot be acknowledged. |
| `qg fixed <id> <finding-id> --commit SHA --note TEXT` | Closes a P2 or P3 the session fixed, without a run: status `fixed` and `closed` `{who: session, when, commit, note}`; the report, cycle page and topic pages are rebuilt ("cerrado sin run") and later runs of the cycle no longer carry it. Refused for a P0 or P1 ("a P0 or P1 is closed only by a run of its cycle that re-verifies it"), for a finding that is not open, and on a run when a later finished run of the same cycle holds the same finding (close it there). |
| `qg probe-review <id> <finding-id> [--probe accepted\|rejected] [--flip accepted\|rejected] --why TEXT` | The operator's review of a P0's red probe and its flip patch. It replaces the verifier's review (reason `operador: …`; only an accepted probe is rerun by retests) and is kept with the probe file, its output and the patch in `calibration/<run>-<finding>/`, outside `qg prune`'s reach, as an example for later verifiers. |
| `qg verify-p0 <id>` | Lets the P0 verifier judge a finished run's open P0 findings that no verifier has judged, on a temporary worktree of its commit; prints the tally and rewrites the run's report (and its release's). |
| `qg remerge <id> --reason TEXT` | Recomputes a finished run's findings from its own QA output with the current merge. |

**Pages and cycles**

| Command | What it does |
|---|---|
| `qg topics <run-id>` | Rebuilds the topic pages of the run's cycle and prints, per topic, `topic:`, `name:`, `verdict:`, `page:` and `artifact:`. These are what gets published. |
| `qg topic-artifact <cycle-id> <slug> <url>` | Stores the claude.ai artifact URL of one topic. One topic keeps one URL: a different one is refused. |
| `qg cycle-page <run-id>` | Rebuilds the local cycle page (debugging only, not published) and the topic pages, and prints `cycle:`, `feature:`, `page:` and `artifact:`. |
| `qg cycle-artifact <cycle-id> <url>` | Stores the artifact URL of a cycle; kept for old records. |
| `qg prd <prd>` | Prints every phase of a PRD: state (open, or closed with the deployed commit), last run, artifact. |
| `qg prd-artifact <prd> <url>` | Stores the artifact URL of a PRD on every phase cycle of it. |
| `qg cycle-assign <cycle> <prd> <phase>` | Maps a cycle that predates `--prd`/`--phase` to a PRD phase (one-off). |
| `qg board` | Writes `~/.local/state/qg/board.html`, a static table of the last run per repo and branch with verdict, findings, stage times and links, and prints its path. |

**Repository setup and rules**

| Command | What it does |
|---|---|
| `qg init [--repo PATH] [--profile python-layered] [--write] [--no-baseline]` | Onboards or reforms a repository (see [Onboarding a repository](#onboarding-a-repository)). Dry run unless `--write`; never commits. |
| `qg rules check --base SHA --head SHA [--repo PATH] [--config FILE]` | Prints the rule violations `--head` adds over `--base`, then one summary line. Exit 0 when it adds none, 1 otherwise. |
| `qg rules baseline [--repo PATH] [--config FILE]` | Counts every violation at `HEAD`, per rule (with its change since the previous snapshot) and for the 20 files that hold most, and stores the snapshot. Reads git objects only. |
| `qg simplify-queue [--repo] [--all] [--lens L] [--severity P0,P1]` | Lists what simplify passes and the audit left alone, to decide. |
| `qg simplify-decide <S-id> accepted\|rejected --why TEXT [--repo]` | Decides a simplify queue item. |
| `qg audit-rules` | Downloads the semgrep registry packs the nightly audit runs, replacing the ones it has. |

**Operating**

| Command | What it does |
|---|---|
| `qg status [--all]` | Enabled or paused, service state, the four slots, the queue with why each run waits, the last-deployed record per repo and env, and the last run per repo and branch (id, sha, env, verdict, open P0/P1, when, feature). |
| `qg doctor` | Checks docker, systemd-run (a user scope with `CPUQuota`, `nice`, `ionice`), uv, git, claude, curl, the state root, free disk (10 GiB), free ports (21000-21999) and the image tool. |
| `qg enable` / `qg disable` | Create or remove `~/.local/state/qg/enabled`. |
| `qg runner` | The daemon loop, in the foreground. systemd runs it. |
| `qg install-service` | Copies `systemd/qg-runner.service` to `~/.config/systemd/user/`, records node and npx, runs `systemctl --user daemon-reload`. Enables and starts nothing. |
| `qg prune [--dry-run]` | Deletes the bulk and logs of old finished runs, stale containers and caches (see [Resources and cleanup](#resources-and-cleanup)). The runner does it daily and when the disk is low. |
| `qg shadow-report [--project] [--k 5] [--r 3] [--m 3]` | Counts the shadow window (`shadow.jsonl`) and says whether the cutover exit criterion holds. |

**Forwarding.** `QG_BACKEND` selects the backend: `qg` (default) or an external release backend, to which
`verify`, `wait`, `check`, `topics`, `topic-artifact` and `cycle-artifact` are forwarded (see `qg/shim.py`).
`QG_SHADOW=1` makes `check`, `verify --release` and `wait` also compute the external
backend's verdict and log both to `shadow.jsonl`.

### State

State lives under `~/.local/state/qg` (override with `QG_STATE_ROOT`):

```
enabled  queue.lock  playwright.lock  qa-boot.lock  overrides.log  deployed.json  board.html  settings.json  prune.stamp
runs/<id>/        request.json state.json plan.md report.md report.json findings.json qa.json logs/ executor.log src/
cycles/<id>/      cycle.json page.html attempts/<run-id>.json topics/ topics.json thumbs/
cache/venvs/<sha256(uv.lock)[:16]>   cache/node/<package>-<sha256(yarn.lock)[:16]>
calibration/  simplify-queue/  audit/semgrep/  audit/ledger/  lite/  rules/  shadow.jsonl
```

### verify.yml

Read from the repository's **protected ref**: `refs/heads/master` if it exists, else `refs/remotes/origin/master`
(the local master wins because the operator keeps master local and pushes nothing without an OK, so `origin/master`
lags). It is never read from the commit being verified, so a branch cannot rewrite its own stage commands or poison
the shared dependency caches. The ref is resolved fully qualified with `git rev-parse --verify --end-of-options`, so
a branch or tag named like a ref cannot stand in, and the file is read as `<sha>:.claude/verify.yml` of that sha.
The sha is recorded in the run state and report. When the verified commit's verify.yml differs, the report and
`verify` say "verify.yml changed in this branch: not used". If the protected ref has no verify.yml, `verify` and
`check` refuse.

The schema is closed: an unknown key is an error that names its path. The YAML is parsed strictly: a duplicate key,
a `<<` merge key, an alias, an anchor or an explicit tag is refused, and every command must be a string. The surface
map is parsed the same way.

```yaml
version: 1
deps:
  python_sync: "uv sync --frozen"
  node_packages: ["frontend", "admin-ui"]
  node_install: "yarn --cwd {pkg} install --frozen-lockfile"
postgres: {image: "postgres:16-alpine", db: "app_verify"}
resources:
  cpu_quota: "100%"            # the executor
  memory_max: "1G"
  lanes: {tests: {cpu_quota: "1000%", memory_max: "11G"}}   # setup, static, tests, qa_stack, qa_agent
timeouts_minutes: {simplify: 30, static: 5, backend: 15, frontend: 8, playwright: 15, qa: 30, setup: 30, teardown: 5}
typical_minutes: {backend: 6, qa: 12}   # the estimate when there is no history
affected:
  backend: {command: "uv run pytest -q {tests}", always: [], map: [], all: ["tests"], whole_when: []}
  frontend: [{package: frontend, related: "... {files}", all: "...", whole_when: [], shared: []}]
stages:
  static: ["<cmd>", ...]
  backend: ["<cmd>", ...]
  frontend: ["<cmd>", ...]
  simplify:                      # optional: S0's limits and the directories it may edit
    paths: ["src/", "frontend/src/"]
    time_limit_minutes: 25
    max_turns: 150
    max_cost_usd: 10
  playwright:
    critical: ["frontend/e2e/<spec>.spec.ts", ...]
    surface_map: "docs/smoke/surface-map.yml"
    run: "<cmd with {specs}>"
    shards: 1                    # optional: N > 1 runs the specs as N shards at once
  qa:
    boot: "<cmd that boots the app stack and stays in the foreground>"
    ready_url: "http://localhost:{frontend_port}"
    ready_marker: "STACK READY"  # optional: the stack log must also print this
    app_url: "http://localhost:{frontend_port}"
    api_url: "http://localhost:{backend_port}"
    surfaces: [{name: "mail catcher", url: "http://localhost:8025", notes: "..."}]   # optional extra pages
    secrets_env_file: "/abs/path/.env"
    secrets_keys: ["MODEL_API_KEY", ...]
    accounts: [{role: "admin", name: "...", email: "...", password: "..."}]
    time_limit_minutes: 40
    max_turns: 250
    max_cost_usd: 15
    notes_file: ".claude/qa-notes.md"
probe:                           # optional: the P0 probe and flip (needs affected.backend)
  dir: "tests/qg_probes/"        # where the probe agent writes test_qg_<finding>.py
  max_p0: 5
  max_cost_usd: 3                # per P0 and agent (probe, flip), across its attempts
  time_limit_minutes: 40         # the whole probe pass of a run
mutants:                         # optional: the mutant hunt after a passing backend stage (needs affected.backend)
  max: 12                        # mutants per run, one changed line at a time across the changed files
  time_limit_minutes: 20         # added to the deadline of a run with the backend stage
lite:                            # optional: the tests `qg lite` runs (needs affected.backend)
  command: "uv run pytest -q {tests}"   # runs the selected tests that need no database
  tests: ["tests/unit/**"]       # globs of the tests that need no database (default: every selected test)
audit: {lenses: [security], time_limit_minutes: 240}   # optional: the nightly audit
rules:                           # optional: architecture rules in the static stage
  profile: python-layered
  layers: {components: ["app/api/**"], managers: ["app/managers/**"], services: ["app/services/**"], repositories: ["app/repositories/**"]}
  tests: ["tests/**"]
  markdown_allow: ["docs/**"]
  disable: []
  enforce: true
teardown: ["<cmd>", ...]
services: ["vector-store.service"]  # optional: systemd user units the app stacks need
deploy:
  prod: {stages: [static, backend, frontend, playwright, qa], max_p0: 0}
  staging: {stages: [static, backend, frontend, playwright]}
```

**Required:** `version` (must be 1), `deps.python_sync`, `postgres.image` and `.db`, `stages`, `deploy` (at least
one of `prod`, `staging`) and `deploy.<env>.stages` (at least one stage, none twice). When present,
`stages.playwright` needs `run` (containing `{specs}`) and `stages.qa` needs `boot`, `ready_url`, `app_url` and
`api_url`. `deps.node_install` is required when `node_packages` is not empty. In `qa.accounts`, `role`, `email` and
`password` are required and `name` is optional; in `qa.surfaces`, `name` and `url`. `secrets_keys` needs
`secrets_env_file`. `affected.backend` needs `command` and `all`; each `affected.frontend` entry needs `package`,
`related` and `all`. `probe`, `mutants` and `lite` need `affected.backend`; `probe.dir` is a relative directory
ending in `/`; `lite.command` must contain `{tests}`. Every stage a deploy policy lists must be configured under
`stages`; `simplify` in a policy is dropped. A stage list may be empty (the stage passes with `no commands`).

**Defaults:**

- `resources`: executor 100% and 1G; lanes `setup` 1600% / 8G, `static` 200% / 2G, `tests` 1000% / 11G, `qa_stack`
  500% / 5G, `qa_agent` 200% / 2G. A `memory_max` is a systemd size (`4G`) or `infinity`.
- `timeouts_minutes`: simplify 30, static 5, backend 15, frontend 8, playwright 15, qa 30, setup 30 (per sandbox
  step), teardown 5 (per teardown command).
- `typical_minutes`: setup 3, static 3, backend 6, frontend 3, playwright 6, qa 12.
- `stages.qa`: `time_limit_minutes` 15, `max_turns` 150, `max_cost_usd` 15 (a number > 0).
- `stages.simplify`: `time_limit_minutes` 25, `max_turns` 150, `max_cost_usd` 10, `paths` the app source
  directories `src/` and `frontend*/src/` listed in `DEFAULT_SIMPLIFY_PATHS` of `qg/config.py`.
- `probe`: `max_p0` 5, `max_cost_usd` 3, `time_limit_minutes` 40. `mutants`: `max` 12, `time_limit_minutes` 20.
  `audit`: `lenses [security]`, `time_limit_minutes` 240.
- `deploy.<env>.max_p0` 0, `p1_acknowledged` false (read, gates nothing on its own). `playwright.critical`,
  `teardown`, `services` empty. `rules.enforce` true.

#### Placeholders and environment

Every command runs with `bash -c` in the worktree, a failing pipe stage failing it. It may use these placeholders;
a `{name}` is replaced unless a `$` precedes it, so `${X}` and other braces (`{a,b}`) are untouched:

- `{run_id}` `{src}` `{run_dir}` `{backend_port}` `{frontend_port}` `{pg_port}` `{db}`;
- `{pg_url_async}` (`postgresql+asyncpg://postgres:postgres@127.0.0.1:{pg_port}/{db}`) and `{pg_url_sync}`
  (`postgresql://postgres:postgres@127.0.0.1:{pg_port}/{db}`);
- `{specs}` in `playwright.run`: the selected specs, space-separated and shell-quoted;
- `{shard}`, `{shards}`, `{shard_dir}` in a sharded `playwright.run`;
- `{pkg}` in `deps.node_install`: the absolute path of a temporary copy of the package directory, holding
  `package.json`, `yarn.lock` and any `.yarnrc*`; the command itself runs in the worktree root;
- `{tests}` in `affected.backend.command` and `lite.command`, `{files}` in `affected.frontend[].related`.

The environment of every command is an allowlist of the runner's (`PATH` `HOME` `USER` `LOGNAME` `LANG` `LC_*` `TERM`
`TMPDIR` `XDG_RUNTIME_DIR` `XDG_CACHE_HOME` `XDG_DATA_HOME` `XDG_CONFIG_HOME` `NVM_DIR` `UV_CACHE_DIR`
`UV_PYTHON_INSTALL_DIR` `YARN_CACHE_FOLDER` `PLAYWRIGHT_BROWSERS_PATH` `DOCKER_*`), so `DATABASE_URL`, service URLs
and model keys are never inherited. Added: `UV_PROJECT_ENVIRONMENT` (the cached venv), `UV_FROZEN=1`,
`PYTHONUNBUFFERED=1` and the placeholders as variables: `QG_RUN_ID` `QG_SRC` `QG_RUN_DIR` `QG_BACKEND_PORT`
`QG_FRONTEND_PORT` `QG_PG_PORT` `QG_DB` `QG_PG_URL_ASYNC` `QG_PG_URL_SYNC`.

### Stages

| Stage | What |
|---|---|
| setup | the given worktree in place (or a detached worktree of the sha), Python and node dependency caches, ports, Postgres |
| S0 simplify | **optional (`--simplify`), not part of the gate**: headless `/simplify` over `base..sha`; its fixes become one commit on `refs/heads/qg/simplified/<run-id>` and every later stage verifies that commit (see [S0, simplify](#s0-simplify)) |
| S1 static | commands from `stages.static`, then the rules ratchet when `rules:` is set |
| S2 backend | commands from `stages.backend` (alembic and the `-m pg` tests are just commands) plus the affected tests of `affected.backend`, then the mutant hunt |
| S3 frontend | commands from `stages.frontend` plus `vitest related` per changed package from `affected.frontend` |
| S4 playwright | critical specs plus the specs the surface map ties to the files the feature touched |
| S5 qa | headless Claude Code QA agent with a Playwright MCP browser, then the P0 probe and verifier (see [S5, the manual QA pass](#s5-the-manual-qa-pass)) |

What a run executes is what `deploy.<env>.stages` lists; `simplify` runs only with `--simplify`, and `qa` only at a
phase close or release. Because the stages run in lanes, a normal deploy verification takes about 15 minutes of wall
clock; the QA pass is the real gate.

#### Lanes

After setup the static and tests lanes start together, each in its own systemd scope; the QA lane starts when the
tests lane has ended, so a run never holds the Playwright stack and the QA stack, two app stacks and two browsers, at
once:

```
setup -> static                                                                   (lane static: S1)
      -> backend -> frontend -> playwright -> QA Postgres -> stack boot -> agent   (lane tests: S2, S3, S4, then lane qa: S5)
```

The QA lane has its own ports and its own Postgres container (`qg-<id>-qapg`), so its stack never shares a database
with the tests. A failed stage never stops another lane or another stage of its lane. `--simplify` runs alone before
the lanes; when it fails, every other stage is skipped (`not run: simplify failed`).

#### Cgroups

Commands run as `systemd-run --user --scope -p CPUQuota -p MemoryMax -p MemorySwapMax=0` per lane
(`resources.lanes`). The defaults sum to 20 cores and 21 GB while the lanes run (executor 100% / 1G, static 200% / 2G,
tests 1000% / 11G, qa_stack 500% / 5G, qa_agent 200% / 2G); setup, before the lanes, has 1600% / 8G. Docker Postgres
containers are capped at 1 GB and 2 cores each outside those scopes. `qg cancel` and the janitor stop `qg-<id>.scope`
and `qg-<id>-*.scope`. A command killed by the OOM killer fails its stage with "out of memory in lane <lane>".

#### How command stages behave

- Commands of a stage run in order. The first non-zero exit fails the stage and skips that stage's remaining
  commands; a stage timeout kills the command's process group. Output goes to `runs/<id>/logs/<stage>.log`.
  Background processes a command leaves behind are not killed until the run ends.
- Summaries are read from the output: pytest's final line, vitest's `Tests` line, Playwright's `N passed` and
  `N failed` lines, plus the first 30 `FAILED` lines (and `ERROR`, vitest `FAIL`, Playwright failures). pytest
  commands containing `-m pg` count as the Postgres suite on the cycle page.
- Dependencies: the Python environment is `cache/venvs/<sha256(uv.lock)[:16]>`, synced once per key under a file
  lock; each node package's `node_modules` is installed once per `yarn.lock` hash in a temporary copy and then
  hard-linked into the worktree. The cached node files are made read-only (`chmod a-w`), so a run cannot mutate the
  shared cache. Installs use the protected config's commands only.
- Postgres is `docker run -d --name qg-<id>-pg --tmpfs /var/lib/postgresql/data` on a free port of 21000-21999, and
  the run waits for `pg_isready`. The container's `postgres` user is a superuser (`POSTGRES_PASSWORD=postgres`), so
  `CREATEDB` and `DROP DATABASE` work: a stack may drop and recreate the database `postgres.db`.
- `teardown` commands run last, in the worktree with the placeholders and the environment above, after the stacks
  stopped and **before the Postgres container is removed**, so a command can still read the run's database (for
  example to drop the run's vector-store collections). Their output is `logs/teardown.log`. A failing or timed-out
  teardown command is logged and never fails the run.
- `services`: see [Declare shared services](#6-declare-shared-services).

#### Affected tests

`affected` (read from the protected master) maps the changed paths of `base..sha` (else against the base branch) to
tests:

- `affected.backend.map` is a list of `{paths: [globs], tests: [pytest paths]}`.
- The `always` list (the core: tenant isolation, billing, auth and permissions, webhook signatures) runs whatever
  changed.
- Every changed test module runs.
- A path in `whole_when` (fixtures, root conftest, lockfile) selects `all`.
- A changed path that maps to nothing is named in the stage summary.
- `command` must contain `{tests}`. The Postgres `-m pg` lane stays in `stages.backend`.
- `affected.frontend` lists packages with `related` (`{files}`: vitest's module graph, the tests that import a
  changed file) and `all` (the whole suite, used when a `whole_when` path of the package or a `shared` path
  changed). A package with no change runs nothing.

The selected tests are part of the stage fingerprint, so `resume` reuses a pass only for the same selection.

#### Playwright

S4 runs the `critical` specs plus the `specs` of every surface-map entry whose `paths` globs (`fnmatch`) match a file
in `git diff --name-only <merge-base(sha, base branch)>..<sha>` (the base branch is the first of `master`,
`origin/master`, `main`, `origin/main`). Specs that do not exist in the commit are dropped, the rest are
deduplicated. The surface map is a YAML list of `{paths, flows, specs}`; every listed spec is handed to
`playwright.run`, so keep non-Playwright files out of `specs`. With no spec at all the stage is skipped with
`no specs`, which is not a pass. **S4 boots its own stack**: the repository's Playwright `webServer` does it inside
`playwright.run`, on the run's ports and Postgres. Two runs never run their Playwright stage together: a stage takes
`playwright.lock` first.

**Shards.** `stages.playwright.shards: N` (default 1) runs the selected specs as at most N shards at once, dealt
round-robin. Shard 0 keeps the tests lane's ports and Postgres. Every other shard has its own backend, frontend and
Postgres ports, its own container (`qg-<id>-pg<k>`) and its own `TMPDIR` (`<run>/shards/<k>/tmp`), and runs `run`
with its own `{backend_port}`, `{frontend_port}`, `{pg_url_*}`, plus `{shard}`, `{shards}` and `{shard_dir}` (a
scratch directory: point Playwright's `--output` there, or the shards wipe each other's `test-results`). The tests
lane's cpu and memory limits are divided among the shards, so raise `resources.lanes.tests` when sharding. Each shard
logs to `playwright-shard<k>.log`, joined into `playwright.log`. The stage fails when any shard fails; its failures
are the union, its commands every shard's, its specs the whole selection.

#### Release gate

`qg verify --release --repo <candidate> --phases F01:P1,F02:P1,... --plan-dir <dir> [--ref] [--env] [--base]` queues
one release:

- one release run (mode `release`, in the cycle `RELEASE-<sha12>`) with static, backend, frontend and the whole
  Playwright;
- one QA-only run per phase (mode `phase`, the plan being `<dir>/F01-P1.md`), each an attempt of its own phase cycle
  with its own QA, ACs and findings. They queue behind the release run, so under the memory admission they run as
  the machine allows. When the release run finishes, its shared stages are copied onto every phase run as
  `reused from <release run>`.

There is **one verdict**: the release run's, judged on its stages plus the QA of every phase and every finding of the
tree. A phase run is never accepted by `qg check` on its own, and `qg check` of the release closes every phase cycle.
A failing Playwright test belongs to the phase whose tag its scenario carries: an AC tag `@F01-P1-AC03` names its
phase, a PRD tag `@F01-D10` names the PRD's one phase in the release. The release report says "1 test failed, owned
by F01·P1" and lists each failing test with its owner (`sin dueño` for none); the other phases' pages say "bloqueada
por F01·P1" instead of repeating the failure.

**Flake hunt.** When the Playwright stage of a release run fails, only the failed tests are run again 3 times with
the stage's own `run` command (one `file::test` node id for pytest lines, `file:line` for Playwright lines; each
rerun capped at 10 minutes, and the run's deadline grows by 30). Each failed test is labelled `falla siempre` (failed
in all 3) or `intermitente N/3`; the labels show beside the test and its owner and as a tally in the verdict line.
When every failed test is `intermitente 0/3` (it failed once and passed all its reruns) the stage passes and the
report lists the tests under "Avisos" as intermittent, to be fixed; any other label keeps the stage failed. The
reruns use the run's own ports and database, not the stack of the first pass (a repo whose run command boots the
stack boots it again).

#### Stress pass

`qg verify --stress N` (any verification, 1 to 20) runs the whole Playwright stage N times on the same tree, never
reusing an earlier pass, and reports every test that failed at least once, labelled `falla siempre` (failed in all
N) or `intermitente k/N`. The stage still fails when any pass failed, and the run's deadline grows by (N-1)
Playwright timeouts. A release run with `--stress` skips the flake hunt.

#### QA only at phase close or release

`qg verify` runs the QA stage only when given both `--prd` and `--phase`, and `qg verify --release` runs one QA pass
per phase. A verification without them is queued without QA and joins the cycle of its `--feature` (opening it when
there is none). Its verdict is judged on the stages it ran, so it ends READY when they pass, and its first warning
says `QA not run: ...`. `qg check` still judges every run under the whole `deploy.<env>` policy, so a run without QA
never satisfies a deploy that lists `qa`. `qg retest` and `qg resume` keep the QA setting of the run they repeat;
`qg nightly` never runs QA. A plain verification of exactly the tree a phase QA of its cycle already passed carries
that QA over.

#### Findings: what the session does with them

P0 and P1 block (an open P0 the verifier did not refute, an open P1 the operator has not acknowledged): the session
fixes them and the next run of the cycle (`qg retest <run>`) re-verifies them. P2 and P3 never block and never need a
run: the session fixes them, commits, and closes each with `qg fixed <run> <finding> --commit <sha> --note <text>`.
A later QA pass of the cycle never re-checks an earlier P2 or P3: the charter lists them apart as known, and the
retest agent gets only the P0 and P1.

### The deployed record, the base and the plan

- A successful `qg check <sha> --env <env>` is the moment a deploy script proceeds. On OK it writes `deployed.json`:
  `{<canonical repo>: {<env>: {sha, tree, at}}}`. `qg status` shows it; `qg deployed` sets it by hand.
- `qg verify --base <sha>`; absent, the base is that record for the repo and env. With a base and no `--plan`, qg
  writes `runs/<id>/plan.md` and records it as the run's plan: `git log --no-merges --format='%h %s' base..sha`,
  then the paths of `git diff --name-only base..sha` grouped by the `surface_map` entries (paths matching no entry
  under "other"). An explicit `--plan` wins but is still prefixed with the commit log. Staging may run with no base:
  S0 and the generated plan are skipped and the report says so. Production refuses (see `qg verify`).
- Without `--plan`, the generated plan opens with **Topics to walk**: the user-facing commits grouped, by a heuristic
  over the files each touches against the surface map (no model call), into at most 12 topics. Commits of type test,
  docs, style, chore, ci, build, reverts and commits that touch only tests, documents or tooling are left out. The
  section is first in the plan, so a truncated plan never loses it, and the charter tells the agent to walk those
  topics as its `plan:` flows.
- The QA budget follows the plan. With generated topics the agent gets 7 minutes per topic plus 7 for the retests,
  at least 14 and at most 70; its turn cap and the stage timeout grow in the same proportion
  (`qa.time_limit_minutes` and `max_turns` set the turns per minute). A plan without generated topics scales the
  limits by 1 + 0.5 per full 3000 characters, at most 1.9 times. The cost cap never grows. `qg verify` prints the
  estimate up front.

### Onboarding a repository

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

- **verify.yml at the protected ref**: only the optional sections it lacks, appended to its text, which survives
  byte for byte. `rules:` with the detected layers and tests and `enforce: false`, so the first rollout is advisory.
  `lite:` when `affected.backend` exists: its command is `affected.backend.command` without the pytest-xdist options
  after its `pytest` word (`-n`, `--numprocesses`, `--dist`, with their values; a command with an unclosed quote or a
  `$(…)` or backtick substitution is copied as is), so the tests run in one process: for a small selection, workers
  each importing the application cost more than they save. Its `tests` are each unit-test directory and its `/**`
  glob (left out, and said so, when there is none). With both present it proposes nothing: already configured.
- **no verify.yml**: a whole minimal file: `uv sync --frozen`, the yarn packages, `postgres:16-alpine` and a
  `<repo>_verify` database, ruff as the static stage when the project uses it, pytest on the test directories as the
  backend stage, prod and staging gating those two, and `rules:`. `uv.lock` is required.

Every proposal is parsed with the verify.yml schema and its rules resolved against the profile before it is shown;
the values to review (database name, commands, deploy policies, frontend stage, a lite command that uses a database
placeholder) are listed as `review` lines. Then it counts the proposed rules over the whole tree and stores the
snapshot (see *The burn-down*); `--no-baseline` skips it.

`--write` writes `.claude/verify.yml` into the working tree of `--repo`, only when the file there is absent or equal
to the protected ref's; qg never commits it. It ends with the next steps: commit to master, restart the runner if it
predates the new keys, `qg lite`, `qg rules baseline`, `rules.enforce: true`.

### Architecture rules

A profile is data bundled with qg: `qg/profiles/<name>.yml` names the layers of an architecture, the imports
forbidden between them, banned patterns per layer and two hygiene rules, each with an id and a message naming the
fix. `python-layered` describes a layered Python backend (components → managers → services → repositories):

- **Forbidden imports**: a component may not import a service or a repository; a service may not import a manager
  or a component; a repository may not import a service, manager or component.
- **Banned patterns**: `.model_dump()` and `.dict()` in managers and services; swallowed exceptions
  (`except Exception: pass`) and bare `except:` in every layer; `TestClient` in tests; SQLite URLs and imports
  outside tests.
- **`new-comment`**: no new code comment in `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs` and `.cjs` files. One-line
  docstrings, `# noqa`, `# type:`, `# pragma`, `# fmt:`, `# pyright:`, `# mypy:`, `# ruff:`, `# pylint:`, shebang and
  encoding lines are not comments here; a docstring of two or more lines is. In TS/JS, `//` and `/* */` count, but
  not text in strings, template literals, regex literals or JSX, tool pragmas (`eslint-disable`,
  `@ts-expect-error`, `@ts-ignore`, `prettier-ignore`, …), `/// <reference`, a shebang, or a one-line `/** … */`
  alone above a function, method, class or exported declaration.
- **`new-markdown`**: no new `.md` file outside the allow-list: `README.md` and `CLAUDE.md` at any depth,
  `CHANGELOG.md` and `LICENSE.md` at the root, plus `markdown_allow`.

A repository opts in with the top-level `rules:` of its verify.yml: `profile`, `layers` (each profile layer it has,
mapped to path globs), `tests` (globs), `markdown_allow` (globs), `disable` (rule ids) and `enforce` (default
`true`). Globs are `fnmatch`, so `*` crosses `/`. A forbidden import matches any import whose dotted module has, as
one of its parts, the last directory (or module) the target layer's globs name: `app/services/**` stands for any
`…services…` module, `from app import services` included. A layer the repository leaves out has no rules. Without
`rules:` the static stage is exactly its commands.

**The ratchet.** No baseline is stored. The check lists the files `base..head` adds, modifies or renames, reads each
one from git at the base and at the head into a temporary directory, runs the profile's generated semgrep rules on
both sides in one semgrep call, and fails only where the head holds more matches of one rule in one file than the
base held (a new file starts at 0, a renamed one is compared with its old path); it never keys on line numbers. For a
regression it prints every head-side match of that rule in that file as `path:line rule-id message`. Comments count
only on the lines the change adds and markdown only for added files. Code moved into a new file counts as new there.
Nothing is written into the repository. The repository's contents are untrusted: files are read by blob id
(`git diff --raw -z`, `git ls-tree -z`, `git cat-file --batch`, each answer checked against the id asked for), only
regular files are written (a symlink or submodule entry is skipped), and a path that is absolute, holds an empty,
`.` or `..` part, a backslash, NUL or newline fails the check instead of being written.

In a run, the check follows the static stage's commands: its base is the run's `--base`, else the merge-base of the
commit with the base branch. semgrep runs in the static lane's scope, its lines go to `logs/static.log` and the
stage's failures, and a regression fails the stage. The rules and their profile are part of the static stage's
reuse fingerprint. `qg rules check` runs the same check by hand; `--config FILE` takes a YAML file holding only a
`rules:` mapping instead of the protected verify.yml, to try a repository that has not opted in. Both need `semgrep`
on `PATH` (the audit's pinned 1.183.0).

`enforce: false` is the rollout mode: in a run the static stage still checks, logs every regression and the summary
(marked `(advisory)`) to `static.log` and records the `qg rules check` command, but never fails the stage;
`qg rules check` still exits 1 on a regression.

**The burn-down.** `qg rules baseline` (and `qg init`) count the whole tree: every Python file read from git by blob
id, semgrep in a systemd scope with the static lane's default limits (`qg-rules-<id>-static-<n>`), at nice 10 in the
idle I/O class. Each count is stored as `~/.local/state/qg/rules/<repository>-<hash>/<UTC time>-<sha12>.json` (the
commit, the `rules:` it counted with, and every rule, file and count), and the output gives the total and each rule's
change since the previous snapshot, noting when the rules configuration changed in between. The ratchet never reads
these snapshots.

### Mutant hunt

With `mutants` in verify.yml, a passing backend stage is followed by the mutant hunt (`qg/mutants.py`, no LLM).

- **What is mutated**: for every changed Python file under `stages.simplify.paths` (no test or tooling file), the
  lines the commit adds over the merge-base get simple mutations: a comparison swapped (`==`/`!=`, `<`/`>=`,
  `>`/`<=`, `in`/`not in`, `is`/`is not`), `and`/`or`, `True`/`False`, an integer +1, `return x` to `return None`.
  Only the operator or literal is rewritten in place, so format and comments stay; an operator next to a
  parenthesis or a comment is left alone.
- **How**: at most `mutants.max`, one line at a time, on a scratch worktree (`runs/<id>/mutants/src`) against the
  tests `affected.backend.map` ties to that file (not the `always` list: its tests would kill every mutant without
  the file's own tests saying anything). Each set of tests runs once unmutated first; a mutant is killed only by a
  `FAILED` or `ERROR` line that run did not have, so a test that already fails or flakes kills nothing. A mutant the
  tests still pass is a gap in them.
- **Output**: it only reports. The stage summary ends with "mutantes: 2 vivos de 12 (src/x.py:41 `>=` -> `<`; …)" and
  `runs/<id>/mutants.json` lists each mutant as killed, survived or undecided; the stage's status never changes. A
  changed file that does not parse is named and skipped, and a crash of the hunt becomes its note.

### Lite verification

`qg lite [--repo PATH] [--ref HEAD]` is the inner loop a coding agent runs between iterations: a foreground check of
one commit. It runs only the deterministic steps, one after the other:

- **static**: the commands of `stages.static`, stopping at the first that fails and printing its output tail;
- **rules**: the ratchet of the commit against its merge-base with the base branch, when verify.yml has `rules:`
  (`path:line rule-id message` per new violation; with `enforce: false` they are shown as `info` and never fail);
- **tests**: the affected tests of the change (`affected.backend`) narrowed to the `lite.tests` globs and run with
  `lite.command`, when verify.yml has `lite:`; without it the line says the tests are not configured for lite;
- **mutants**: the mutant hunt on the added lines with the same narrowed tests, after the tests passed, when
  verify.yml has `mutants:` and `lite:`, bounded to 2 minutes; it only informs.

There is no Postgres container, no app stack, no agent and no Playwright. The commit is checked out in a detached
worktree under `~/.local/state/qg/lite/<id>/src` with the cached Python and node dependencies (the runner's caches,
built on a miss), and removed afterwards; uncommitted changes are never checked, and the output says so when the
worktree has some. verify.yml is read from the protected ref. Commands run in systemd scopes like the runner's
(`qg-lite-<id>-<lane>-<n>`) with the lane limits exactly as verify.yml configures them, and no memory wait of its
own. At most 2 lite checks run at once on the machine (the others print `waiting for a lite slot`), at nice 10 in
the idle I/O class. Lite never takes a runner slot.

The output is one line per step (passed, failed, skipped or info, with its time), the lines to fix under a failed
step, and one summary line: `GREEN` (exit 0) or `RED: <steps> failed` (exit 1). Logs stay in
`~/.local/state/qg/lite/<id>/logs` for a day. Lite writes nothing under `runs/`: `qg check` never accepts it and no
run reuses it.

### Nightly simplify

`qg nightly --simplify [--since REF]` adds S0 to a nightly run: /simplify reviews what reached the commit since
`--since` (default: the commit the repository's previous passing nightly simplify was asked to review, else the last
commit older than 7 days) and publishes its fixes as `qg/simplified/<run>`, never merged, for the developers to take
or leave: garbage collection of the drift agents leave on master. The suite of that nightly run verifies the
simplified commit. Its report lists each change with why (the agent's `summary.json`, redacted, kept as
`simplify/explained.json`), the full diff, the findings the agent left alone, and how to take it:
`git cherry-pick <sha>`. qg ships no timer: schedule `qg nightly --simplify` with a systemd user timer of your own
(for example `qg-nightly-<repo>.timer`).

**The simplify queue.** Every S0 pass queues each `left_alone` finding that starts with a file of the checkout in
`STATE_ROOT/simplify-queue/<repo>.json`, out of `qg prune`'s reach; a note that names no file is not queued.
`qg simplify-queue` lists it and `qg simplify-decide <S-id> accepted|rejected --why` decides an item. The next pass's
prompt carries the queue: accepted items are fixed first (their file is in scope even outside the diff) and cited by
id, which marks them `done` once the commit is published; pending items are not listed again; rejected ones are not
proposed again. **Coverage**: the agent must not finish while a changed file under `stages.simplify.paths` is unread
and turns remain, and lists the rest in `unreviewed`; the report shows them, and the stage summary says how many, how
many items it queued, and when the agent wrote no `summary.json`.

**The nightly audit.** With a top-level `audit:` (`lenses: [security]`, `time_limit_minutes` 240), a nightly S0
audits the night before the simplify agent runs, commit by commit, with no cap on cost, turns or findings. Its
minutes are added to the simplify stage's timeout and only guard against a hung audit: reaching them, or the 30
minutes one agent may take, fails the audit.

1. **Deterministic tools**, on the files the night adds or changes under `stages.simplify.paths` (tests and tooling
   left out): ruff `S` and `PERF` (the repository's own ruff config, extended) and semgrep with the registry packs
   `qg audit-rules` downloads into `STATE_ROOT/audit/semgrep`, both kept only on lines the diff adds; gitleaks over
   the commits `base..sha` with `--redact`; osv-scanner over the tracked lockfiles. Their hits are leads for the
   lens, never findings.
2. **Lens agents**: per lens, one confined agent per commit (merges left out, a commit of more than 25 reviewed files
   split in parts of 25, a commit with none listed in the report as without code), on Sonnet (`LENS_MODEL` in
   `qg/audit.py`), with read-only access to the checkout at the night's tip (`qg/audit_<lens>_prompt.md`). It reads
   the commit's message and diff and every file of it in full as it stands at the tip, and gets the tool hits on
   those files (the first part also those on files the commit changes outside the review, such as a lockfile). A
   file a lens leaves unread is given to it again, up to 3 runs in all. A part still unread, or whose findings file
   is malformed, is left out of the ledger and the lens goes on; after the last part the audit fails, naming each
   part left unfinished and why, and the next night reads those parts again. The findings carry file, line, symbol,
   rule, severity, the evidence path and a one-line fix.
3. **Refuter**: as each part ends, its findings deduplicated by file, rule and symbol, less those already queued or
   judged earlier that night, go to a refuter, another confined agent on the default model that sees the claims but
   not the lens's reasoning (`qg/audit_refute_prompt.md`) and tries to disprove them in batches of 8. What it
   confirms goes to the simplify queue at once with its lens, severity, evidence and fix, identified by lens, file,
   rule and symbol, so a rejected finding is never queued again however it is reworded. What it refutes, and what
   it left unjudged, goes only to the report.
4. **Ledger**: a part read to its last line whose findings were all judged is recorded in
   `STATE_ROOT/audit/ledger/<repo>-<hash>.json` (lens, commit, part, files, run, date) and no later night reads it
   again, even when a later part, stage or the whole run fails. Since a failed night keeps its base, the next one
   starts at the first part the ledger lacks, so a normal night costs only the new commits' agents.

Each agent writes only its own fresh directory under `runs/<id>/audit/` and its file tools reach only that, the
checkout and the directory it reads (`--add-dir` each); qg's own files there (tool hits, commit patches, claims,
`audit.json`) are read-only to every agent and out of the simplify agent's reach, and the refuter is shown only the
claims. Each agent renews the Claude login for its own time. `qg simplify-queue --lens security --severity P0,P1`
narrows the list. An accepted audit item is fixed by the next simplify pass like any other. A part left unfinished, a
malformed verdicts file, a missing tool or rule pack, or the time guard fails the simplify stage. The audit only
informs: no finding blocks a deploy. Tools, pinned: semgrep 1.183.0, ruff 0.17.3, gitleaks 8.30.1, osv-scanner 2.7.0
(and, for later lenses, vulture 2.16, radon 6.0.1, pip-audit 2.10.1, jscpd 5.4.2, knip 6.44.1), in `~/.local/bin`.

### S0, simplify

Runs first, in the sandbox checkout of the requested sha, only when there is a base. The headless Claude launcher,
the bubblewrap jail, the fresh HOME and the systemd scope are S5's. The agent has only Read, Edit, Write, Glob and Grep
(no shell, no browser, no MCP); the working directory is the checkout, `--add-dir` is `runs/<id>/simplify/` (the full
`git diff base..sha`, its summary, its output) and the jail binds nothing else read-write. S0 never takes a host
`flock` and never reaches the host checkout. The prompt is the `/simplify` review (reuse, simplification, efficiency,
altitude) limited to the diff, plus the repo's CLAUDE.md; the four lenses are embedded in `qg/simplify_prompt.md`
because a jailed, settings-less session has no skills. The agent runs no tests: the later stages run the suites on
its result.

What S0 may change is enforced in code after the agent ends, never by the prompt alone. A violation fails the stage:
nothing is dropped silently, nothing is committed, `simplify/rejected.diff` keeps the attempt and the report shows it.

- A file must be in `git diff base..sha` and under `stages.simplify.paths`.
- Never a test, `tests/`, `__tests__/`, `e2e/`, `*.test.*`, `*.spec.*`, snapshot, fixture, `conftest.py`,
  `quality-gates-notes.md`, `.claude/`, `.github/`, `.git*`, lockfile.
- No new file, no file the diff deleted, no symlink (nor a path through one), no submodule, no mode change, no added,
  removed or changed ignored file, no rewrite of the sandbox's `.git` file; the sandbox must be clean before the
  agent runs.
- Path rules compare NFKC-casefolded components (`Tests/`, fullwidth spellings, `Test_x.py`, `Makefile` are
  protected too). The commit must be the requested sha plus exactly the checked files and leave the sandbox clean;
  the executor asserts at the end that the sandbox's HEAD tree is the tree the run recorded as verified.
  `stages.simplify.paths` comes from the protected master, never the branch.

Outcomes:

- No change passes with "nothing to simplify" (sha and tree unchanged).
- Changes become one commit on top of the requested sha (message `simplify: ...`, the agent's one-line changes and
  the files, last line `qg run <id>`), created in the canonical repository as the **new** ref
  `refs/heads/qg/simplified/<run-id>` (an `update-ref` with the null old value, so it can never move an existing
  ref; qg writes no other ref, and never a branch checked out in a worktree). `state.json` keeps
  `requested_sha`/`requested_tree` and sets `sha`/`tree` to the verified commit; report.md and report.json carry
  `requested_sha`, `verified_sha` and `verified_tree`, the report says "merge `qg/simplified/<run-id>` into
  `<branch>` (fast-forward) before deploying" and shows the S0 diff. `qg check` gates on the verified tree, so it
  passes only after that fast-forward.
- If the agent fails or times out the stage fails, every other stage is skipped, and the run is NOT READY.

The deduplication of `qg verify` is by the requested tree and the stage set: a tree whose READY run found nothing to
simplify is not queued again, and after the fast-forward the new tip's tree is the verified one of that run.

### Stage reuse

A queued run does not repeat what an earlier run of the same repository and env already proved. The executor decides,
stage by stage, just before the stage would run (so a source that finished while the run waited counts). A source is
any earlier run with the hardened provenance (see [Verdicts](#verdicts)) whose status is passed, failed, cancelled,
interrupted or error, for the same repo and env, the same **requested tree** and the same **base**. Newest first; the
first one that proves out lends the stage. `qg verify --force` and `qg retest` reuse nothing; `qg resume` and a plain
`qg verify` do.

- **S0 simplify** is reused only if the source's S0 passed and, when it committed, its `qg/simplified/<id>` ref still
  points at the recorded sha, that commit's tree is the recorded one and its parent is the requested sha itself.
  With nothing simplified the verified commit must be the requested one. The sandbox then checks out the verified
  commit and every later stage verifies that tree. If the checkout fails S0 runs.
- **static, backend, frontend, playwright** are reused only if the source stage passed **and** its fingerprint equals
  the one the stage would have now: the verified tree, the stage's commands from the protected config, the
  dependency and Postgres configuration, the env allowlist and, for Playwright, the selected specs and the run
  command (for backend and frontend, the selected tests). A fingerprint is stored on every stage a run executes; a
  stage of an older run has it rebuilt from the run's own request and state (not possible for qa, which is then
  never reused).
- **qa** is reused only with the same fingerprint, which adds the whole QA configuration (scaled limits, accounts,
  boot) and the **plan text**, and only from a run of the same cycle with its `qa.json` on disk. The walked flows,
  findings and evidence are copied into the new run (paths rewritten to its directory), refusing symlinks and any
  path that resolves outside its run directory.
- A reused stage keeps the source's summary, counts, specs, flows, cost and turns, and records `reused_from=<run>`
  and its fingerprint. `state.json`, `report.md`, `qg wait`, the cycle page and a topic page show it as
  `passed (reused from <run>)`.
- Anything uncertain re-runs the stage: no source, an unreadable source, a ref that moved, another tree, base or
  config, a fingerprint that cannot be rebuilt.
- `qg check` (and every verdict) accepts a run built from reused stages only while each source still exists as a
  trusted finished run of the repository, passed that very stage itself (or by a reuse that stands, recursively) and
  verified exactly the same tree; otherwise the reason is listed and the run is NOT READY. Retention never deletes a
  run another run reuses.

### Verdicts

A run is judged against a policy, never against a stored flag. `deploy.<env>` demands its command stages `passed` (a
`skipped` stage never counts), a finished QA stage when it lists `qa`, and at most `max_p0` open P0 findings. READY
means the policy is satisfied; otherwise NOT READY, with the reasons in the report. Operator decisions D7-D13:

- A P0 blocks until the P0 verifier refutes it; one it confirmed or could not judge keeps blocking, and the reason
  names the ones still `sin verificar` (D7). A P0 without a red, accepted probe keeps blocking (D13).
- An open P1 blocks until the operator acknowledges it (`qg ack`). `p1_acknowledged` is still read from old configs
  and gates nothing on its own. P2 and P3 never block; the session closes them with `qg fixed`, without a run.
- A QA that walked nothing blocks (D12): a run, or a phase of a release, whose QA ended without walking a single flow
  of its plan (stack not ready, agent dead, account, cancelled) is NOT READY with `sin QA:` and the reason. A phase
  run of a release that ends so is retried once automatically (`release.retry`: a fresh walk of the same plan,
  `retest_of` the first run; the release waits for it and the newest run of the chain stands for the phase). A QA
  that walked part of its plan and then failed only warns: `QA incompleta` under "Avisos" (D9). A QA stage still
  pending or running blocks until it ends.
- A release still blocks on its integrity: no phases declared, a declared phase with no run or several runs that are
  not one retry chain, a run of an undeclared phase, a phase run on another tree, untrusted, or whose reuse does not
  stand.
- Warnings (`report.json` key `warnings`, section "Avisos" of `report.md`, the cycle page header and `qg check`'s
  output) never change the verdict.

How env and stage set interact:

- `qg verify --env X` asks for the stages of `deploy.X`, `qa` only at a phase close (see
  [QA only at phase close or release](#qa-only-at-phase-close-or-release)).
- `qg check <sha> --env X` evaluates the policy of X on the stage results of the runs of that tree, so a run
  requested for prod whose S1-S4 passed satisfies `--env staging` even though its own prod verdict is NOT READY.
- A run whose state lacks the provenance fields of the hardened runner (a 40-hex `config_sha`, `config_ref`, tree and
  sha, a canonical absolute repo path) is legacy: `qg check` and reuse ignore it, so it never satisfies a deploy.
- A run's own verdict (`qg wait`, `report.md`) is the one under the env it was requested for.

### Cycles and the published pages

A **cycle** groups the runs of one unit of work. With `--prd` and `--phase` the key is repo + PRD + phase (the cycle
of that phase, whatever the branch or feature text). Without them a run joins the cycle of repo + branch + feature
name (whitespace collapsed, case ignored), opening it when there is none. Every later `verify` or `retest` of the
same key appends a run (attempt 2, 3, ...), also after it reached READY. `qg retest <id>` inherits the cycle. A run
superseded before it started stays in the cycle's list but is not an attempt. A successful `qg check` closes the
cycle; a later `qg verify` of it is refused unless it names a new phase or passes `--reopen "<reason>"` after a
rollback.

`cycles/<cycle-id>/cycle.json` holds `{id, repo, branch, feature, plan, runs: [run ids in order], status: open |
ready, artifact_url, created, updated}` plus the PRD, phase, closing and reopening records. The status is `ready` when
the latest finished attempt satisfies its own env policy and no run of the cycle is queued or running.

**What is published is one page per topic, not the cycle page** (see *Topic pages*). The cycle page stays a local
file for debugging.

After every run finishes, qg freezes it as an attempt (`attempts/<run-id>.json`, so run retention never erases
history) and regenerates `cycles/<cycle-id>/page.html`, one self-contained page in Spanish:

- header with the feature, repo and branch, latest sha and tree, the verdicts for prod and staging, attempts and
  total time;
- figure tiles (attempts, wall time, tests per suite, Playwright specs, findings open and fixed);
- the attempts timeline (what failed, the first failing tests or P0 titles, `git log --oneline` between attempts,
  duration) and the stages of the latest attempt;
- "Pruebas" (the S4 specs and flows, the S5 flows walked with widths and locales);
- "Hallazgos" (P0 and P1 with status open, fixed in attempt n, or acknowledged with the operator's note, steps,
  expected, actual and evidence) and "Mejoras" (P2 and P3);
- the report and log paths.

Evidence screenshots are embedded as JPEG data URIs at most 1280 px wide, 8 MB per page, converted with `ffmpeg` or
ImageMagick (`magick` or `convert`); without one of them, or beyond the budget, they are listed by path. The page is
an artifact fragment: it starts at `<title>` (the feature name) and has no doctype, `html`, `head` or `body`, because
the Artifact tool wraps it. It follows the artifact page contract: colours as tokens on `:root` with the two
dark-mode blocks, an explicit body background, Google Fonts as the only external resource, and no horizontal scroll
at 390 px.

#### What the page, report.md and report.json may contain

Every text that reaches them goes through `qg/redact.py`. Replaced by `[redacted]`:

- the run's secret values: the `secrets_keys` read from `secrets_env_file` and every `stages.qa.accounts` password;
- `Authorization`/`Bearer` values, `password`/`secret`/`api_key`/token assignments (`password=x`,
  `"password": "x"`), `sk_`/`pk_`/`whsec_`/`polar_`/`xox` style keys, JWTs, `enc:v1:`/`enc:v2:` blobs;
- URL userinfo (`scheme://user:pass@`), including the Postgres password in connection URLs, and
  `POSTGRES_PASSWORD=` assignments (the bare word `postgres` is not blanket-replaced).

Emails are masked to their first letter (`t***@example.com`). The oracle's backend-log excerpts
(`evidence/backend-<n>.txt`) and the findings' title, area, steps, expected and actual are redacted when written, and
everything is redacted again at render (the page, via its escape function) and in report.json and report.md. The page
never renders account emails, roles or any other `stages.qa.accounts` value, nor config values. Not covered: text
inside screenshots, and a secret in a form no pattern or configured value matches.

#### Topic pages

The operator reads only manual-test results, one page per topic like a ticket. A topic is one user-facing change of
the plan: each `plan:<name>` flow S5 walked. qg writes `cycles/<id>/topics/<slug>/page.html` for each, an
`index.html` of them and `cycles/<id>/topics.json` (slug, name, verdict, `artifact_url`). **No page exists until S5
has walked the topic.** A page holds:

- what the change is (one paragraph);
- the verdict (works, works with improvements, broken, blocked and why; derived from the walk and the open findings);
- the walk as a sequence of screenshots, one per step at 1280 px and at 390 px, each with a caption;
- the topic's findings P0-P3 with their screenshot (an improvement says what should change, anchored to its
  screenshot).

It never lists failing tests, test counts, commit logs, stage tables or durations; an attempt that stopped before S5
adds one line, "attempt n stopped at <stage>: <one-sentence reason>". The 8 MB image budget applies per page; the
redaction above applies to every text.

#### Publishing procedure for a Claude session

qg cannot reach claude.ai, so the session that asked for the verification publishes the pages. One topic, one URL,
forever.

1. Start the run: `qg verify --feature "<feature name>" [--prd ID --phase ID] [--base SHA] [--plan PATH]
   [--env staging]`. Keep the feature name identical on every attempt. Follow it with `qg wait <run-id>` in the
   background.
2. When the run ends (READY or not, also `error`), run `qg topics <run-id>`. For each topic it prints:
   ```
   topic: <slug>
   name: <topic name>
   verdict: works | works with improvements | broken | blocked
   page: ~/.local/state/qg/cycles/<cycle-id>/topics/<slug>/page.html
   artifact: none | https://claude.ai/artifact/<id>
   ```
   With no topic it says so: no page exists.
3. Publish each `page:` file with the Artifact tool, unchanged:
   - `artifact: none`: publish with `file_path` set to the page, an `icon` such as `checklist`, and a one-sentence
     `description` of the topic; then store the URL the tool returns: `qg topic-artifact <cycle-id> <slug> <url>`.
   - `artifact: <url>`: read that artifact first (`action: "read"`), then publish the same page with `url` set to the
     stored URL. Omit `icon`. Nothing needs storing.
4. After each later attempt, repeat steps 2 and 3. A page is rebuilt from the frozen attempts; `qg topics <run-id>`
   regenerates a stale one.

#### Files the QA stage writes (read by `qg ack`, `qg check` and the page)

`runs/<id>/findings.json`:

```json
{"findings": [{"id": "F-001", "severity": "P0", "title": "...", "area": "bookings / calendar",
  "steps": ["..."], "expected": "...", "actual": "...", "evidence": ["/abs/shot.png"],
  "status": "open", "acknowledged": null, "verification": null}]}
```

- `severity` is P0 to P3.
- `status` is `open` or `fixed`: a retest marks a finding `fixed` when it no longer reproduces; `qg fixed` marks a P2
  or P3 the session fixed, recording `closed`: `null` or `{"who": "session", "when": "...", "commit": "<sha>",
  "note": "..."}`.
- `acknowledged` is `null` or `{"who": "...", "when": "...", "note": "..."}`.
- `verification` is `null` or `{"verdict": "confirmed|refuted|error", "reason": "...", "category": "...",
  "filed": "P0", "when": "..."}` (see the P0 verifier in S5).
- Evidence paths are absolute or relative to the run directory, and only a regular image file (`.jpg` `.jpeg` `.png`
  `.webp`, at most 10 MB) whose real path is inside `runs/<id>/evidence/` is read; any other path (`..`, absolute,
  symlink out) is listed as `rejected evidence path` and never opened.
- Across attempts findings are matched by `id`: the first mention gives the description, every mention adds
  evidence, the latest decides severity, status, acknowledgement and verification.

`runs/<id>/qa.json`: `{"flows": [{"name": "...", "widths": [1280, 390], "locales": ["es"], "result": "...",
"summary": "", "steps": [{"caption": "...", "width": 1280, "locale": "en", "screenshot": "/abs/evidence/x.jpg"}]}]}`.
A plan flow (`plan:<name>`) has `summary` and `steps`; a finding may carry `flow` to name its topic. Evidence may
also be a `.txt` file (the backend-log excerpts), shown on the page as a `<pre>` block under the same realpath
restriction.

### S5, the manual QA pass

Runs in the QA lane, after the tests lane has ended, only at a phase close or release. It waits for the memory its
`qa_stack` lane needs (see [Resources and cleanup](#resources-and-cleanup)), holds the run's `services`, then:

1. **Boot.** `stages.qa.boot` (placeholders filled, the QA lane's own ports and Postgres) starts in the worktree in
   its own process group, with the stage environment plus **only** the `secrets_keys` read from `secrets_env_file`
   (KEY=VALUE lines, no shell). Output goes to `logs/qa-stack.log`. Ready when `ready_url` answers and, when
   `ready_marker` is set, the log holds it too; 10 minutes at most, counted once the run holds the machine-wide boot
   lock (`qa-boot.lock`): no two QA stacks boot, seed or build at the same time, since concurrent seeds and builds
   starve each other. While it waits, `qg status` shows `qa boot lock`; the wait is bounded by what the stage has
   left.
2. **Charter.** `runs/<id>/qa/charter.md` holds:
   - the feature and "Plan changes to walk": the agent derives every user-facing change from the plan, walks each at
     1280 and 390 px (one locale is enough; a second of en, es, vi when the UI offers it and there is time), and
     records each as its own flow `plan:<short name>` with `walked`, `blocked` or `not reachable`, a summary and a
     screenshot of every step at both widths. A run that records no `plan:` flow fails the stage. A `blocked` or `not reachable` flow, with
     its reason in `notes`, never fails it (a blocked plan topic is kept as a P2 coverage note `not walked: blocked
     or not reachable`, and a plan whose only flow is `plan:no user-facing change` is complete);
   - the turn, time and cost budget, and the order to rewrite `findings.json` after every topic, so a run stopped by
     a cap keeps its walk;
   - the plan file (`--plan`, read from the sandbox when inside the repository), `git diff --stat` and the changed
     files since the merge-base, the accounts and URLs (and `surfaces`), the project notes file;
   - "Journeys to walk": with a plan, none (Playwright covers the generic journeys; the agent walks only the plan
     topics); without one, the critical specs as journeys to walk by hand and the surface-map flows the diff touches.
     A journey with no `walked` entry is a P2 finding;
   - the P0 and P1 still open or acknowledged in earlier attempts of the cycle (re-check first, with the next free
     `F-nnn` id). Earlier P2 and P3 are listed apart as known, not to be re-checked nor filed again.
3. **Agent.** The pass is split: one short agent per plan topic and one more for the retests of earlier findings,
   run one after another on the same stack, each with its share of the turns and minutes and a note naming only its
   own topics and ids; their `findings.json` are merged before judging. Several short contexts cost a fraction of
   one long one. Each is `claude -p <qg/qa_prompt.md> --model sonnet --max-turns N --max-budget-usd CAP
   --output-format json --restricted --strict-mcp-config --mcp-config=qa/mcp.json --tools=Read,Write
   --allowedTools=mcp__playwright,Read,Write` (file upload and browser install disallowed), with cwd
   `runs/<id>/qa`, no user or project settings, no hooks, no shell. The MCP is `npx -y @playwright/mcp@0.0.41
   --isolated --headless --viewport-size 1280x800 --output-dir runs/<id>/evidence --allowed-origins <app, api and
   surfaces>` (`--isolated`: the shared `~/.cache/ms-playwright-mcp` profile is never touched). Time limit: the
   smaller of the scaled `time_limit_minutes` and what is left of the stage timeout; SIGTERM, 30 s, SIGKILL for the
   whole process group, which also ends the browser. The agent is a tester: it records findings P0 (broken, 5xx,
   wrong data, dead end), P1 (misleading, stuck), P2 (unclear, needless step), P3 (cosmetic) and writes
   `qa/findings.json`:

   ```json
   {"walked": [{"flow": "...", "widths": [1280, 390], "locales": ["en"], "result": "walked", "notes": "..."}],
    "findings": [{"id": "F-001", "severity": "P1", "area": "...", "title": "...", "steps": ["..."],
                  "expected": "...", "actual": "...", "evidence": ["evidence/F-001-x.jpg"]}],
    "improvements": [{"id": "I-001", "severity": "P3", "area": "...", "title": "...", "steps": [], "expected": "",
                      "actual": "", "evidence": []}]}
   ```

   Running out of time, turns or money, a topic not reached, a missing locale or width, or a journey left out is a
   P2 finding `not walked: <why>` on that topic (`G-` ids; a plan with no plan flow at all is a P1), re-judged on
   every run. A walked plan flow needs 1280 and 390 px in at least one locale.
4. **Oracles** (added by qg, not the agent), over the stack log written while the agent ran: every distinct Python
   traceback (chained ones as one), every distinct ` ERROR ` line and every HTTP 5xx access-log line becomes one P0
   finding, area `backend log`, id `L-<hash of the signature>` (stable across attempts), evidence
   `evidence/backend-<n>.txt`. An ERROR line that heads a traceback is part of it. The P0 verifier (step 7) marks the
   ones that repeat another finding's root cause as `duplicate`.
5. **Validation.** A missing file fails the stage with `qa agent produced no findings file`. An invalid file gets up
   to 3 repair turns; one still invalid is read leniently: every valid `walked`, `findings` and `improvements` entry
   is kept and only the invalid ones are dropped, the stage problem saying `findings.json: N invalid entries
   dropped: <first error>` (a file that is not JSON, or not an object with its lists, still fails). An account the
   CLI reports as limited or with subscription access disabled is handed to the rotator and the agent retried once
   on the next account; a login file caught mid-rotation is re-read for up to 150 s before failing. Evidence names
   become absolute paths inside `evidence/`. The merged list is written to `runs/<id>/findings.json` (improvements
   are P2/P3 findings) and the walked flows to `qa.json`. A finding of an earlier attempt of the cycle that is open or
   acknowledged and is not reported again is written as `fixed` (the page says "corregido en el intento n"); an
   acknowledged P1 that is reported again keeps its acknowledgement.
6. **P0 probe and flip** (`qg/probe.py`, prompts `qg/probe_prompt.md` and `qg/flip_prompt.md`; only when verify.yml
   has `probe`, D13). After the stack is stopped, every P0 carried from an earlier run with an accepted probe is
   re-run 3 times on this commit (a scratch worktree with the earlier probe file copied in): green in all 3 marks it
   `fixed` though the QA agent never saw it again; the same failing test in all 3 keeps it open whatever the agent
   said; runs that disagree (a flaky probe) or an error leave what the QA stage recorded. Then each open P0 without
   a probe, at most `probe.max_p0`, gets a scratch worktree of the commit (`runs/<id>/probes/<finding>/src`, removed
   afterwards) where:
   - a **probe agent** (`claude -p --model opus --tools=Read,Edit,Write,Glob,Grep`, no shell, the same jail, the
     finding's card, the plan and its evidence read-only in `probes/<finding>/card/`) writes exactly one new pytest
     file, `<probe.dir>test_qg_<finding>.py`; any other change ends the probe. qg runs it with
     `affected.backend.command` on that file, on the QA lane's ports and Postgres in the `qa_stack` scope. It is
     **red** only with exit 1, exactly one `FAILED` line of that file, no `ERROR` line and no passed test, and only
     when two more runs fail the same test; anything else is an error, and the next agent (up to 3) is told why with
     the output. Each agent's time leaves room for those 3 runs;
   - on red, a **flip agent** edits product files under `stages.simplify.paths` (never tests, fixtures, conftest,
     tooling, lockfiles, seeds or the probe) until the probe passes and then the tests `affected.backend` maps to the
     changed files pass (up to 3 attempts). A flip with which the probe passes but existing tests fail, and no attempt
     reaches green, is kept as `contradicts` with those tests named: they may assert the defect itself (the verifier
     decides). A green or contradicts flip's diff is `probes/<finding>/flip.patch`; it is never committed, merged or
     published as a ref. Each agent's .git file and ignored files are compared right before and after it runs, so
     the caches qg's own test runs write are never held against it;
   - for a green or contradicts flip, a **held-out variant**: a third agent, on a fresh scratch worktree
     (`probes/<finding>/variant-src`), writes `<probe>_variant.py`, a second test of the same defect with other
     data, seeing the first probe but never the flip; it must be red 3/3 like the probe. qg then applies
     `flip.patch` there (dropping the changed files' bytecode) and runs it: a flip with which the variant still fails
     is `special-case` ("flip que solo arregla el caso del probe"). It only informs: the P0 blocks either way. No
     clean variant leaves the flip as it was, with the reason.

   Each agent gets at most `probe.max_cost_usd` over its attempts and the whole pass `probe.time_limit_minutes`
   (added to the run's deadline); a P0 left out gets "sin reproducción" with the reason. The finding's `probe`
   records status (red, error, none), reason, file, flip and the reviews. To turn it on: `probe.dir` must be a
   directory the repository's `affected.backend.command` really runs (a command that drops arguments under `tests/`
   needs its probes elsewhere, for example `src/tests/qg_probes/`), and `__pycache__` and `.pytest_cache` there must
   be gitignored, or the probe agent's next attempt is refused for the cache files pytest wrote. Then add `probe:` to
   master's verify.yml and restart `qg-runner`. The stage summary adds
   `probes: 1 rojos de 1 P0; flips: 0 en verde, 1 que contradicen tests existentes`.
7. **P0 verifier** (`qg/verifier.py`, prompt `qg/verify_prompt.md`). After the stack is stopped, every open P0 of the
   run that no verifier has judged (the agent's, the oracles', the `W-` flow failures and the unjudged P0s carried
   from earlier attempts) goes to one independent agent: `claude -p --model opus --tools=Read,Grep,Glob --add-dir
   <worktree>`, in the same bubblewrap jail with a fresh copy of the login, its working directory `runs/<id>/verify/`
   (read-only: `p0.json`, `plan.md`, `charter.md` and the evidence files of this run the findings cite; nothing
   outside the run's own `evidence/` is ever copied), the worktree of the commit read-only, 15 minutes
   (`config.VERIFY_MINUTES`, added to the run's deadline), 80 turns, $5. It tries to refute each P0 and answers
   `confirmed`, or `refuted` with a category (`harness`, `designed`, `duplicate`, `unreachable`, `minor`,
   `unsupported`) and the severity it deserves (P1-P3). A refuted P0 takes that severity; the finding's
   `verification` keeps the verdict, category, reason with its file:line references, the severity filed and when.
   A confirmed P0 keeps blocking; so does one it could not judge, with that reason. The stage summary ends with the
   tally (`verificador: 1 P0 confirmados; 3 refutados (duplicate 3)`). A P0 with a red probe also carries the probe
   file, its output and the flip patch: the verifier accepts or rejects the probe (it must fail because of exactly
   this defect, through the product as shipped) and the flip (no special case of the probe's data, no weakened
   check, the cause rather than the symptom; for `contradicts`, every failing test must assert the defect itself).
   Only an accepted probe is rerun by later retests, and the report and the cycle page say "reproducido por …",
   "sin reproducción: …", "flip propuesto en …/flip.patch" or why not. Up to 3 earlier operator reviews
   (`qg probe-review`; those that overruled a verifier first, then the newest) are copied to `verify/examples/` so
   the verifier holds itself to the operator's standard. `qg verify-p0 <run>` does the same for a finished run on a
   temporary worktree of its commit; it makes no probes.
8. **Teardown.** The stack is stopped (SIGTERM to the group, 30 s, SIGKILL) before the run's teardown commands.

The stage **fails** only when the run did not complete: the agent exited with an error, left no valid findings file,
walked no flow or took no screenshot, or claude reported no cost or turn count (the caps would be unchecked). An agent
that left no readable JSON result is a `StackFailure` (below). Running out of budget is not a failure: a timeout, the
turn cap, or the **cost cap** (`stages.qa.max_cost_usd`, default 15, passed to claude as `--max-budget-usd` and
checked against `total_cost_usd` in its JSON output: `cost cap exceeded ($x > $y)`) stops the agent, keeps what it
walked, and becomes a P2 `not walked` finding. Cost and turns are stored on the stage (`cost_usd`, `turns` in
`report.json`) and shown in the summary and on the cycle page. Findings found by a failed stage are still recorded. A failed QA stage that walked part of its plan is a warning (D9); one that walked
nothing blocks (D12). The rest of the verdict comes from the findings.

**Infrastructure failures** are not a verdict on the code. They are typed where they happen (`qg/proc.py`) and end
the run `error` (never `failed`), its error reading `infrastructure, not the code: <cause>`:

- `StackFailure`: the QA stack was not ready after 10 minutes or exited, died while the agent ran, or the agent left
  no readable JSON result. A plain verification that ends so is queued again once, at once (no hold), with the same
  request as a retest of the failed run (`retest_of`), so the stages that passed are reused; its error ends
  `; queued again as run <id>` and only the retry's end is announced to the coordinator inbox (see
  `qg/notify.py`). A run that is itself a retest, resume or retry is not queued again, nor one with a newer run of
  its line.
- `AccountFailure`: the claude CLI said the account cannot run the agent (a usage limit, with its reset time as the
  CLI prints it, or an organisation that disabled subscription access) after the one switch to another account, or
  the login is missing or too short and did not renew. Never queued again; it ends S0 and the nightly audit too, and
  a phase run that ends so is not retried by `release.retry`.
- Any other `Infrastructure` failure, such as `services` that did not start.

When a stage of the run already failed on the code the run ends `failed` as before, and it is not queued again.

Looking at the evidence: `runs/<id>/evidence/` holds the JPEG screenshots and the `backend-<n>.txt` excerpts; the
cycle page embeds them. Also under the run: `qa/charter.md`, `qa/mcp.json`, `qa/findings.json` (the agent's own
file), `qa/claude-output.json`, `logs/qa-stack.log`, `logs/qa-agent.log`.

### Resources and cleanup

**Admission** (`qg/admission.py`).

- Four slots machine-wide, `auto` to `auto4`: a run holds one from setup to its end.
- A run starts only when a slot is free, the run started last has had 20 s to take its memory, the state root has at
  least 10 GiB free, and `MemAvailable` (`/proc/meminfo`) is at least the need of its setup lane.
- The need of a lane: with a measured peak (the biggest recent one of that lane in the repository) it is peak × 1.25
  + 1 GiB, never above `memory_max` + 1 GiB; with no history it is min(`memory_max`, 4 GiB) + 1.5 GiB (`infinity`
  counts as 4 GiB). The need is recomputed on every pass, so queued runs see new peaks at once and a changed formula
  once the runner restarts.
- Queue order is FIFO, verifications before nightly runs. When the head does not fit, a later run whose setup fits
  starts first. A nightly run starts only when no verification waits, a held one included, and a running nightly run
  is cancelled when a verification cannot start, but never for a run still held. A run alone on the machine always
  starts (limits are caps, not use).
- Once two runs run, the younger run's stages (`static`, `tests` and `qa_stack` lanes) wait for the same
  `MemAvailable` need; the oldest running run never waits, so there is no deadlock. Two runs never run their
  Playwright stage together (`playwright.lock`) and two QA stacks never boot together (`qa-boot.lock`). Nothing is
  killed by qg's choice; the reasons show in `qg status`. The run's deadline keeps counting while it waits.

**The executor** of a run starts as `systemd-run --user --scope --unit qg-<id> --collect -p CPUQuota=<cfg>
-p MemoryMax=<cfg> -p TimeoutStopSec=300 nice -n 10 ionice -c2 -n7 python -m qg _execute <id>`. It stores its pid in
the run's state; `qg cancel` stops the scope, and a runner restart does not kill a run in flight (the new runner
re-adopts it while that pid is alive). A run whose executor pid is gone is finished `interrupted` (`cancelled` after
`qg cancel`) and its scope stopped. A run past its overall deadline is stopped and ends `interrupted`, never passed. A failure while handling one
run marks that run `error` and the daemon goes on.

**In place.** `qg verify` run from a worktree (`--repo`, default the current directory) whose HEAD is the requested
commit works on that path directly: no `git worktree add`.

- At `qg verify` time a dirty path (tracked changes, or untracked files outside gitignored paths) is refused
  ("commit or stash first; qg verifies a commit").
- When the run starts, under the path's lock (`qg-inplace.lock` in the worktree's own git dir; two runs on one path
  serialise, the run's QA and Playwright share it), HEAD must still be the commit and the tree clean, else the run
  gets a detached worktree.
- `--ref` of another commit, `--simplify`, `nightly`, retest and release runs always use a detached worktree.
- Dependencies link `node_modules` into the worktree (it must be gitignored; one already there is moved aside under
  `runs/<id>/aside` and put back).
- At the end HEAD and `git status` are compared with the start; a change makes the run `failed` ("el worktree cambió
  durante la verificación"), never READY. Teardown removes only what qg added and never the worktree. Gitignored
  output the stages write (caches, `test-results`) stays.

**Teardown** always runs in a `finally` (its subprocesses get default signal handlers): the stacks stop, the
`teardown` commands run, the `services` hold is released, the containers named `qg-<id>-*` are removed, then the
run's own worktree (`git worktree remove --force`; if the directory is already gone, only that run's entry under
`.git/worktrees` is removed).

**The janitor**, at runner start and every 10 minutes, stops the scopes of finished runs, tears down leftovers of
runs whose sandbox was not torn down, and applies retention.

**Retention** (`qg/prune.py`) runs daily, at once when the state root has less than 30 GiB free, and, while runs
wait for disk (under 10 GiB), at most every 5 minutes. `qg prune [--dry-run]` runs it by hand. Per finished run:

- kept whole: the latest run of each repo and branch, a run whose P0 awaits `verify-p0`, a run of a cycle that still
  has a run queued or running, any run within 3 days of its end (1 hour under disk pressure), and a failed,
  cancelled, interrupted or errored run for 7 days (it may be resumed; not under pressure);
- only the bulk removed (QA home, built bundles, storage, worktrees): a run with an open blocking finding, a run whose
  QA another run reuses, the newest QA of a cycle, a run of an open cycle within 7 days;
- otherwise the bulk plus the logs and evidence are removed; the small files (state, request, plan, report,
  findings, qa) stay;
- dropped whole after 45 days, except the newest READY run of each branch line, a READY run of a deployed tree, and
  runs that block, lend a stage or belong to an open or live cycle.

It also removes stale `qg-*` containers, the thumbnails of cycles untouched for 45 days, venv and node caches unused
for 14 days, `.tmp-*` install directories older than an hour, and cycles with no runs left and no artifact URL idle
for 45 days, then runs `git worktree prune` in each repository a removed run verified (teardown itself never
prunes). `calibration/`, the simplify queue and the audit ledger are out of its reach.

**Ports** are free only if `127.0.0.1`, `::1` and `0.0.0.0` all bind. The queue has its own lock (`queue.lock`).

## License

MIT. See [LICENSE](LICENSE).
