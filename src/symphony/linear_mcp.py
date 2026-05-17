"""Minimal stdio MCP server exposing a `linear_graphql` tool.

Symphony spawns this as a child process under Claude Code so Claude can reach
Linear with the same auth Symphony itself uses. The protocol implemented here
is the subset of Model Context Protocol that Claude Code needs:

- `initialize` -> server info + tools capability
- `tools/list` -> a single `linear_graphql` tool spec
- `tools/call` -> execute the GraphQL operation, return the response as text content

Authentication is read from the `LINEAR_API_KEY` environment variable (which
Symphony injects when launching Claude).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import IO, Any

PROTOCOL_VERSION = "2024-11-05"
LINEAR_ENDPOINT = os.environ.get("LINEAR_ENDPOINT", "https://api.linear.app/graphql")
SERVER_NAME = "symphony-linear"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "linear_graphql"
TOOL_DESCRIPTION = (
    "Execute a raw GraphQL query or mutation against Linear using Symphony's configured auth."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
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


def main(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> int:
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    for line in iter(inp.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = _handle(message)
        if response is not None:
            _write(response, out)
    return 0


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return _ok(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _ok(
            msg_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_INPUT_SCHEMA,
                    }
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _err(msg_id, -32602, "invalid params")
        name = params.get("name")
        if name != TOOL_NAME:
            return _err(msg_id, -32601, f"unknown tool: {name!r}")
        arguments = params.get("arguments") or {}
        return _ok(msg_id, _call_linear(arguments))
    if msg_id is None:
        return None
    return _err(msg_id, -32601, f"method not found: {method!r}")


def _call_linear(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        query = arguments.strip()
        variables: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        query_value = arguments.get("query")
        if not isinstance(query_value, str):
            return _tool_error("linear_graphql requires a `query` string argument.")
        query = query_value.strip()
        variables_value = arguments.get("variables") or {}
        if not isinstance(variables_value, dict):
            return _tool_error("linear_graphql.variables must be an object when provided.")
        variables = variables_value
    else:
        return _tool_error("linear_graphql expects either a string or an object argument.")
    if not query:
        return _tool_error("linear_graphql requires a non-empty `query`.")
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        return _tool_error(
            "Symphony is missing Linear auth. Set LINEAR_API_KEY in the environment."
        )
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        LINEAR_ENDPOINT,
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return _tool_error(f"Linear HTTP {exc.code}: {exc.reason}")
    except OSError as exc:
        return _tool_error(f"Linear request failed: {exc}")
    if status != 200:
        return _tool_error(f"Linear HTTP {status}")
    try:
        parsed = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _tool_error(f"Linear returned non-JSON payload: {exc}")
    text = json.dumps(parsed, indent=2)
    is_error = isinstance(parsed, dict) and bool(parsed.get("errors"))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(payload: dict[str, Any], out: IO[str]) -> None:
    out.write(json.dumps(payload) + "\n")
    out.flush()


if __name__ == "__main__":
    raise SystemExit(main())
