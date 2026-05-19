from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from symphony.agent import AgentRunner
from symphony.config import ServiceConfig, build_config
from symphony.models import Issue, JsonObject, StateHandler, WorkflowDefinition, Workspace


def _config(tmp_path: Path) -> ServiceConfig:
    return build_config(
        WorkflowDefinition(
            {
                "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
                "workspace": {"root": str(tmp_path)},
                "agent": {"harness": "codex", "max_turns": 5},
            },
            "",
        )
    )


class _FakeSession:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.stopped = False

    async def run_turn(
        self, *, prompt: str, turn_number: int, on_event: Any
    ) -> None:
        self.prompts.append(prompt)

    async def stop(self) -> None:
        self.stopped = True


class _FakeClient:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    async def start_session(
        self, *, workspace: Path, issue: Issue, on_event: Any
    ) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


class _FakeWorkspaceManager:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "ws"
        self.path.mkdir()

    async def create_for_issue(self, identifier: str) -> Workspace:
        return Workspace(path=self.path, workspace_key=identifier, created_now=True)

    async def before_run(self, path: Path) -> None:
        return None

    async def after_run(self, path: Path) -> None:
        return None


class _FakeTracker:
    def __init__(self, sequence: list[Issue]) -> None:
        self._sequence = list(sequence)

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> list[Issue]:
        if not self._sequence:
            return []
        return [self._sequence.pop(0)]

    async def fetch_candidate_issues(self) -> list[Issue]:  # pragma: no cover
        return []

    async def fetch_issues_by_states(
        self, state_names: list[str]
    ) -> list[Issue]:  # pragma: no cover
        return []


def _issue(state: str) -> Issue:
    return Issue("i1", "ABC-1", "T", None, None, state, None, None)


@pytest.mark.asyncio
async def test_state_change_breaks_turn_loop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    workflow = WorkflowDefinition(
        config={"tracker": {"kind": "linear"}},
        prompt_template="default body",
        state_handlers={
            "todo": StateHandler(
                harness="codex",
                prompt_template="implement {{ issue.identifier }}",
            ),
            "in review": StateHandler(
                harness="codex",
                prompt_template="review {{ issue.identifier }}",
            ),
        },
    )
    tracker = _FakeTracker(
        sequence=[
            _issue("Todo"),       # after turn 1: still Todo
            _issue("In Review"),  # after turn 2: state changed -> break
        ]
    )
    client = _FakeClient()
    runner = AgentRunner(
        config,
        workspace_manager=_FakeWorkspaceManager(tmp_path),
        tracker=tracker,  # type: ignore[arg-type]
        client=client,
    )

    async def _noop(event: str, payload: JsonObject) -> None:
        return None

    await runner.run(_issue("Todo"), None, workflow, _noop)

    # Exactly two turns ran (turn 3 would have used "review" template).
    session = client.sessions[-1]
    assert len(session.prompts) == 2
    assert "implement ABC-1" in session.prompts[0]
    # Turn 2 must still be the generic continuation under the Todo handler,
    # not anything rendered from the In Review handler — proves the break runs
    # before the next render rather than after.
    assert "review" not in session.prompts[1].lower()
    assert "Continue work on ABC-1" in session.prompts[1]
    assert session.stopped is True


@pytest.mark.asyncio
async def test_hot_reload_harness_change_breaks_loop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    initial = WorkflowDefinition(
        config={"tracker": {"kind": "linear"}},
        prompt_template="default",
        state_handlers={
            "todo": StateHandler(harness="codex", prompt_template="impl"),
        },
    )
    reloaded = WorkflowDefinition(
        config={"tracker": {"kind": "linear"}},
        prompt_template="default",
        state_handlers={
            "todo": StateHandler(harness="claude", prompt_template="impl"),
        },
    )
    # State stays Todo across turns; only the workflow's handler harness flips.
    tracker = _FakeTracker(sequence=[_issue("Todo"), _issue("Todo")])
    client = _FakeClient()
    runner = AgentRunner(
        config,
        workspace_manager=_FakeWorkspaceManager(tmp_path),
        tracker=tracker,  # type: ignore[arg-type]
        client=client,
    )
    live: dict[str, WorkflowDefinition] = {"workflow": initial}

    async def _swap_after_turn_one(
        event: str, payload: JsonObject
    ) -> None:
        if event == "swap":
            live["workflow"] = reloaded

    # Trigger the swap from inside the first turn via the event hook.
    class _SwappingSession(_FakeSession):
        async def run_turn(self, *, prompt: str, turn_number: int, on_event: Any) -> None:
            await super().run_turn(prompt=prompt, turn_number=turn_number, on_event=on_event)
            if turn_number == 1:
                await on_event("swap", {})

    swap_client = _FakeClient()

    async def _start(**kwargs: Any) -> _SwappingSession:
        session = _SwappingSession()
        swap_client.sessions.append(session)
        return session

    swap_client.start_session = _start  # type: ignore[method-assign]
    runner._clients[config.agent.harness] = swap_client

    await runner.run(_issue("Todo"), None, lambda: live["workflow"], _swap_after_turn_one)

    session = swap_client.sessions[-1]
    assert len(session.prompts) == 1, "harness change should break before turn 2"


@pytest.mark.asyncio
async def test_missing_state_handler_falls_back_to_default(tmp_path: Path) -> None:
    config = _config(tmp_path)
    workflow = WorkflowDefinition(
        config={"tracker": {"kind": "linear"}},
        prompt_template="fallback {{ issue.identifier }}",
        state_handlers={},
    )
    tracker = _FakeTracker(sequence=[_issue("In Progress")])
    client = _FakeClient()
    runner = AgentRunner(
        config,
        workspace_manager=_FakeWorkspaceManager(tmp_path),
        tracker=tracker,  # type: ignore[arg-type]
        client=client,
    )

    async def _noop(event: str, payload: JsonObject) -> None:
        return None

    await runner.run(_issue("In Progress"), None, workflow, _noop)

    session = client.sessions[-1]
    assert session.prompts
    assert "fallback ABC-1" in session.prompts[0]


@pytest.mark.asyncio
async def test_runner_caches_clients_per_harness(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient()
    runner = AgentRunner(
        config,
        workspace_manager=_FakeWorkspaceManager(tmp_path),
        tracker=_FakeTracker(sequence=[]),  # type: ignore[arg-type]
        client=client,
    )
    # Same harness asked twice: must reuse the cached instance.
    assert runner._client_for(config.agent.harness) is client
    assert runner._client_for(config.agent.harness) is client
