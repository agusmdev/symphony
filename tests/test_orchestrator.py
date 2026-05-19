from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from symphony.config import build_config
from symphony.errors import TrackerError
from symphony.models import (
    Issue,
    JsonObject,
    RunningEntry,
    WorkflowDefinition,
    utc_now,
)
from symphony.orchestrator import Orchestrator
from symphony.workspace import WorkspaceManager


def _orchestrator(tmp_path: Path, tracker: Any) -> Orchestrator:
    definition = WorkflowDefinition(
        {
            "tracker": {"kind": "linear", "api_key": "x", "project_slug": "proj"},
            "workspace": {"root": str(tmp_path)},
        },
        "",
    )
    config = build_config(definition)
    workspace = WorkspaceManager(config.workspace, config.hooks)
    runner = _NoopRunner()
    return Orchestrator(config, tracker, workspace, runner, definition)


class _NoopRunner:
    async def run(self, *_args: Any, **_kwargs: Any) -> object:
        return None


class _RecordingTracker:
    def __init__(
        self,
        *,
        candidates: list[Issue] | None = None,
        by_states: dict[str, list[Issue]] | None = None,
        by_ids: dict[str, list[Issue]] | None = None,
        by_ids_error: Exception | None = None,
        candidates_error: Exception | None = None,
    ) -> None:
        self._candidates = candidates or []
        self._by_states = by_states or {}
        self._by_ids = by_ids or {}
        self._by_ids_error = by_ids_error
        self._candidates_error = candidates_error
        self.fetch_states_calls: list[tuple[str, ...]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        if self._candidates_error is not None:
            raise self._candidates_error
        return list(self._candidates)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        key = ",".join(sorted(state_names))
        return list(self._by_states.get(key, []))

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if self._by_ids_error is not None:
            raise self._by_ids_error
        self.fetch_states_calls.append(tuple(issue_ids))
        key = ",".join(sorted(issue_ids))
        return list(self._by_ids.get(key, []))


def _issue(
    *,
    id: str = "i1",
    identifier: str = "ABC-1",
    state: str = "Todo",
    assigned_to_worker: bool = True,
    assignee_id: str | None = None,
    blocked_by: list[Any] | None = None,
    priority: int | None = None,
    created_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="Title",
        description=None,
        priority=priority,
        state=state,
        branch_name=None,
        url=None,
        assignee_id=assignee_id,
        assigned_to_worker=assigned_to_worker,
        blocked_by=blocked_by or [],
        created_at=created_at,
    )


def test_should_dispatch_rejects_unassigned(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, _RecordingTracker())
    issue = _issue(assigned_to_worker=False)
    assert orchestrator.should_dispatch(issue) is False


def test_should_dispatch_accepts_assigned(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, _RecordingTracker())
    issue = _issue(assigned_to_worker=True)
    assert orchestrator.should_dispatch(issue) is True


def test_should_dispatch_accepts_human_review_state(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, _RecordingTracker())
    issue = _issue(state="Human Review")
    assert orchestrator.should_dispatch(issue) is True


def test_should_dispatch_rejects_done(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, _RecordingTracker())
    issue = _issue(state="Done")
    assert orchestrator.should_dispatch(issue) is False


@pytest.mark.asyncio
async def test_revalidate_skips_when_no_longer_eligible(tmp_path: Path) -> None:
    stale = _issue(state="Todo")
    current = _issue(state="Done")
    tracker = _RecordingTracker(by_ids={stale.id: [current]})
    orchestrator = _orchestrator(tmp_path, tracker)

    result = await orchestrator._revalidate_for_dispatch(stale)

    assert result is None


@pytest.mark.asyncio
async def test_revalidate_skips_when_missing(tmp_path: Path) -> None:
    stale = _issue()
    tracker = _RecordingTracker(by_ids={})
    orchestrator = _orchestrator(tmp_path, tracker)

    result = await orchestrator._revalidate_for_dispatch(stale)

    assert result is None


@pytest.mark.asyncio
async def test_revalidate_returns_refreshed_issue(tmp_path: Path) -> None:
    stale = _issue(state="Todo")
    fresher = _issue(state="In Progress", priority=2)
    tracker = _RecordingTracker(by_ids={stale.id: [fresher]})
    orchestrator = _orchestrator(tmp_path, tracker)

    result = await orchestrator._revalidate_for_dispatch(stale)

    assert result is fresher


@pytest.mark.asyncio
async def test_revalidate_skips_on_tracker_error(tmp_path: Path) -> None:
    stale = _issue()
    tracker = _RecordingTracker(by_ids_error=TrackerError("linear_api_status", "boom"))
    orchestrator = _orchestrator(tmp_path, tracker)

    result = await orchestrator._revalidate_for_dispatch(stale)

    assert result is None


@pytest.mark.asyncio
async def test_reconcile_terminates_terminal(tmp_path: Path) -> None:
    running = _issue(state="Todo")
    after = _issue(state="Done")
    tracker = _RecordingTracker(by_ids={running.id: [after]})
    orchestrator = _orchestrator(tmp_path, tracker)
    orchestrator.state.running[running.id] = _make_running_entry(running)

    await orchestrator.reconcile_running_issues()

    assert running.id not in orchestrator.state.running


@pytest.mark.asyncio
async def test_reconcile_terminates_reassigned(tmp_path: Path) -> None:
    running = _issue(state="In Progress", assigned_to_worker=True)
    reassigned = _issue(state="In Progress", assigned_to_worker=False)
    tracker = _RecordingTracker(by_ids={running.id: [reassigned]})
    orchestrator = _orchestrator(tmp_path, tracker)
    orchestrator.state.running[running.id] = _make_running_entry(running)

    await orchestrator.reconcile_running_issues()

    assert running.id not in orchestrator.state.running


@pytest.mark.asyncio
async def test_reconcile_terminates_non_active(tmp_path: Path) -> None:
    running = _issue(state="In Progress")
    moved = _issue(state="Backlog")
    tracker = _RecordingTracker(by_ids={running.id: [moved]})
    orchestrator = _orchestrator(tmp_path, tracker)
    orchestrator.state.running[running.id] = _make_running_entry(running)

    await orchestrator.reconcile_running_issues()

    assert running.id not in orchestrator.state.running


@pytest.mark.asyncio
async def test_reconcile_terminates_vanished(tmp_path: Path) -> None:
    running = _issue(state="In Progress")
    tracker = _RecordingTracker(by_ids={})
    orchestrator = _orchestrator(tmp_path, tracker)
    orchestrator.state.running[running.id] = _make_running_entry(running)

    await orchestrator.reconcile_running_issues()

    assert running.id not in orchestrator.state.running


@pytest.mark.asyncio
async def test_reconcile_keeps_active_and_refreshes_issue(tmp_path: Path) -> None:
    running = _issue(state="Todo")
    refreshed = _issue(state="In Progress", priority=1)
    tracker = _RecordingTracker(by_ids={running.id: [refreshed]})
    orchestrator = _orchestrator(tmp_path, tracker)
    entry = _make_running_entry(running)
    orchestrator.state.running[running.id] = entry

    await orchestrator.reconcile_running_issues()

    assert running.id in orchestrator.state.running
    assert orchestrator.state.running[running.id].issue is refreshed


@pytest.mark.asyncio
async def test_reconcile_keeps_running_when_refresh_fails(tmp_path: Path) -> None:
    running = _issue()
    tracker = _RecordingTracker(by_ids_error=TrackerError("linear_api_status", "boom"))
    orchestrator = _orchestrator(tmp_path, tracker)
    orchestrator.state.running[running.id] = _make_running_entry(running)

    await orchestrator.reconcile_running_issues()

    assert running.id in orchestrator.state.running


def _make_running_entry(issue: Issue) -> RunningEntry:
    task: asyncio.Task[Any] = asyncio.Task(_never_completes(), loop=_ensure_loop())
    task.cancel()
    return RunningEntry(
        issue=issue,
        worker=task,
        workspace_path=None,
        retry_attempt=None,
        started_at=utc_now(),
    )


async def _never_completes() -> None:
    await asyncio.sleep(60)


def _ensure_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _on_event(events: list[tuple[str, JsonObject]]) -> Any:
    async def _capture(event: str, payload: JsonObject) -> None:
        events.append((event, payload))

    return _capture
