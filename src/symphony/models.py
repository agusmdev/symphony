from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BlockerRef:
    id: str | None
    identifier: str | None
    state: str | None


@dataclass(frozen=True, slots=True)
class Issue:
    id: str
    identifier: str
    title: str
    description: str | None
    priority: int | None
    state: str
    branch_name: str | None
    url: str | None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str


@dataclass(frozen=True, slots=True)
class Workspace:
    path: Path
    workspace_key: str
    created_now: bool


class RunStatus(StrEnum):
    PREPARING_WORKSPACE = "PreparingWorkspace"
    BUILDING_PROMPT = "BuildingPrompt"
    LAUNCHING_AGENT_PROCESS = "LaunchingAgentProcess"
    INITIALIZING_SESSION = "InitializingSession"
    STREAMING_TURN = "StreamingTurn"
    FINISHING = "Finishing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    STALLED = "Stalled"
    CANCELED_BY_RECONCILIATION = "CanceledByReconciliation"


@dataclass(slots=True)
class LiveSession:
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    codex_app_server_pid: str | None = None
    last_codex_event: str | None = None
    last_codex_timestamp: datetime | None = None
    last_codex_message: str | None = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


@dataclass(slots=True)
class RunningEntry:
    issue: Issue
    worker: Any
    workspace_path: Path | None
    retry_attempt: int | None
    started_at: datetime
    live_session: LiveSession = field(default_factory=LiveSession)
    cancel_requested: bool = False


@dataclass(slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: float
    error: str | None
    task: Any | None = None


@dataclass(slots=True)
class CodexTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0


@dataclass(slots=True)
class OrchestratorState:
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    codex_totals: CodexTotals = field(default_factory=CodexTotals)
    codex_rate_limits: JsonObject | None = None
