from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from symphony.agent import (
    AgentRunner,
    ClaudeTmuxClient,
    _ClaudeTmuxSession,
    _pid_alive,
    _tmux_session_name,
    kill_orphan_tmux_sessions,
)
from symphony.config import ServiceConfig, build_config
from symphony.models import Issue, JsonObject, WorkflowDefinition


@pytest.mark.asyncio
async def test_claude_tmux_run_turn_passes_prompt(tmp_path: Path) -> None:
    # Stand in for `claude`: a Python script that consumes a pasted line and exits.
    # Bracketed paste markers (ESC[200~ ... ESC[201~) are tolerated and stripped.
    helper = tmp_path / "fake_claude.py"
    helper.write_text(
        "import re, sys\n"
        "data = sys.stdin.readline()\n"
        "data = re.sub(r'\\x1b\\[20[01]~', '', data)\n"
        "sys.stdout.write(data.strip() + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    command = f"{sys.executable} {shlex.quote(str(helper))}"
    config = _claude_config(
        tmp_path,
        command=command,
        api_key="linear-key",
        read_timeout_ms=100,
        stall_timeout_ms=2000,
    )
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
        prompt="hello quoted world",
        turn_number=1,
        on_event=lambda event, payload: _append(events, event, payload),
    )
    await session.stop()

    assert events[0][0] == "session_started"
    assert "tmux_session" in events[0][1]
    assert "tmux attach-session -t" in str(events[0][1].get("tmux_attach_command"))
    assert any("hello quoted world" in str(payload.get("text", "")) for _, payload in events)
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
        done_path=workspace / "done",
        mcp_config_path=mcp_path,
    )

    expected_flag = f"--mcp-config {shlex.quote(str(mcp_path))}"
    assert expected_flag in command
    assert "claude" in command
    assert "printf '%s'" in command  # writes done file


def test_tmux_command_without_mcp_config(tmp_path: Path) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    workspace = tmp_path / "ABC-5"
    workspace.mkdir()
    session = _make_session(config, workspace, "ABC-5")

    command = session._tmux_command(
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


def test_tmux_session_name_embeds_owner_pid() -> None:
    name = _tmux_session_name("ABC-7", 3)
    assert name.startswith(f"symphony-pid{os.getpid()}-ABC-7-3-")


def test_pid_alive_current_process() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_invalid_pid() -> None:
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    # Very high PID well beyond any plausible live process.
    assert _pid_alive(2**31 - 2) is False


@pytest.mark.asyncio
async def test_kill_orphan_tmux_sessions_only_kills_dead_pids() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    unique = uuid.uuid4().hex[:8]
    stale_pid = 2**31 - 2
    stale = f"symphony-pid{stale_pid}-orphan-{unique}"
    live = f"symphony-pid{os.getpid()}-live-{unique}"
    unrelated = f"symphony-no-pid-marker-{unique}"
    _tmux_start_detached(stale)
    _tmux_start_detached(live)
    _tmux_start_detached(unrelated)
    try:
        killed = await kill_orphan_tmux_sessions()
        assert stale in killed
        assert live not in killed
        assert unrelated not in killed
        assert _tmux_session_exists(stale) is False
        assert _tmux_session_exists(live) is True
        assert _tmux_session_exists(unrelated) is True
    finally:
        for name in (stale, live, unrelated):
            _tmux_kill_quiet(name)


@pytest.mark.asyncio
async def test_agent_runner_startup_cleanup_delegates_to_client(
    tmp_path: Path,
) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")
    calls: list[str] = []

    class _Recorder:
        async def start_session(self, **_: object) -> object:  # pragma: no cover
            raise AssertionError("not used by this test")

        async def startup_cleanup(self) -> None:
            calls.append("cleaned")

    runner = AgentRunner(
        config,
        workspace_manager=_DummyWorkspaceManager(),
        tracker=_DummyTracker(),
        client=_Recorder(),  # type: ignore[arg-type]
    )
    await runner.startup_cleanup()
    assert calls == ["cleaned"]


@pytest.mark.asyncio
async def test_agent_runner_startup_cleanup_swallows_failures(
    tmp_path: Path,
) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")

    class _Broken:
        async def start_session(self, **_: object) -> object:  # pragma: no cover
            raise AssertionError("not used by this test")

        async def startup_cleanup(self) -> None:
            raise RuntimeError("boom")

    runner = AgentRunner(
        config,
        workspace_manager=_DummyWorkspaceManager(),
        tracker=_DummyTracker(),
        client=_Broken(),  # type: ignore[arg-type]
    )
    # Must not raise: startup must never fail because of cleanup.
    await runner.startup_cleanup()


@pytest.mark.asyncio
async def test_agent_runner_startup_cleanup_no_client_method(
    tmp_path: Path,
) -> None:
    config = _claude_config(tmp_path, api_key="linear-key")

    class _NoCleanup:
        async def start_session(self, **_: object) -> object:  # pragma: no cover
            raise AssertionError("not used by this test")

    runner = AgentRunner(
        config,
        workspace_manager=_DummyWorkspaceManager(),
        tracker=_DummyTracker(),
        client=_NoCleanup(),  # type: ignore[arg-type]
    )
    await runner.startup_cleanup()


# Helpers


def _claude_config(
    tmp_path: Path,
    *,
    command: str = "claude",
    api_key: str | None = "linear-key",
    read_timeout_ms: int = 5000,
    stall_timeout_ms: int = 300_000,
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
                "claude": {
                    "command": command,
                    "read_timeout_ms": read_timeout_ms,
                    "stall_timeout_ms": stall_timeout_ms,
                },
            },
            "",
        )
    )


def _make_session(config: ServiceConfig, workspace: Path, identifier: str) -> _ClaudeTmuxSession:
    issue = Issue("id", identifier, "Title", None, None, "Todo", None, None)
    return _ClaudeTmuxSession(config, workspace, issue)


async def _append(events: list[tuple[str, JsonObject]], event: str, payload: JsonObject) -> None:
    events.append((event, payload))


def _tmux_start_detached(session_name: str) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "sleep", "120"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _tmux_kill_quiet(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _tmux_session_exists(session_name: str) -> bool:
    rc = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    return rc == 0


class _DummyTracker:
    async def fetch_candidate_issues(self) -> list[Issue]:  # pragma: no cover
        return []

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> list[Issue]:  # pragma: no cover
        return []

    async def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:  # pragma: no cover
        return []


class _DummyWorkspaceManager:
    async def create_for_issue(self, identifier: str) -> object:  # pragma: no cover
        raise AssertionError("not used")

    async def before_run(self, path: Path) -> None:  # pragma: no cover
        return None

    async def after_run(self, path: Path) -> None:  # pragma: no cover
        return None

    async def remove_for_issue(self, identifier: str) -> None:  # pragma: no cover
        return None
