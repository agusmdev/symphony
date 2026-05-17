from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from symphony.agent import ClaudeTmuxClient, _ClaudeTmuxSession
from symphony.config import ServiceConfig, build_config
from symphony.models import Issue, JsonObject, WorkflowDefinition


@pytest.mark.asyncio
async def test_claude_tmux_run_turn_passes_prompt(tmp_path: Path) -> None:
    command = f'{sys.executable} -c "import sys; print(sys.stdin.read().strip())"'
    config = _claude_config(tmp_path, command=command, api_key="linear-key", read_timeout_ms=100)
    events: list[tuple[str, JsonObject]] = []
    workspace = tmp_path / "ABC-1"
    workspace.mkdir()

    client = ClaudeTmuxClient(config)
    issue = Issue("id", "ABC-1", "Title", None, None, "Todo", None, None)
    session = await client.start_session(
        workspace=workspace,
        issue=issue,
        on_event=lambda event, payload: _append(events, event, payload),
    )
    await session.run_turn(
        prompt="hello 'quoted' world",
        turn_number=1,
        on_event=lambda event, payload: _append(events, event, payload),
    )
    await session.stop()

    assert events[0][0] == "session_started"
    assert "tmux_session" in events[0][1]
    assert "tmux attach-session -t" in str(events[0][1].get("tmux_attach_command"))
    assert any(payload.get("text") == "hello 'quoted' world" for _, payload in events)
    assert events[-1][0] == "turn_completed"


def test_write_mcp_config_emits_linear_server(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    workspace = tmp_path / "ABC-2"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-2")

    mcp_path = session._write_mcp_config(workspace)
    assert mcp_path is not None
    data = json.loads(mcp_path.read_text())
    server = data["mcpServers"]["symphony-linear"]
    assert server["type"] == "stdio"
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "symphony.linear_mcp"]
    assert server["env"] == {"LINEAR_API_KEY": "linear-key"}


def test_write_mcp_config_returns_none_without_api_key(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key=None)
    workspace = tmp_path / "ABC-3"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-3")

    assert session._write_mcp_config(workspace) is None


def test_tmux_command_appends_mcp_config_flag(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    workspace = tmp_path / "ABC-4"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-4")
    mcp_path = workspace / "mcp.json"

    command = session._tmux_command(
        prompt="hi",
        log_path=workspace / "log",
        done_path=workspace / "done",
        mcp_config_path=mcp_path,
    )

    expected_flag = f"--mcp-config {shlex.quote(str(mcp_path))}"
    assert expected_flag in command
    assert "claude " in command


def test_tmux_command_without_mcp_config(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    workspace = tmp_path / "ABC-5"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-5")

    command = session._tmux_command(
        prompt="hi",
        log_path=workspace / "log",
        done_path=workspace / "done",
        mcp_config_path=None,
    )

    assert "--mcp-config" not in command


def test_claude_env_carries_api_key(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    workspace = tmp_path / "ABC-6"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-6")

    env = session._claude_env()
    assert env is not None
    assert env["LINEAR_API_KEY"] == "linear-key"


# Helpers


def _claude_config(
    tmp_path: Path,
    *,
    command: str = "claude",
    api_key: str | None = "linear-key",
    read_timeout_ms: int = 5000,
) -> ServiceConfig:
    tracker_block: dict[str, object] = {"kind": "linear", "project_slug": "proj"}
    if api_key is not None:
        tracker_block["api_key"] = api_key
    return build_config(
        WorkflowDefinition(
            {
                "tracker": tracker_block,
                "workspace": {"root": str(tmp_path)},
                "agent": {"harness": "claude"},
                "claude": {"command": command, "read_timeout_ms": read_timeout_ms},
            },
            "",
        )
    )


def _make_session(
    config: ServiceConfig, workspace: Path, identifier: str
) -> _ClaudeTmuxSession:
    issue = Issue("id", identifier, "Title", None, None, "Todo", None, None)
    return _ClaudeTmuxSession(config, workspace, issue)


async def _append(
    events: list[tuple[str, JsonObject]], event: str, payload: JsonObject
) -> None:
    events.append((event, payload))
