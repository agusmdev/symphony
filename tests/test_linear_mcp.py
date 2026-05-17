from __future__ import annotations

import io
import json
import subprocess
import sys
from typing import Any

import pytest

from symphony import linear_mcp


def test_initialize_returns_server_info() -> None:
    response = linear_mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response is not None
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == linear_mcp.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "symphony-linear"
    assert "tools" in result["capabilities"]


def test_initialized_notification_is_silent() -> None:
    response = linear_mcp._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response is None


def test_tools_list_returns_linear_graphql() -> None:
    response = linear_mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert response is not None
    tools = response["result"]["tools"]
    assert tools[0]["name"] == "linear_graphql"
    assert tools[0]["inputSchema"]["type"] == "object"


def test_method_not_found_returns_jsonrpc_error() -> None:
    response = linear_mcp._handle({"jsonrpc": "2.0", "id": 7, "method": "nope"})
    assert response is not None
    assert response["error"]["code"] == -32601


def test_tools_call_unknown_tool() -> None:
    response = linear_mcp._handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "other"}}
    )
    assert response is not None
    assert response["error"]["code"] == -32601


def test_tools_call_missing_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    response = linear_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "linear_graphql", "arguments": {}},
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "query" in result["content"][0]["text"]


def test_tools_call_proxies_to_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "linear-key")
    captured: dict[str, Any] = {}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"data": {"viewer": {"id": "u1"}}}).encode()

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: int = 30) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["auth"] = request.headers.get("Authorization")
        return _FakeResponse()

    monkeypatch.setattr("symphony.linear_mcp.urllib.request.urlopen", fake_urlopen)

    response = linear_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "linear_graphql",
                "arguments": {"query": "query { viewer { id } }", "variables": {"x": 1}},
            },
        }
    )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"data": {"viewer": {"id": "u1"}}}
    body = json.loads(captured["body"].decode())
    assert body == {"query": "query { viewer { id } }", "variables": {"x": 1}}
    assert captured["auth"] == "linear-key"


def test_tools_call_marks_graphql_errors_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "linear-key")

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"errors": [{"message": "bad"}]}).encode()

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(
        "symphony.linear_mcp.urllib.request.urlopen",
        lambda *_a, **_k: _FakeResponse(),
    )

    response = linear_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "linear_graphql",
                "arguments": {"query": "query { viewer { id } }"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True


def test_tools_call_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    response = linear_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "linear_graphql",
                "arguments": {"query": "query { viewer { id } }"},
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "LINEAR_API_KEY" in result["content"][0]["text"]


def test_main_reads_lines_and_writes_responses() -> None:
    inputs = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    outputs = io.StringIO()

    assert linear_mcp.main(stdin=inputs, stdout=outputs) == 0

    lines = outputs.getvalue().strip().splitlines()
    assert len(lines) == 2
    init = json.loads(lines[0])
    tools = json.loads(lines[1])
    assert init["id"] == 1
    assert tools["result"]["tools"][0]["name"] == "linear_graphql"


def test_subprocess_smoke() -> None:
    payload = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "symphony.linear_mcp"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 2
    init = json.loads(lines[0])
    assert init["result"]["serverInfo"]["name"] == "symphony-linear"
