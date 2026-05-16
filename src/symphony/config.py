from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from symphony.errors import ConfigError
from symphony.models import WorkflowDefinition

LINEAR_ENDPOINT = "https://api.linear.app/graphql"
ACTIVE_STATES = ("Todo", "In Progress")
TERMINAL_STATES = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done")


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    kind: str | None
    endpoint: str
    api_key: str | None
    project_slug: str | None
    active_states: tuple[str, ...] = ACTIVE_STATES
    terminal_states: tuple[str, ...] = TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class PollingConfig:
    interval_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir(), "symphony_workspaces").resolve()
    )


@dataclass(frozen=True, slots=True)
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: Mapping[str, int] = field(default_factory=dict)
    harness: str = "codex"


@dataclass(frozen=True, slots=True)
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | None = None
    thread_sandbox: str | None = None
    turn_sandbox_policy: str | None = None
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    command: str = "claude"
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass(frozen=True, slots=True)
class ServerConfig:
    port: int | None = None


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    codex: CodexConfig
    claude: ClaudeConfig
    server: ServerConfig

    @property
    def active_states_normalized(self) -> set[str]:
        return {state.lower() for state in self.tracker.active_states}

    @property
    def terminal_states_normalized(self) -> set[str]:
        return {state.lower() for state in self.tracker.terminal_states}

    def validate_for_dispatch(self) -> None:
        if self.tracker.kind != "linear":
            raise ConfigError("unsupported_tracker_kind", "tracker.kind must be linear")
        if not self.tracker.api_key:
            raise ConfigError("missing_tracker_api_key", "tracker.api_key is required")
        if not self.tracker.project_slug:
            raise ConfigError("missing_tracker_project_slug", "tracker.project_slug is required")
        if self.agent.harness not in {"codex", "claude"}:
            raise ConfigError("unsupported_agent_harness", "agent.harness must be codex or claude")
        if self.agent.harness == "codex" and not self.codex.command.strip():
            raise ConfigError("missing_codex_command", "codex.command is required")
        if self.agent.harness == "claude" and not self.claude.command.strip():
            raise ConfigError("missing_claude_command", "claude.command is required")

    @property
    def harness_stall_timeout_ms(self) -> int:
        if self.agent.harness == "claude":
            return self.claude.stall_timeout_ms
        return self.codex.stall_timeout_ms


def build_config(definition: WorkflowDefinition, *, base_dir: Path | None = None) -> ServiceConfig:
    root = definition.config
    tracker = _mapping(root.get("tracker"))
    polling = _mapping(root.get("polling"))
    workspace = _mapping(root.get("workspace"))
    hooks = _mapping(root.get("hooks"))
    agent = _mapping(root.get("agent"))
    codex = _mapping(root.get("codex"))
    claude = _mapping(root.get("claude"))
    server = _mapping(root.get("server"))

    kind = _optional_str(tracker.get("kind"))
    endpoint = _str(tracker.get("endpoint"), LINEAR_ENDPOINT)
    api_key = _resolve_secret(_optional_str(tracker.get("api_key")))
    if api_key is None and kind == "linear":
        api_key = _empty_to_none(os.environ.get("LINEAR_API_KEY"))
    workspace_root = _resolve_path(
        _str(workspace.get("root"), str(Path(tempfile.gettempdir(), "symphony_workspaces"))),
        base_dir=base_dir,
    )

    return ServiceConfig(
        tracker=TrackerConfig(
            kind=kind,
            endpoint=endpoint,
            api_key=api_key,
            project_slug=_optional_str(tracker.get("project_slug")),
            active_states=_str_tuple(tracker.get("active_states"), ACTIVE_STATES),
            terminal_states=_str_tuple(tracker.get("terminal_states"), TERMINAL_STATES),
        ),
        polling=PollingConfig(interval_ms=_positive_int(polling.get("interval_ms"), 30_000)),
        workspace=WorkspaceConfig(root=workspace_root),
        hooks=HooksConfig(
            after_create=_optional_str(hooks.get("after_create")),
            before_run=_optional_str(hooks.get("before_run")),
            after_run=_optional_str(hooks.get("after_run")),
            before_remove=_optional_str(hooks.get("before_remove")),
            timeout_ms=_positive_int(hooks.get("timeout_ms"), 60_000),
        ),
        agent=AgentConfig(
            max_concurrent_agents=_positive_int(agent.get("max_concurrent_agents"), 10),
            max_turns=_positive_int(agent.get("max_turns"), 20),
            max_retry_backoff_ms=_positive_int(agent.get("max_retry_backoff_ms"), 300_000),
            max_concurrent_agents_by_state=_state_limits(
                agent.get("max_concurrent_agents_by_state")
            ),
            harness=_str(agent.get("harness"), "codex"),
        ),
        codex=CodexConfig(
            command=_str(codex.get("command"), "codex app-server"),
            approval_policy=_optional_str(codex.get("approval_policy")),
            thread_sandbox=_optional_str(codex.get("thread_sandbox")),
            turn_sandbox_policy=_optional_str(codex.get("turn_sandbox_policy")),
            turn_timeout_ms=_positive_int(codex.get("turn_timeout_ms"), 3_600_000),
            read_timeout_ms=_positive_int(codex.get("read_timeout_ms"), 5_000),
            stall_timeout_ms=_int(codex.get("stall_timeout_ms"), 300_000),
        ),
        claude=ClaudeConfig(
            command=_str(claude.get("command"), "claude"),
            turn_timeout_ms=_positive_int(claude.get("turn_timeout_ms"), 3_600_000),
            read_timeout_ms=_positive_int(claude.get("read_timeout_ms"), 5_000),
            stall_timeout_ms=_int(claude.get("stall_timeout_ms"), 300_000),
        ),
        server=ServerConfig(port=_optional_int(server.get("port"))),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_int(value: Any, default: int) -> int:
    parsed = _int(value, default)
    return parsed if parsed > 0 else default


def _str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    result = tuple(item for item in value if isinstance(item, str) and item)
    return result or default


def _state_limits(value: Any) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, limit in value.items():
        if (
            isinstance(key, str)
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and limit > 0
        ):
            result[key.lower()] = limit
    return result


def _resolve_secret(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("$") and len(value) > 1:
        return _empty_to_none(os.environ.get(value[1:]))
    return _empty_to_none(value)


def _empty_to_none(value: str | None) -> str | None:
    return value if value else None


def _resolve_path(value: str, *, base_dir: Path | None) -> Path:
    resolved_input = _resolve_secret(value) or value
    path = Path(resolved_input).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()
