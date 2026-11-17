"""A nightly run a waiting verification preempts is queued again with what it passed, never
cancelled; the stage it was stopped in runs again, and a killed stage never lends its failure."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from qg import executor, notify, proc, queue, release, report, reuse, runner
from qg.queue import CommandRun, Request, StageState

VERIFY = """
version: 1
deps: {python_sync: "true", node_packages: [], node_install: "true"}
postgres: {image: "postgres:15-alpine", db: "d"}
stages:
  static: ["true"]
  backend: ["true"]
  frontend: []
  playwright: {critical: [], surface_map: "m.yml", run: "true {specs}"}
deploy: {prod: {stages: [static, backend], max_p0: 0, p1_acknowledged: false}}
"""
SHA, TREE = "a" * 40, "b" * 40
N0, N1, V1 = "20261117-010000-aaaa", "20261117-020000-bbbb", "20261117-020500-cccc"
STATIC, BACKEND = "static-fingerprint", "backend-fingerprint"
KILLED = CommandRun(command="uv run pytest", code=143)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(report, "write", lambda state, request: None)
    monkeypatch.setattr(release, "sync", lambda run_id: [])
    monkeypatch.setattr(executor.topics, "refresh", lambda run_id: None)
    path = tmp_path.resolve() / "repo"
    path.mkdir()
    return path


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(notify, "finished", calls.append)
    return calls


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(proc, "stop_scope", calls.append)
    return calls


def asked(repo: Path, run_id: str, **kwargs: object) -> Request:
    base = Request(
        id=run_id,
        repo=str(repo),
        branch="master",
        sha=SHA,
        tree=TREE,
        feature="nightly",
        plan="",
        env="prod",
        qa=False,
        retest_of=None,
        stage_set=["static", "backend"],
        config=yaml.safe_load(VERIFY),
        cycle="nightly-aaaa",
        created="2026-11-17T02:00:00+00:00",
        config_ref="refs/heads/master",
        config_sha="c" * 40,
        reuse=False,
        mode="nightly",
    )
    return replace(base, **kwargs)


def running(request: Request, backend: StageState) -> None:
    queue.enqueue(request)

    def start(state: queue.RunState) -> None:
        state.status = "running"
        state.stages["setup"] = StageState(status="passed")
        state.stages["static"] = StageState(status="passed", fingerprint=STATIC)
        state.stages["backend"] = backend

    queue.update(request.id, start)


def preempt(run_id: str) -> None:
    waiting = queue.enqueue(asked(Path(queue.load(run_id).repo), V1, mode="verify", cycle="f"))
    runner._preempt_nightly([queue.load(run_id)], [waiting[0]])


def requeued_of(run_id: str) -> queue.RunState:
    [again] = [run for run in queue.list_runs() if run.retest_of == run_id]
    return again


def test_a_waiting_verification_stops_the_nightly_once_without_cancelling_it(
    repo: Path, stopped: list[str]
) -> None:
    running(asked(repo, N1), StageState(status="running"))

    preempt(N1)
    runner._preempt_nightly([queue.load(N1)], [queue.load(V1)])

    assert stopped == [N1]
    assert executor.preempted_for(N1) == V1
    assert queue.load(N1).cancel_requested is False


def test_a_waiting_nightly_never_preempts_a_running_nightly(repo: Path, stopped: list[str]) -> None:
    running(asked(repo, N0), StageState(status="running"))
    other = queue.enqueue(asked(repo, N1, cycle="nightly-other"))[0]

    runner._preempt_nightly([queue.load(N0)], [other])

    assert stopped == []
    assert executor.preempted_for(N0) is None


def test_a_preempted_nightly_ends_interrupted_and_is_queued_again_with_reuse(
    repo: Path, stopped: list[str], announced: list[str]
) -> None:
    running(asked(repo, N1), StageState(status="running", fingerprint=BACKEND))
    preempt(N1)

    executor.finalize(N1, proc.Cancelled(), True)

    ended = queue.load(N1)
    again = requeued_of(N1)
    assert ended.status == "interrupted"
    assert ended.error == f"preempted for run {V1}; queued again as run {again.id}"
    assert ended.stages["backend"].status == "failed"
    assert again.status == "queued"
    assert again.mode == "nightly"
    assert (again.sha, again.tree, again.cycle) == (SHA, TREE, "nightly-aaaa")
    assert again.waiting == {"preempted": f"queued again: run {N1} was preempted for run {V1}"}
    assert queue.load_request(again.id).reuse is True
    assert announced == []


@pytest.mark.parametrize(
    "backend",
    [
        StageState(status="running", fingerprint=BACKEND),
        StageState(status="failed", fingerprint=BACKEND, commands=[KILLED]),
    ],
    ids=["stopped while running", "recorded as failed by the kill"],
)
def test_the_requeued_run_keeps_the_passed_stage_and_runs_the_stopped_one_again(
    repo: Path, stopped: list[str], announced: list[str], backend: StageState
) -> None:
    running(asked(repo, N1), backend)
    preempt(N1)
    executor.finalize(N1, proc.Cancelled(), True)

    request = queue.load_request(requeued_of(N1).id)

    found = reuse.stage(request, "static", TREE, STATIC)
    assert found is not None and found[0] == N1
    assert reuse.stage(request, "backend", TREE, BACKEND) is None


def test_a_nightly_preempted_again_keeps_coming_back_with_what_it_passed(
    repo: Path, stopped: list[str], announced: list[str]
) -> None:
    running(asked(repo, N1), StageState(status="running", fingerprint=BACKEND))
    preempt(N1)
    executor.finalize(N1, proc.Cancelled(), True)
    second = requeued_of(N1)
    request = queue.load_request(second.id)
    held = reuse.stage(request, "static", TREE, STATIC)
    assert held is not None

    def start(state: queue.RunState) -> None:
        state.status = "running"
        state.stages["setup"] = StageState(status="passed")
        state.stages["static"] = reuse.reused(held[1], held[0], STATIC, "2026-11-17T03:00:00+00:00")
        state.stages["backend"] = StageState(status="running", fingerprint=BACKEND)

    queue.update(second.id, start)
    queue.set_waiting(second.id, "preempted", None)
    waiting = queue.enqueue(asked(repo, "20261117-040000-dddd", mode="verify", cycle="g"))[0]
    runner._preempt_nightly([queue.load(second.id)], [waiting])
    executor.finalize(second.id, proc.Cancelled(), True)

    third = requeued_of(second.id)
    found = reuse.stage(queue.load_request(third.id), "static", TREE, STATIC)
    assert queue.load(second.id).status == "interrupted"
    assert third.status == "queued"
    assert found is not None and found[0] == second.id
    assert report.reuse_problems(queue.load(second.id)) == []
    assert announced == []


def test_the_operator_cancel_of_a_preempted_nightly_is_final(
    repo: Path, stopped: list[str], announced: list[str]
) -> None:
    running(asked(repo, N1), StageState(status="running"))
    preempt(N1)
    queue.update(N1, lambda state: setattr(state, "cancel_requested", True))

    executor.finalize(N1, proc.Cancelled(), True)

    assert queue.load(N1).status == "cancelled"
    assert queue.load(N1).error == "cancelled by the operator"
    assert [run.id for run in queue.list_runs() if run.retest_of == N1] == []
    assert announced == [N1]


def test_the_requeued_nightly_never_reuses_an_older_nightly_of_the_same_tree(
    repo: Path, stopped: list[str], announced: list[str]
) -> None:
    running(asked(repo, N0), StageState(status="passed", fingerprint=BACKEND))
    executor.finalize(N0, None, True)
    running(asked(repo, N1), StageState(status="running", fingerprint=BACKEND))
    preempt(N1)
    executor.finalize(N1, proc.Cancelled(), True)

    request = queue.load_request(requeued_of(N1).id)

    assert queue.load(N0).status == "passed"
    assert reuse.stage(request, "backend", TREE, BACKEND) is None
    found = reuse.stage(request, "static", TREE, STATIC)
    assert found is not None and found[0] == N1


@pytest.mark.parametrize(("code", "lent"), [(1, True), (143, False), (137, False)])
def test_a_failed_run_lends_a_stage_failure_only_when_no_command_was_killed(
    repo: Path, announced: list[str], code: int, lent: bool
) -> None:
    command = CommandRun(command="uv run pytest", code=code)
    verify = asked(repo, N1, mode="verify", reuse=True, cycle="f")
    running(verify, StageState(status="failed", fingerprint=BACKEND, commands=[command]))
    executor.finalize(N1, None, True)
    later = replace(verify, id=V1, retest_of=N1)

    found = reuse.stage(later, "backend", TREE, BACKEND)

    assert queue.load(N1).status == "failed"
    assert (found is not None) is lent


def test_a_preempted_nightly_begins_no_further_stage(repo: Path, stopped: list[str]) -> None:
    running(asked(repo, N1), StageState(status="pending"))
    preempt(N1)

    with pytest.raises(proc.Cancelled):
        executor._begin(N1, "backend")

    assert queue.load(N1).stages["backend"].status == "pending"


def test_a_nightly_past_its_deadline_is_never_preempted(repo: Path, stopped: list[str]) -> None:
    running(asked(repo, N1), StageState(status="running"))
    queue.update(N1, lambda state: setattr(state, "deadline_exceeded", True))

    preempt(N1)

    assert stopped == []
    assert executor.preempted_for(N1) is None
