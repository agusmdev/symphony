from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from symphony.config import ServiceConfig
from symphony.errors import AgentError
from symphony.models import Issue, JsonObject, utc_now
from symphony.prompt import continuation_prompt, render_prompt
from symphony.tracker import IssueTracker
from symphony.workspace import WorkspaceManager, ensure_inside_root

AgentEventCallback = Callable[[str, JsonObject], Awaitable[None]]


class AgentClient(Protocol):
    async def run_turn(
        self,
        *,
        workspace: Path,
        issue: Issue,
        prompt: str,
        turn_number: int,
        on_event: AgentEventCallback,
    ) -> None: ...


class CodexAppServerClient:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    async def run_turn(
        self,
        *,
        workspace: Path,
        issue: Issue,
        prompt: str,
        turn_number: int,
        on_event: AgentEventCallback,
    ) -> None:
        ensure_inside_root(self._config.workspace.root, workspace)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            self._config.codex.command,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin is None or proc.stdout is None:
            raise AgentError("port_exit", "failed to open app-server pipes")
        await on_event(
            "session_started",
            {
                "event": "session_started",
                "timestamp": utc_now().isoformat(),
                "codex_app_server_pid": str(proc.pid),
                "thread_id": f"{issue.id}",
                "turn_id": str(turn_number),
                "session_id": f"{issue.id}-{turn_number}",
            },
        )
        request = {
            "method": "turn.start",
            "params": {
                "cwd": str(workspace),
                "prompt": prompt,
                "title": f"{issue.identifier}: {issue.title}",
                "approval_policy": self._config.codex.approval_policy,
                "thread_sandbox": self._config.codex.thread_sandbox,
                "turn_sandbox_policy": self._config.codex.turn_sandbox_policy,
            },
        }
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        await proc.stdin.drain()
        try:
            await asyncio.wait_for(
                self._stream_until_done(proc, on_event), self._config.codex.turn_timeout_ms / 1000
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AgentError("turn_timeout", "Codex turn timed out") from exc
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def _stream_until_done(
        self,
        proc: asyncio.subprocess.Process,
        on_event: AgentEventCallback,
    ) -> None:
        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=self._config.codex.read_timeout_ms / 1000
            )
            if not line:
                rc = await proc.wait()
                if rc == 0:
                    await on_event(
                        "turn_completed",
                        {"event": "turn_completed", "timestamp": utc_now().isoformat()},
                    )
                    return
                raise AgentError("port_exit", f"Codex app-server exited rc={rc}")
            if len(line) > 10 * 1024 * 1024:
                raise AgentError("response_error", "Codex protocol line exceeded 10MB")
            try:
                payload = json.loads(line.decode())
            except json.JSONDecodeError:
                await on_event(
                    "malformed", {"event": "malformed", "timestamp": utc_now().isoformat()}
                )
                continue
            if not isinstance(payload, dict):
                continue
            event = str(payload.get("event") or payload.get("type") or "other_message")
            await on_event(event, _json_object(payload))
            if event in {"turn_completed", "turn.done", "done"}:
                return
            if event in {"turn_failed", "turn_cancelled", "turn_input_required"}:
                raise AgentError(event, f"Codex event {event}")


class ClaudePrintClient:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    async def run_turn(
        self,
        *,
        workspace: Path,
        issue: Issue,
        prompt: str,
        turn_number: int,
        on_event: AgentEventCallback,
    ) -> None:
        ensure_inside_root(self._config.workspace.root, workspace)
        command = f"{self._config.claude.command} {shlex.quote(prompt)}"
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdout is None:
            raise AgentError("port_exit", "failed to open Claude stdout")
        await on_event(
            "session_started",
            {
                "event": "session_started",
                "timestamp": utc_now().isoformat(),
                "agent_process_pid": str(proc.pid),
                "claude_process_pid": str(proc.pid),
                "thread_id": f"{issue.id}",
                "turn_id": str(turn_number),
                "session_id": f"{issue.id}-{turn_number}",
            },
        )
        try:
            await asyncio.wait_for(
                self._stream_until_done(proc, on_event),
                self._config.claude.turn_timeout_ms / 1000,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AgentError("turn_timeout", "Claude turn timed out") from exc
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def _stream_until_done(
        self,
        proc: asyncio.subprocess.Process,
        on_event: AgentEventCallback,
    ) -> None:
        assert proc.stdout is not None
        stderr_task = None
        if proc.stderr is not None:
            stderr_task = asyncio.create_task(proc.stderr.read())
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=self._config.claude.read_timeout_ms / 1000
                )
            except TimeoutError:
                if proc.returncode is not None:
                    line = b""
                else:
                    await on_event(
                        "heartbeat",
                        {"event": "heartbeat", "timestamp": utc_now().isoformat()},
                    )
                    continue
            if not line:
                rc = await proc.wait()
                stderr = ""
                if stderr_task is not None:
                    stderr = (await stderr_task).decode(errors="replace").strip()
                if rc == 0:
                    await on_event(
                        "turn_completed",
                        {"event": "turn_completed", "timestamp": utc_now().isoformat()},
                    )
                    return
                message = f"Claude harness exited rc={rc}"
                if stderr:
                    message = f"{message}: {stderr[-1000:]}"
                raise AgentError("port_exit", message)
            if len(line) > 10 * 1024 * 1024:
                raise AgentError("response_error", "Claude protocol line exceeded 10MB")
            text = line.decode(errors="replace").rstrip()
            payload = _parse_claude_line(text)
            await on_event(str(payload.get("event") or "message"), payload)


class AgentRunner:
    def __init__(
        self,
        config: ServiceConfig,
        workspace_manager: WorkspaceManager,
        tracker: IssueTracker,
        client: AgentClient | None = None,
    ) -> None:
        self._config = config
        self._workspace_manager = workspace_manager
        self._tracker = tracker
        self._client = client or _build_client(config)

    async def run(
        self,
        issue: Issue,
        attempt: int | None,
        prompt_template: str,
        on_event: AgentEventCallback,
    ) -> Path:
        workspace = await self._workspace_manager.create_for_issue(issue.identifier)
        await self._workspace_manager.before_run(workspace.path)
        try:
            current_issue = issue
            for turn_number in range(1, self._config.agent.max_turns + 1):
                prompt = (
                    render_prompt(prompt_template, current_issue, attempt)
                    if turn_number == 1
                    else continuation_prompt(
                        current_issue, turn_number, self._config.agent.max_turns
                    )
                )
                await self._client.run_turn(
                    workspace=workspace.path,
                    issue=current_issue,
                    prompt=prompt,
                    turn_number=turn_number,
                    on_event=on_event,
                )
                refreshed = await self._tracker.fetch_issue_states_by_ids([issue.id])
                if refreshed:
                    current_issue = refreshed[0]
                if current_issue.state.lower() not in self._config.active_states_normalized:
                    break
            return workspace.path
        finally:
            await self._workspace_manager.after_run(workspace.path)


def _json_object(value: dict[str, Any]) -> JsonObject:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _build_client(config: ServiceConfig) -> AgentClient:
    if config.agent.harness == "claude":
        return ClaudePrintClient(config)
    return CodexAppServerClient(config)


def _parse_claude_line(text: str) -> JsonObject:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"event": "message", "timestamp": utc_now().isoformat(), "text": text}
    if not isinstance(payload, dict):
        return {"event": "message", "timestamp": utc_now().isoformat(), "text": text}
    result = _json_object(payload)
    result.setdefault("event", str(payload.get("type") or "message"))
    result.setdefault("timestamp", utc_now().isoformat())
    return result
