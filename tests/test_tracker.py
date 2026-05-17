from __future__ import annotations

from typing import Any

import pytest

from symphony.config import build_config
from symphony.errors import TrackerError
from symphony.models import JsonObject, WorkflowDefinition
from symphony.tracker import LinearTracker, _normalize_issue


def _tracker(monkeypatch: pytest.MonkeyPatch, **tracker_overrides: Any) -> LinearTracker:
    tracker_block: dict[str, Any] = {
        "kind": "linear",
        "api_key": "linear-key",
        "project_slug": "proj",
    }
    tracker_block.update(tracker_overrides)
    config = build_config(
        WorkflowDefinition(
            config={"tracker": tracker_block},
            prompt_template="",
        )
    )
    return LinearTracker(config)


@pytest.mark.asyncio
async def test_graphql_proxies_to_blocking_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str, JsonObject]] = []

    def fake_blocking(
        endpoint: str, api_key: str, query: str, variables: JsonObject
    ) -> JsonObject:
        calls.append((endpoint, api_key, query, variables))
        return {"data": {"ok": True}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    result = await tracker.graphql("query { x }", {"a": 1})

    assert result == {"data": {"ok": True}}
    assert calls == [("https://api.linear.app/graphql", "linear-key", "query { x }", {"a": 1})]


@pytest.mark.asyncio
async def test_graphql_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    config = build_config(
        WorkflowDefinition(
            config={"tracker": {"kind": "linear", "project_slug": "proj"}},
            prompt_template="",
        )
    )
    tracker = LinearTracker(config)

    with pytest.raises(TrackerError, match="missing_tracker_api_key"):
        await tracker.graphql("query { x }", {})


@pytest.mark.asyncio
async def test_resolve_viewer_id_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_blocking(
        _endpoint: str, _api_key: str, query: str, _variables: JsonObject
    ) -> JsonObject:
        calls.append(query)
        return {"data": {"viewer": {"id": "viewer-123"}}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    first = await tracker.resolve_viewer_id()
    second = await tracker.resolve_viewer_id()

    assert first == "viewer-123"
    assert second == "viewer-123"
    assert len(calls) == 1, "viewer id should be cached after first lookup"


@pytest.mark.asyncio
async def test_resolve_viewer_id_raises_on_missing_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "symphony.tracker._graphql_request_blocking",
        lambda *_a: {"data": {"viewer": None}},
    )
    tracker = _tracker(monkeypatch)
    with pytest.raises(TrackerError, match="missing_linear_viewer_identity"):
        await tracker.resolve_viewer_id()


@pytest.mark.asyncio
async def test_assignee_match_resolution_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "symphony.tracker._graphql_request_blocking",
        lambda *_a: {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}},
    )
    tracker = _tracker(monkeypatch, assignee="user-id-42")

    match = await tracker._resolve_assignee_match()

    assert match == "user-id-42"


@pytest.mark.asyncio
async def test_assignee_match_resolution_me(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_blocking(
        _endpoint: str, _api_key: str, query: str, _variables: JsonObject
    ) -> JsonObject:
        captured.append(query)
        if "viewer" in query:
            return {"data": {"viewer": {"id": "viewer-77"}}}
        return {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch, assignee="me")

    match = await tracker._resolve_assignee_match()

    assert match == "viewer-77"
    assert any("viewer" in q for q in captured)


@pytest.mark.asyncio
async def test_assignee_match_no_filter_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_ASSIGNEE", raising=False)
    tracker = _tracker(monkeypatch)
    match = await tracker._resolve_assignee_match()
    assert match is None


def test_normalize_issue_assigned_to_worker_when_no_filter() -> None:
    raw = {
        "id": "i1",
        "identifier": "ABC-1",
        "title": "T",
        "state": {"name": "Todo"},
        "assignee": {"id": "someone-else"},
    }
    issue = _normalize_issue(raw, assignee_match=None)
    assert issue.assigned_to_worker is True
    assert issue.assignee_id == "someone-else"


def test_normalize_issue_assigned_to_worker_match() -> None:
    raw = {
        "id": "i1",
        "identifier": "ABC-1",
        "title": "T",
        "state": {"name": "Todo"},
        "assignee": {"id": "viewer-77"},
    }
    issue = _normalize_issue(raw, assignee_match="viewer-77")
    assert issue.assigned_to_worker is True


def test_normalize_issue_unassigned_when_filter_set() -> None:
    raw = {
        "id": "i1",
        "identifier": "ABC-1",
        "title": "T",
        "state": {"name": "Todo"},
        "assignee": {"id": "someone-else"},
    }
    issue = _normalize_issue(raw, assignee_match="viewer-77")
    assert issue.assigned_to_worker is False


def test_normalize_issue_no_assignee_with_filter() -> None:
    raw = {
        "id": "i1",
        "identifier": "ABC-1",
        "title": "T",
        "state": {"name": "Todo"},
    }
    issue = _normalize_issue(raw, assignee_match="viewer-77")
    assert issue.assignee_id is None
    assert issue.assigned_to_worker is False


@pytest.mark.asyncio
async def test_create_comment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, JsonObject]] = []

    def fake_blocking(
        _endpoint: str, _api_key: str, query: str, variables: JsonObject
    ) -> JsonObject:
        seen.append((query, variables))
        return {"data": {"commentCreate": {"success": True}}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    await tracker.create_comment("issue-1", "body text")

    assert seen[0][1] == {"issueId": "issue-1", "body": "body text"}


@pytest.mark.asyncio
async def test_create_comment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "symphony.tracker._graphql_request_blocking",
        lambda *_a: {"data": {"commentCreate": {"success": False}}},
    )
    tracker = _tracker(monkeypatch)
    with pytest.raises(TrackerError, match="comment_create_failed"):
        await tracker.create_comment("issue-1", "body")


@pytest.mark.asyncio
async def test_update_issue_state_resolves_state_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, JsonObject]] = []

    def fake_blocking(
        _endpoint: str, _api_key: str, query: str, variables: JsonObject
    ) -> JsonObject:
        seen.append((query, variables))
        if "ResolveStateId" in query:
            return {
                "data": {
                    "issue": {"team": {"states": {"nodes": [{"id": "state-id-9"}]}}}
                }
            }
        return {"data": {"issueUpdate": {"success": True}}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    await tracker.update_issue_state("issue-1", "In Progress")

    assert seen[0][1] == {"issueId": "issue-1", "stateName": "In Progress"}
    assert seen[1][1] == {"issueId": "issue-1", "stateId": "state-id-9"}


@pytest.mark.asyncio
async def test_update_issue_state_missing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "symphony.tracker._graphql_request_blocking",
        lambda *_a: {"data": {"issue": {"team": {"states": {"nodes": []}}}}},
    )
    tracker = _tracker(monkeypatch)

    with pytest.raises(TrackerError, match="state_not_found"):
        await tracker.update_issue_state("issue-1", "Nope")


@pytest.mark.asyncio
async def test_update_issue_state_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_blocking(
        _endpoint: str, _api_key: str, query: str, _variables: JsonObject
    ) -> JsonObject:
        if "ResolveStateId" in query:
            return {"data": {"issue": {"team": {"states": {"nodes": [{"id": "s1"}]}}}}}
        return {"data": {"issueUpdate": {"success": False}}}

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    with pytest.raises(TrackerError, match="issue_update_failed"):
        await tracker.update_issue_state("issue-1", "In Progress")


@pytest.mark.asyncio
async def test_fetch_candidate_issues_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[JsonObject] = [
        {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "i1",
                            "identifier": "ABC-1",
                            "title": "First",
                            "state": {"name": "Todo"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"},
                }
            }
        },
        {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "i2",
                            "identifier": "ABC-2",
                            "title": "Second",
                            "state": {"name": "In Progress"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
    ]
    page_iter = iter(pages)

    def fake_blocking(*_a: Any) -> JsonObject:
        return next(page_iter)

    monkeypatch.setattr("symphony.tracker._graphql_request_blocking", fake_blocking)
    tracker = _tracker(monkeypatch)

    issues = await tracker.fetch_candidate_issues()

    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2"]


@pytest.mark.asyncio
async def test_fetch_issue_states_by_ids_returns_empty_for_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _tracker(monkeypatch)
    assert await tracker.fetch_issue_states_by_ids([]) == []
    assert await tracker.fetch_issues_by_states([]) == []
