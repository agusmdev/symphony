from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from symphony.agent import CodexSession
from symphony.config import ServiceConfig, build_config
from symphony.errors import AgentError
from symphony.models import Issue, JsonObject, WorkflowDefinition


def _config(tmp_path: Path, **codex_overrides: Any) -> ServiceConfig:
    codex_block: dict[str, Any] = {"command": "codex app-server"}
    codex_block.update(codex_overrides)
    return build_config(
        WorkflowDefinition(
            {
                "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
                "workspace": {"root": str(tmp_path)},
                "codex": codex_block,
            },
            "",
        )
    )


def _session(
    tmp_path: Path,
    *,
    tools: dict[str, Any] | None = None,
    codex_overrides: dict[str, Any] | None = None,
) -> CodexSession:
    config = _config(tmp_path, **(codex_overrides or {}))
    workspace = tmp_path / "ABC-1"
    workspace.mkdir()
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = 4242
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.is_closing = MagicMock(return_value=True)  # block _send
    proc.stderr = None
    issue = Issue("id", "ABC-1", "Title", None, None, "Todo", None, None)
    return CodexSession(
        config=config,
        proc=proc,
        workspace=workspace,
        issue=issue,
        tool_map=tools or {},
        tool_specs=[],
    )


def _capture_events(session: CodexSession) -> list[tuple[str, JsonObject]]:
    events: list[tuple[str, JsonObject]] = []

    async def on_event(event: str, payload: JsonObject) -> None:
        events.append((event, payload))

    session._current_turn_events = on_event
    return events


def _capture_outgoing(session: CodexSession) -> list[JsonObject]:
    sent: list[JsonObject] = []

    async def fake_send(message: JsonObject) -> None:
        sent.append(message)

    session._send = fake_send  # type: ignore[method-assign]
    return sent


def _new_turn(session: CodexSession) -> asyncio.Future[None]:
    loop = asyncio.get_event_loop()
    future: asyncio.Future[None] = loop.create_future()
    session._turn_done = future
    return future


@pytest.mark.asyncio
async def test_dispatch_routes_turn_completed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    events = _capture_events(session)
    turn_done = _new_turn(session)

    await session._dispatch({"method": "turn/completed"})

    assert turn_done.done()
    assert turn_done.result() is None
    assert events[0][0] == "turn_completed"


@pytest.mark.asyncio
async def test_dispatch_routes_turn_failed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _capture_events(session)
    turn_done = _new_turn(session)

    await session._dispatch({"method": "turn/failed", "params": {"message": "boom"}})

    assert turn_done.done()
    with pytest.raises(AgentError, match="turn_failed"):
        turn_done.result()


@pytest.mark.asyncio
async def test_dispatch_routes_turn_cancelled(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _capture_events(session)
    turn_done = _new_turn(session)

    await session._dispatch({"method": "turn/cancelled"})

    assert turn_done.done()
    with pytest.raises(AgentError, match="turn_cancelled"):
        turn_done.result()


@pytest.mark.asyncio
async def test_dispatch_tool_call_invokes_tool_and_responds(tmp_path: Path) -> None:
    class _Tool:
        name = "linear_graphql"
        description = "x"
        input_schema = {"type": "object"}

        async def execute(self, arguments: Any) -> JsonObject:
            return {
                "success": True,
                "output": json.dumps({"data": {"ok": arguments}}),
                "contentItems": [],
            }

    tool = _Tool()
    session = _session(tmp_path, tools={"linear_graphql": tool})
    events = _capture_events(session)
    sent = _capture_outgoing(session)

    await session._dispatch(
        {
            "id": 99,
            "method": "item/tool/call",
            "params": {"name": "linear_graphql", "arguments": {"q": 1}},
        }
    )

    assert sent[0]["id"] == 99
    result = sent[0]["result"]
    assert isinstance(result, dict)
    assert result["success"] is True
    assert events[-1][0] == "tool_call_completed"


@pytest.mark.asyncio
async def test_dispatch_tool_call_unknown_tool(tmp_path: Path) -> None:
    session = _session(tmp_path, tools={})
    events = _capture_events(session)
    sent = _capture_outgoing(session)

    await session._dispatch(
        {
            "id": 11,
            "method": "item/tool/call",
            "params": {"name": "unknown_tool", "arguments": {}},
        }
    )

    assert sent[0]["id"] == 11
    result = sent[0]["result"]
    assert isinstance(result, dict)
    assert result["success"] is False
    assert events[-1][0] == "unsupported_tool_call"


@pytest.mark.asyncio
async def test_dispatch_approval_auto_approved_when_never(tmp_path: Path) -> None:
    session = _session(tmp_path, codex_overrides={"approval_policy": "never"})
    events = _capture_events(session)
    sent = _capture_outgoing(session)

    await session._dispatch(
        {
            "id": 5,
            "method": "item/commandExecution/requestApproval",
            "params": {"cmd": "ls"},
        }
    )

    assert sent[0]["id"] == 5
    result = sent[0]["result"]
    assert isinstance(result, dict)
    assert result["decision"] == "acceptForSession"
    assert events[-1][0] == "approval_auto_approved"


@pytest.mark.asyncio
async def test_dispatch_approval_blocks_turn_when_not_never(tmp_path: Path) -> None:
    session = _session(tmp_path)  # default approval_policy is reject map
    _capture_events(session)
    sent = _capture_outgoing(session)
    turn_done = _new_turn(session)

    await session._dispatch(
        {
            "id": 6,
            "method": "item/commandExecution/requestApproval",
            "params": {"cmd": "ls"},
        }
    )

    assert sent == []
    assert turn_done.done()
    with pytest.raises(AgentError, match="approval_required"):
        turn_done.result()


@pytest.mark.asyncio
async def test_dispatch_user_input_auto_answered_when_never(tmp_path: Path) -> None:
    session = _session(tmp_path, codex_overrides={"approval_policy": "never"})
    _capture_events(session)
    sent = _capture_outgoing(session)

    await session._dispatch(
        {
            "id": 7,
            "method": "item/tool/requestUserInput",
            "params": {"prompt": "really?"},
        }
    )

    assert sent[0]["id"] == 7
    result = sent[0]["result"]
    assert isinstance(result, dict)
    answer = result["answer"]
    assert isinstance(answer, str)
    assert "non-interactive" in answer


@pytest.mark.asyncio
async def test_dispatch_apply_patch_approval(tmp_path: Path) -> None:
    session = _session(tmp_path, codex_overrides={"approval_policy": "never"})
    _capture_events(session)
    sent = _capture_outgoing(session)

    await session._dispatch(
        {"id": 9, "method": "applyPatchApproval", "params": {"file": "x.py"}}
    )

    assert sent[0]["id"] == 9
    result = sent[0]["result"]
    assert isinstance(result, dict)
    assert result["decision"] == "approved_for_session"


@pytest.mark.asyncio
async def test_dispatch_response_resolves_pending_future(tmp_path: Path) -> None:
    session = _session(tmp_path)
    loop = asyncio.get_event_loop()
    future: asyncio.Future[JsonObject] = loop.create_future()
    session._pending[42] = future

    await session._dispatch({"id": 42, "result": {"thread": {"id": "t-1"}}})

    assert future.done()
    assert future.result() == {"thread": {"id": "t-1"}}


@pytest.mark.asyncio
async def test_dispatch_response_propagates_error(tmp_path: Path) -> None:
    session = _session(tmp_path)
    loop = asyncio.get_event_loop()
    future: asyncio.Future[JsonObject] = loop.create_future()
    session._pending[42] = future

    await session._dispatch({"id": 42, "error": {"code": -1, "message": "nope"}})

    with pytest.raises(AgentError, match="nope"):
        future.result()


@pytest.mark.asyncio
async def test_dispatch_emits_notification_for_unknown_method(tmp_path: Path) -> None:
    session = _session(tmp_path)
    events = _capture_events(session)

    await session._dispatch({"method": "thread/tokenUsage/updated", "params": {}})

    assert events[-1][0] == "notification"


def test_default_approval_policy_disables_auto_approve(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert session._auto_approve is False


def test_never_approval_policy_enables_auto_approve(tmp_path: Path) -> None:
    session = _session(tmp_path, codex_overrides={"approval_policy": "never"})
    assert session._auto_approve is True


def test_default_turn_sandbox_policy_anchored_to_workspace(tmp_path: Path) -> None:
    session = _session(tmp_path)
    policy = session._turn_sandbox_policy
    assert policy is not None
    assert policy["type"] == "workspaceWrite"
    writable = policy["writableRoots"]
    assert isinstance(writable, list)
    assert str(tmp_path / "ABC-1") in writable
