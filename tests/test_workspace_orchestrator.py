from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from symphony.config import build_config
from symphony.errors import WorkspaceError
from symphony.models import BlockerRef, Issue, WorkflowDefinition
from symphony.orchestrator import Orchestrator, sort_for_dispatch
from symphony.workspace import WorkspaceManager, ensure_inside_root, workspace_key


def test_workspace_key_sanitizes_identifier() -> None:
    assert workspace_key("ABC/123 x") == "ABC_123_x"


@pytest.mark.asyncio
async def test_workspace_create_reuse_and_after_create(tmp_path: Path) -> None:
    marker = tmp_path / "root" / "ABC-1" / "created"
    config = build_config(
        WorkflowDefinition(
            {
                "workspace": {"root": str(tmp_path / "root")},
                "hooks": {"after_create": "touch created"},
            },
            "",
        )
    )
    manager = WorkspaceManager(config.workspace, config.hooks)
    first = await manager.create_for_issue("ABC-1")
    second = await manager.create_for_issue("ABC-1")
    assert first.created_now is True
    assert second.created_now is False
    assert marker.exists()


def test_root_containment(tmp_path: Path) -> None:
    ensure_inside_root(tmp_path, tmp_path / "child")
    with pytest.raises(WorkspaceError):
        ensure_inside_root(tmp_path, tmp_path.parent / "outside")


def test_dispatch_sort_order() -> None:
    issues = [
        Issue("2", "B-2", "B", None, None, "Todo", None, None),
        Issue(
            "1",
            "A-1",
            "A",
            None,
            1,
            "Todo",
            None,
            None,
            created_at=datetime(2020, 1, 2, tzinfo=UTC),
        ),
        Issue(
            "3",
            "A-0",
            "C",
            None,
            1,
            "Todo",
            None,
            None,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    ]
    assert [issue.identifier for issue in sort_for_dispatch(issues)] == ["A-0", "A-1", "B-2"]


class EmptyTracker:
    async def fetch_candidate_issues(self) -> list[Issue]:
        return []

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return []

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return []


class DummyRunner:
    async def run(self, *args: Any, **kwargs: Any) -> object:
        return None


def make_orchestrator(tmp_path: Path) -> Orchestrator:
    config = build_config(
        WorkflowDefinition(
            {
                "tracker": {"kind": "linear", "api_key": "x", "project_slug": "proj"},
                "workspace": {"root": str(tmp_path)},
            },
            "",
        )
    )
    tracker = EmptyTracker()
    workspace = WorkspaceManager(config.workspace, config.hooks)
    return Orchestrator(config, tracker, workspace, DummyRunner(), "")


def test_todo_blocked_by_non_terminal_is_ineligible(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    issue = Issue(
        "id",
        "ABC-1",
        "Title",
        None,
        None,
        "Todo",
        None,
        None,
        blocked_by=[BlockerRef("b", "ABC-0", "In Progress")],
    )
    assert orchestrator.should_dispatch(issue) is False


def test_todo_blocked_by_terminal_is_eligible(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    issue = Issue(
        "id",
        "ABC-1",
        "Title",
        None,
        None,
        "Todo",
        None,
        None,
        blocked_by=[BlockerRef("b", "ABC-0", "Done")],
    )
    assert orchestrator.should_dispatch(issue) is True
