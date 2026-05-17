from __future__ import annotations

import json

import pytest

from symphony.errors import TrackerError
from symphony.models import Issue, JsonObject
from symphony.tools import LinearGraphqlTool, tool_specs


class FakeTracker:
    def __init__(self, response: JsonObject | None = None, exc: Exception | None = None) -> None:
        self._response = response or {}
        self._exc = exc
        self.calls: list[tuple[str, JsonObject]] = []

    async def graphql(self, query: str, variables: JsonObject) -> JsonObject:
        self.calls.append((query, variables))
        if self._exc is not None:
            raise self._exc
        return self._response

    # Unused tracker interface methods (Protocol compatibility).
    async def fetch_candidate_issues(self) -> list[Issue]:  # pragma: no cover
        return []

    async def fetch_issues_by_states(self, _states: list[str]) -> list[Issue]:  # pragma: no cover
        return []

    async def fetch_issue_states_by_ids(self, _ids: list[str]) -> list[Issue]:  # pragma: no cover
        return []

    async def resolve_viewer_id(self) -> str | None:  # pragma: no cover
        return None

    async def create_comment(self, _id: str, _body: str) -> None:  # pragma: no cover
        return None

    async def update_issue_state(self, _id: str, _state: str) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_linear_graphql_tool_executes_dict_arguments() -> None:
    tracker = FakeTracker(response={"data": {"issue": {"id": "abc"}}})
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"query": "query { viewer { id } }", "variables": {"x": 1}})

    assert tracker.calls == [("query { viewer { id } }", {"x": 1})]
    assert result["success"] is True
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload == {"data": {"issue": {"id": "abc"}}}
    assert result["contentItems"] == [
        {"type": "inputText", "text": result["output"]},
    ]


@pytest.mark.asyncio
async def test_linear_graphql_tool_executes_string_arguments() -> None:
    tracker = FakeTracker(response={"data": {"viewer": {"id": "u1"}}})
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute("query { viewer { id } }")

    assert result["success"] is True
    assert tracker.calls == [("query { viewer { id } }", {})]


@pytest.mark.asyncio
async def test_linear_graphql_tool_missing_query() -> None:
    tracker = FakeTracker()
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"variables": {}})

    assert result["success"] is False
    assert tracker.calls == []
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"]["code"] == "missing_query"


@pytest.mark.asyncio
async def test_linear_graphql_tool_invalid_variables() -> None:
    tracker = FakeTracker()
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"query": "query { viewer { id } }", "variables": "nope"})

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"]["code"] == "invalid_variables"
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_linear_graphql_tool_invalid_argument_type() -> None:
    tracker = FakeTracker()
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute(123)

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_linear_graphql_tool_empty_string_argument() -> None:
    tracker = FakeTracker()
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute("   ")

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"]["code"] == "missing_query"


@pytest.mark.asyncio
async def test_linear_graphql_tool_tracker_error_missing_key() -> None:
    tracker = FakeTracker(exc=TrackerError("missing_tracker_api_key", "no key"))
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"query": "query { viewer { id } }"})

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"]["code"] == "missing_tracker_api_key"
    assert "LINEAR_API_KEY" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_linear_graphql_tool_tracker_error_generic() -> None:
    tracker = FakeTracker(exc=TrackerError("linear_api_status", "HTTP 503"))
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"query": "query { viewer { id } }"})

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["error"] == {"code": "linear_api_status", "message": "HTTP 503"}


@pytest.mark.asyncio
async def test_linear_graphql_tool_marks_graphql_errors_as_failure() -> None:
    tracker = FakeTracker(response={"errors": [{"message": "bad"}], "data": None})
    tool = LinearGraphqlTool(tracker)

    result = await tool.execute({"query": "query { viewer { id } }"})

    assert result["success"] is False
    payload = json.loads(result["output"])  # type: ignore[arg-type]
    assert payload["errors"][0]["message"] == "bad"


def test_tool_specs_shape() -> None:
    tool = LinearGraphqlTool(FakeTracker())
    specs = tool_specs([tool])
    assert len(specs) == 1
    assert specs[0]["name"] == "linear_graphql"
    assert specs[0]["description"]
    schema = specs[0]["inputSchema"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "query" in properties
