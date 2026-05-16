from __future__ import annotations

import sys
from pathlib import Path

import pytest

from symphony.agent import ClaudeTmuxClient
from symphony.config import build_config
from symphony.models import Issue, JsonObject, WorkflowDefinition


@pytest.mark.asyncio
async def test_claude_print_client_passes_prompt_to_tmux_session(tmp_path: Path) -> None:
    command = f'{sys.executable} -c "import sys; print(sys.stdin.read().strip())"'
    config = build_config(
        WorkflowDefinition(
            {
                "workspace": {"root": str(tmp_path)},
                "agent": {"harness": "claude"},
                "claude": {"command": command, "read_timeout_ms": 100},
            },
            "",
        )
    )
    events: list[tuple[str, JsonObject]] = []
    workspace = tmp_path / "ABC-1"
    workspace.mkdir()

    await ClaudeTmuxClient(config).run_turn(
        workspace=workspace,
        issue=Issue("id", "ABC-1", "Title", None, None, "Todo", None, None),
        prompt="hello 'quoted' world",
        turn_number=1,
        on_event=lambda event, payload: _append(events, event, payload),
    )

    assert events[0][0] == "session_started"
    assert "tmux_session" in events[0][1]
    assert "tmux attach-session -t" in str(events[0][1].get("tmux_attach_command"))
    assert any(payload.get("text") == "hello 'quoted' world" for _, payload in events)
    assert events[-1][0] == "turn_completed"


async def _append(
    events: list[tuple[str, JsonObject]], event: str, payload: JsonObject
) -> None:
    events.append((event, payload))
