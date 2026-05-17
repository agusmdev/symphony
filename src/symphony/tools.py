from __future__ import annotations

import json
from typing import Any, Protocol

from symphony.errors import TrackerError
from symphony.models import JsonObject
from symphony.tracker import IssueTracker

LINEAR_GRAPHQL_TOOL = "linear_graphql"
LINEAR_GRAPHQL_DESCRIPTION = (
    "Execute a raw GraphQL query or mutation against Linear using Symphony's configured auth."
)
LINEAR_GRAPHQL_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {
            "type": "string",
            "description": "GraphQL query or mutation document to execute against Linear.",
        },
        "variables": {
            "type": ["object", "null"],
            "description": "Optional GraphQL variables object.",
            "additionalProperties": True,
        },
    },
}


class Tool(Protocol):
    name: str
    description: str
    input_schema: JsonObject

    async def execute(self, arguments: Any) -> JsonObject: ...


class LinearGraphqlTool:
    name = LINEAR_GRAPHQL_TOOL
    description = LINEAR_GRAPHQL_DESCRIPTION
    input_schema = LINEAR_GRAPHQL_INPUT_SCHEMA

    def __init__(self, tracker: IssueTracker) -> None:
        self._tracker = tracker

    async def execute(self, arguments: Any) -> JsonObject:
        try:
            query, variables = _normalize_linear_args(arguments)
        except _ToolArgError as exc:
            return _tool_failure(exc.payload())
        try:
            response = await self._tracker.graphql(query, variables)
        except TrackerError as exc:
            return _tool_failure(_tracker_error_payload(exc))
        return _tool_response_from_graphql(response)


def tool_specs(tools: list[Tool]) -> list[JsonObject]:
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
        for tool in tools
    ]


class _ToolArgError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> JsonObject:
        return {"error": {"code": self.code, "message": self.message}}


def _normalize_linear_args(arguments: Any) -> tuple[str, JsonObject]:
    if isinstance(arguments, str):
        trimmed = arguments.strip()
        if not trimmed:
            raise _ToolArgError("missing_query", "linear_graphql requires a non-empty query.")
        return trimmed, {}
    if not isinstance(arguments, dict):
        raise _ToolArgError(
            "invalid_arguments",
            "linear_graphql expects a string or an object with query/variables.",
        )
    query_value = arguments.get("query")
    if not isinstance(query_value, str) or not query_value.strip():
        raise _ToolArgError("missing_query", "linear_graphql requires a non-empty query.")
    variables_value = arguments.get("variables") or {}
    if not isinstance(variables_value, dict):
        raise _ToolArgError(
            "invalid_variables", "linear_graphql.variables must be an object when provided."
        )
    return query_value.strip(), variables_value


def _tracker_error_payload(exc: TrackerError) -> JsonObject:
    if exc.code == "missing_tracker_api_key":
        message = (
            "Symphony is missing Linear auth. Set LINEAR_API_KEY or tracker.api_key in WORKFLOW.md."
        )
        return {"error": {"code": exc.code, "message": message}}
    return {"error": {"code": exc.code, "message": exc.message}}


def _tool_response_from_graphql(response: JsonObject) -> JsonObject:
    errors = response.get("errors") if isinstance(response, dict) else None
    success = not (isinstance(errors, list) and errors)
    return _tool_response(success, _encode(response))


def _tool_failure(payload: JsonObject) -> JsonObject:
    return _tool_response(False, _encode(payload))


def _tool_response(success: bool, output: str) -> JsonObject:
    return {
        "success": success,
        "output": output,
        "contentItems": [{"type": "inputText", "text": output}],
    }


def _encode(payload: Any) -> str:
    try:
        return json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(payload)
