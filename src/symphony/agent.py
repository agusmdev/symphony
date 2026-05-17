from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from symphony.config import ServiceConfig
from symphony.errors import AgentError
from symphony.models import Issue, JsonObject, utc_now
from symphony.prompt import continuation_prompt, render_prompt
from symphony.tools import Tool, tool_specs
from symphony.tracker import IssueTracker, LinearTracker
from symphony.workspace import WorkspaceManager, ensure_inside_root

LOG = logging.getLogger("symphony.agent")

AgentEventCallback = Callable[[str, JsonObject], Awaitable[None]]

_DEFAULT_APPROVAL_POLICY: JsonObject = {
    "reject": {"sandbox_approval": True, "rules": True, "mcp_elicitations": True}
}
_DEFAULT_THREAD_SANDBOX = "workspace-write"
_SUBPROCESS_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
_MAX_PROTOCOL_LINE_BYTES = 10 * 1024 * 1024


class AgentSession(Protocol):
    async def run_turn(
        self, *, prompt: str, turn_number: int, on_event: AgentEventCallback
    ) -> None: ...

    async def stop(self) -> None: ...


class AgentClient(Protocol):
    async def start_session(
        self, *, workspace: Path, issue: Issue, on_event: AgentEventCallback
    ) -> AgentSession: ...


class CodexAppServerClient:
    def __init__(self, config: ServiceConfig, tools: list[Tool]) -> None:
        self._config = config
        self._tools = tools
        self._tool_map = {tool.name: tool for tool in tools}

    async def start_session(
        self, *, workspace: Path, issue: Issue, on_event: AgentEventCallback
    ) -> CodexSession:
        ensure_inside_root(self._config.workspace.root, workspace)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            self._config.codex.command,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_SUBPROCESS_STREAM_LIMIT_BYTES,
        )
        if proc.stdin is None or proc.stdout is None:
            raise AgentError("port_exit", "failed to open app-server pipes")
        session = CodexSession(
            config=self._config,
            proc=proc,
            workspace=workspace,
            issue=issue,
            tool_map=self._tool_map,
            tool_specs=tool_specs(self._tools),
        )
        try:
            await session._initialize(on_event)
        except BaseException:
            await session.stop()
            raise
        return session


class CodexSession:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        proc: asyncio.subprocess.Process,
        workspace: Path,
        issue: Issue,
        tool_map: dict[str, Tool],
        tool_specs: list[JsonObject],
    ) -> None:
        self._config = config
        self._proc = proc
        self._workspace = workspace
        self._issue = issue
        self._tool_map = tool_map
        self._tool_specs = tool_specs
        self._thread_id: str | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._approval_policy = config.codex.approval_policy or _DEFAULT_APPROVAL_POLICY
        self._auto_approve = self._approval_policy == "never"
        self._thread_sandbox = config.codex.thread_sandbox or _DEFAULT_THREAD_SANDBOX
        self._turn_sandbox_policy: JsonObject | None = _resolve_turn_sandbox_policy(
            config.codex.turn_sandbox_policy, workspace
        )
        self._current_turn_events: AgentEventCallback | None = None
        self._turn_done: asyncio.Future[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def _initialize(self, on_event: AgentEventCallback) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(_drain_stderr(self._proc.stderr))
        await self._request(
            "initialize",
            {
                "capabilities": {"experimentalApi": True},
                "clientInfo": {
                    "name": "symphony-orchestrator",
                    "title": "Symphony Orchestrator",
                    "version": "0.1.0",
                },
            },
        )
        await self._notify("initialized", {})
        thread_params: JsonObject = {
            "approvalPolicy": self._approval_policy,
            "sandbox": self._thread_sandbox,
            "cwd": str(self._workspace),
            "dynamicTools": list(self._tool_specs),
        }
        thread_payload = await self._request("thread/start", thread_params)
        thread = thread_payload.get("thread") if isinstance(thread_payload, dict) else None
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._thread_id = thread_id
                return
        raise AgentError("invalid_thread_payload", "thread/start missing thread.id")

    async def run_turn(
        self, *, prompt: str, turn_number: int, on_event: AgentEventCallback
    ) -> None:
        if self._stopped:
            raise AgentError("port_exit", "Codex session already stopped")
        if self._thread_id is None:
            raise AgentError("invalid_state", "Codex session not initialized")
        loop = asyncio.get_running_loop()
        self._turn_done = loop.create_future()
        self._current_turn_events = on_event
        turn_params: JsonObject = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(self._workspace),
            "title": f"{self._issue.identifier}: {self._issue.title}",
            "approvalPolicy": self._approval_policy,
        }
        if self._turn_sandbox_policy is not None:
            turn_params["sandboxPolicy"] = self._turn_sandbox_policy
        turn_response = await self._request("turn/start", turn_params)
        turn = turn_response.get("turn") if isinstance(turn_response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        session_id = (
            f"{self._thread_id}-{turn_id}" if isinstance(turn_id, str) else self._thread_id
        )
        await on_event(
            "session_started",
            {
                "event": "session_started",
                "timestamp": utc_now().isoformat(),
                "codex_app_server_pid": str(self._proc.pid),
                "thread_id": self._thread_id,
                "turn_id": str(turn_id) if turn_id is not None else None,
                "session_id": session_id,
            },
        )
        try:
            await asyncio.wait_for(
                self._turn_done, timeout=self._config.codex.turn_timeout_ms / 1000
            )
        except TimeoutError as exc:
            raise AgentError("turn_timeout", "Codex turn timed out") from exc
        finally:
            self._turn_done = None
            self._current_turn_events = None

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self._proc.returncode is None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AgentError("port_exit", "Codex session stopped"))
        self._pending.clear()
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_exception(AgentError("port_exit", "Codex session stopped"))

    async def _request(self, method: str, params: JsonObject) -> JsonObject:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "method": method, "id": request_id, "params": params})
        try:
            return await asyncio.wait_for(
                future, timeout=self._config.codex.read_timeout_ms / 1000 * 12
            )
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AgentError("response_error", f"timeout waiting for {method}") from exc

    async def _notify(self, method: str, params: JsonObject) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: JsonObject) -> None:
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            raise AgentError("port_exit", "Codex stdin unavailable")
        line = json.dumps(message).encode() + b"\n"
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    rc = await self._proc.wait()
                    self._fail_all(AgentError("port_exit", f"Codex app-server exited rc={rc}"))
                    return
                if len(line) > _MAX_PROTOCOL_LINE_BYTES:
                    self._fail_all(
                        AgentError("response_error", "Codex protocol line exceeded limit")
                    )
                    return
                try:
                    payload = json.loads(line.decode())
                except json.JSONDecodeError:
                    await self._emit("malformed", {"raw": line.decode(errors="replace").rstrip()})
                    continue
                if not isinstance(payload, dict):
                    continue
                await self._dispatch(payload)
        except asyncio.CancelledError:
            return

    async def _dispatch(self, payload: JsonObject) -> None:
        msg_id = payload.get("id")
        if "result" in payload or "error" in payload:
            if isinstance(msg_id, int) and msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if "error" in payload:
                    error = payload.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or error)
                    else:
                        message = str(error)
                    future.set_exception(AgentError("response_error", message))
                else:
                    result = payload.get("result")
                    future.set_result(result if isinstance(result, dict) else {"result": result})
            return
        method = payload.get("method")
        if not isinstance(method, str):
            await self._emit("other_message", payload)
            return
        if method in {"turn/completed", "turn.done", "done"}:
            await self._emit("turn_completed", payload)
            if self._turn_done is not None and not self._turn_done.done():
                self._turn_done.set_result(None)
            return
        if method == "turn/failed":
            await self._emit("turn_failed", payload)
            if self._turn_done is not None and not self._turn_done.done():
                self._turn_done.set_exception(AgentError("turn_failed", _summary(payload)))
            return
        if method == "turn/cancelled":
            await self._emit("turn_cancelled", payload)
            if self._turn_done is not None and not self._turn_done.done():
                self._turn_done.set_exception(AgentError("turn_cancelled", _summary(payload)))
            return
        if method == "item/tool/call":
            await self._handle_tool_call(payload)
            return
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            await self._handle_approval(payload, "acceptForSession")
            return
        if method in {"execCommandApproval", "applyPatchApproval"}:
            await self._handle_approval(payload, "approved_for_session")
            return
        if method == "item/tool/requestUserInput":
            await self._handle_user_input_request(payload)
            return
        await self._emit("notification", payload)

    async def _handle_tool_call(self, payload: JsonObject) -> None:
        params = payload.get("params") if isinstance(payload, dict) else None
        tool_name: str | None = None
        arguments: Any = {}
        if isinstance(params, dict):
            for key in ("tool", "name"):
                raw_name = params.get(key)
                if isinstance(raw_name, str) and raw_name.strip():
                    tool_name = raw_name.strip()
                    break
            arguments = params.get("arguments", {})
        msg_id = payload.get("id")
        result: JsonObject
        event = "tool_call_completed"
        if tool_name is None or tool_name not in self._tool_map:
            event = "unsupported_tool_call"
            result = {
                "success": False,
                "output": json.dumps(
                    {"error": {"message": f"Unsupported dynamic tool: {tool_name!r}"}}
                ),
                "contentItems": [],
            }
        else:
            try:
                result = await self._tool_map[tool_name].execute(arguments)
            except Exception as exc:  # noqa: BLE001 - report tool failure to Codex
                event = "tool_call_failed"
                result = {
                    "success": False,
                    "output": json.dumps({"error": {"message": str(exc)}}),
                    "contentItems": [],
                }
        if result.get("success") is False:
            event = "tool_call_failed" if event == "tool_call_completed" else event
        await self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        await self._emit(event, payload)

    async def _handle_approval(self, payload: JsonObject, decision: str) -> None:
        msg_id = payload.get("id")
        if self._auto_approve and msg_id is not None:
            await self._send({"jsonrpc": "2.0", "id": msg_id, "result": {"decision": decision}})
            await self._emit(
                "approval_auto_approved", {"event_payload": payload, "decision": decision}
            )
            return
        await self._emit("approval_required", payload)
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_exception(
                AgentError("approval_required", "approval required but auto-approve disabled")
            )

    async def _handle_user_input_request(self, payload: JsonObject) -> None:
        msg_id = payload.get("id")
        if self._auto_approve and msg_id is not None:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "answer": (
                            "This is a non-interactive session. Operator input is unavailable."
                        )
                    },
                }
            )
            await self._emit("user_input_auto_answered", payload)
            return
        await self._emit("turn_input_required", payload)
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_exception(
                AgentError("turn_input_required", _summary(payload))
            )

    async def _emit(self, event: str, payload: JsonObject) -> None:
        if self._current_turn_events is None:
            return
        await self._current_turn_events(event, _normalize_event_payload(event, payload))

    def _fail_all(self, error: AgentError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_exception(error)


class ClaudeTmuxClient:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    async def start_session(
        self, *, workspace: Path, issue: Issue, on_event: AgentEventCallback
    ) -> _ClaudeTmuxSession:
        return _ClaudeTmuxSession(self._config, workspace, issue)


class _ClaudeTmuxSession:
    def __init__(self, config: ServiceConfig, workspace: Path, issue: Issue) -> None:
        self._config = config
        self._workspace = workspace
        self._issue = issue

    async def run_turn(
        self, *, prompt: str, turn_number: int, on_event: AgentEventCallback
    ) -> None:
        ensure_inside_root(self._config.workspace.root, self._workspace)
        run_dir = Path(tempfile.mkdtemp(prefix="symphony-claude-", dir=self._workspace))
        log_path = run_dir / "claude.log"
        done_path = run_dir / "done"
        mcp_config_path = self._write_mcp_config(run_dir)
        session_name = _tmux_session_name(self._issue.identifier, turn_number)
        command = self._tmux_command(prompt, log_path, done_path, mcp_config_path)
        env = self._claude_env()
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "bash",
            "-lc",
            command,
            cwd=self._workspace,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            raise AgentError("port_exit", f"failed to start Claude tmux session: {message}")
        await on_event(
            "session_started",
            {
                "event": "session_started",
                "timestamp": utc_now().isoformat(),
                "tmux_session": session_name,
                "tmux_attach_command": f"tmux attach-session -t {shlex.quote(session_name)}",
                "claude_log_path": str(log_path),
                "thread_id": f"{self._issue.id}",
                "turn_id": str(turn_number),
                "session_id": f"{self._issue.id}-{turn_number}",
            },
        )
        try:
            await asyncio.wait_for(
                self._stream_until_done(log_path, done_path, session_name, on_event),
                self._config.claude.turn_timeout_ms / 1000,
            )
        except TimeoutError as exc:
            await _tmux_kill_session(session_name)
            raise AgentError("turn_timeout", "Claude turn timed out") from exc

    async def stop(self) -> None:
        return None

    def _claude_env(self) -> dict[str, str] | None:
        import os

        env = dict(os.environ)
        if self._config.tracker.api_key:
            env["LINEAR_API_KEY"] = self._config.tracker.api_key
        return env

    def _tmux_command(
        self, prompt: str, log_path: Path, done_path: Path, mcp_config_path: Path | None
    ) -> str:
        claude_command = self._config.claude.command
        if mcp_config_path is not None:
            claude_command = (
                f"{claude_command} --mcp-config {shlex.quote(str(mcp_config_path))}"
            )
        quoted_prompt = shlex.quote(prompt)
        quoted_log = shlex.quote(str(log_path))
        quoted_done = shlex.quote(str(done_path))
        return (
            f"touch {quoted_log}; "
            f"printf '%s\\n' {quoted_prompt} "
            f"| {claude_command} "
            f"> >(tee -a {quoted_log}) "
            f"2> >(tee -a {quoted_log} >&2); "
            "rc=${PIPESTATUS[1]}; "
            f"printf '%s' \"$rc\" > {quoted_done}; "
            'printf "\\n[symphony] Claude exited rc=%s\\n" "$rc"; '
            'exit "$rc"'
        )

    def _write_mcp_config(self, run_dir: Path) -> Path | None:
        api_key = self._config.tracker.api_key
        if not api_key:
            return None
        config_path = run_dir / "mcp.json"
        config: JsonObject = {
            "mcpServers": {
                "symphony-linear": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "symphony.linear_mcp"],
                    "env": {"LINEAR_API_KEY": api_key},
                }
            }
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    async def _stream_until_done(
        self,
        log_path: Path,
        done_path: Path,
        session_name: str,
        on_event: AgentEventCallback,
    ) -> None:
        offset = 0
        pending = b""
        while True:
            await asyncio.sleep(self._config.claude.read_timeout_ms / 1000)
            if log_path.exists():
                data = log_path.read_bytes()
                chunk = data[offset:]
                offset = len(data)
                if chunk:
                    pending += chunk
                    lines = pending.splitlines(keepends=True)
                    if lines and not lines[-1].endswith((b"\n", b"\r")):
                        pending = lines.pop()
                    else:
                        pending = b""
                    for line in lines:
                        await self._emit_claude_line(line, on_event)
                elif not done_path.exists():
                    await on_event(
                        "heartbeat",
                        {"event": "heartbeat", "timestamp": utc_now().isoformat()},
                    )
            if done_path.exists():
                if log_path.exists():
                    data = log_path.read_bytes()
                    chunk = data[offset:]
                    offset = len(data)
                    if chunk:
                        pending += chunk
                if pending:
                    await self._emit_claude_line(pending, on_event)
                    pending = b""
                rc_text = done_path.read_text(errors="replace").strip()
                rc = int(rc_text) if rc_text.isdigit() else 1
                if rc == 0:
                    await on_event(
                        "turn_completed",
                        {"event": "turn_completed", "timestamp": utc_now().isoformat()},
                    )
                    return
                message = f"Claude harness exited rc={rc}"
                tail = log_path.read_text(errors="replace")[-1000:] if log_path.exists() else ""
                if tail:
                    message = f"{message}: {tail.strip()}"
                await _tmux_kill_session(session_name)
                raise AgentError("port_exit", message)

    async def _emit_claude_line(self, line: bytes, on_event: AgentEventCallback) -> None:
        if len(line) > _MAX_PROTOCOL_LINE_BYTES:
            raise AgentError("response_error", "Claude protocol line exceeded limit")
        text = line.decode(errors="replace").rstrip()
        if not text:
            return
        payload = _parse_claude_line(text)
        await on_event(str(payload.get("event") or "message"), payload)


ClaudePrintClient = ClaudeTmuxClient


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
        self._client = client or _build_client(config, tracker)

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
            session = await self._client.start_session(
                workspace=workspace.path, issue=issue, on_event=on_event
            )
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
                    await session.run_turn(
                        prompt=prompt, turn_number=turn_number, on_event=on_event
                    )
                    refreshed = await self._tracker.fetch_issue_states_by_ids([issue.id])
                    if refreshed:
                        current_issue = refreshed[0]
                    if current_issue.state.lower() not in self._config.active_states_normalized:
                        break
                    if not current_issue.assigned_to_worker:
                        break
            finally:
                await session.stop()
            return workspace.path
        finally:
            await self._workspace_manager.after_run(workspace.path)


def _build_client(config: ServiceConfig, tracker: IssueTracker) -> AgentClient:
    if config.agent.harness == "claude":
        return ClaudeTmuxClient(config)
    tools: list[Tool] = []
    if isinstance(tracker, LinearTracker):
        from symphony.tools import LinearGraphqlTool

        tools.append(LinearGraphqlTool(tracker))
    return CodexAppServerClient(config, tools)


def _resolve_turn_sandbox_policy(
    configured: str | None, workspace: Path
) -> JsonObject | None:
    if configured is None:
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(workspace)],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
    try:
        parsed = json.loads(configured)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_event_payload(event: str, payload: JsonObject) -> JsonObject:
    if not isinstance(payload, dict):
        return {"event": event, "timestamp": utc_now().isoformat()}
    normalized: JsonObject = {str(k): _coerce_value(v) for k, v in payload.items()}
    normalized.setdefault("event", event)
    normalized.setdefault("timestamp", utc_now().isoformat())
    if "message" not in normalized:
        summary = _summary(payload)
        if summary:
            normalized["message"] = summary
    return normalized


def _coerce_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_coerce_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _coerce_value(item) for key, item in value.items()}
    return str(value)


def _summary(payload: JsonObject) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "text", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            return value[:500]
    params = payload.get("params")
    if isinstance(params, dict):
        for key in ("message", "text", "summary"):
            value = params.get(key)
            if isinstance(value, str):
                return value[:500]
    return ""


def _tmux_session_name(issue_identifier: str, turn_number: int) -> str:
    safe_issue = "".join(
        char if char.isalnum() or char in {"_", "-"} else "-" for char in issue_identifier
    )
    return f"symphony-{safe_issue}-{turn_number}-{uuid.uuid4().hex[:8]}"


async def _tmux_kill_session(session_name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _drain_stderr(stderr: asyncio.StreamReader) -> None:
    try:
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                LOG.debug("codex stderr: %s", text)
    except asyncio.CancelledError:
        return


def _parse_claude_line(text: str) -> JsonObject:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"event": "message", "timestamp": utc_now().isoformat(), "text": text}
    if not isinstance(payload, dict):
        return {"event": "message", "timestamp": utc_now().isoformat(), "text": text}
    result: JsonObject = {str(k): _coerce_value(v) for k, v in payload.items()}
    result.setdefault("event", str(payload.get("type") or "message"))
    result.setdefault("timestamp", utc_now().isoformat())
    return result
