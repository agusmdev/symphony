from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from symphony.config import ServiceConfig
from symphony.errors import AgentError
from symphony.models import Issue, JsonObject, StateHandler, WorkflowDefinition, utc_now
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
        session_id = f"{self._thread_id}-{turn_id}" if isinstance(turn_id, str) else self._thread_id
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
            self._turn_done.set_exception(AgentError("turn_input_required", _summary(payload)))

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


TMUX_SESSION_PREFIX = "symphony-"


class ClaudeTmuxClient:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    async def startup_cleanup(self) -> None:
        await kill_orphan_tmux_sessions()

    async def start_session(
        self, *, workspace: Path, issue: Issue, on_event: AgentEventCallback
    ) -> _ClaudeTmuxSession:
        return _ClaudeTmuxSession(self._config, workspace, issue)


class _ClaudeTmuxSession:
    def __init__(self, config: ServiceConfig, workspace: Path, issue: Issue) -> None:
        self._config = config
        self._workspace = workspace
        self._issue = issue
        self._active_sessions: set[str] = set()

    async def run_turn(
        self, *, prompt: str, turn_number: int, on_event: AgentEventCallback
    ) -> None:
        ensure_inside_root(self._config.workspace.root, self._workspace)
        run_dir = Path(tempfile.mkdtemp(prefix="symphony-claude-", dir=self._workspace))
        log_path = run_dir / "claude.log"
        done_path = run_dir / "done"
        prompt_path = run_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        mcp_config_path = self._write_mcp_config(run_dir)
        session_name = _tmux_session_name(self._issue.identifier, turn_number)
        buffer_name = f"symphony-{session_name}"
        command = self._tmux_command(done_path, mcp_config_path)
        env = self._claude_env()
        log_path.touch()
        LOG.info(
            "tmux_session creating issue=%s turn=%d session=%s log=%s run_dir=%s",
            self._issue.identifier,
            turn_number,
            session_name,
            log_path,
            run_dir,
        )
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "200",
            "-y",
            "50",
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
            LOG.error(
                "tmux_session start_failed issue=%s session=%s stderr=%s",
                self._issue.identifier,
                session_name,
                message,
            )
            raise AgentError("port_exit", f"failed to start Claude tmux session: {message}")
        self._active_sessions.add(session_name)
        await _tmux_run("set-option", "-t", session_name, "remain-on-exit", "off", check=False)
        pipe_cmd = f"cat >> {shlex.quote(str(log_path))}"
        await _tmux_run("pipe-pane", "-o", "-t", session_name, pipe_cmd)
        attach_command = f"tmux attach-session -t {shlex.quote(session_name)}"
        LOG.info(
            "tmux_session started issue=%s turn=%d session=%s attach=%r",
            self._issue.identifier,
            turn_number,
            session_name,
            attach_command,
        )
        await on_event(
            "session_started",
            {
                "event": "session_started",
                "timestamp": utc_now().isoformat(),
                "tmux_session": session_name,
                "tmux_attach_command": attach_command,
                "claude_log_path": str(log_path),
                "thread_id": f"{self._issue.id}",
                "turn_id": str(turn_number),
                "session_id": f"{self._issue.id}-{turn_number}",
            },
        )
        try:
            await asyncio.wait_for(
                self._drive_turn(
                    prompt_path=prompt_path,
                    buffer_name=buffer_name,
                    session_name=session_name,
                    log_path=log_path,
                    done_path=done_path,
                    on_event=on_event,
                ),
                self._config.claude.turn_timeout_ms / 1000,
            )
        except TimeoutError as exc:
            LOG.warning(
                "tmux_session timed_out issue=%s session=%s timeout_ms=%d",
                self._issue.identifier,
                session_name,
                self._config.claude.turn_timeout_ms,
            )
            raise AgentError("turn_timeout", "Claude turn timed out") from exc
        finally:
            await self._cleanup_session(session_name)

    async def stop(self) -> None:
        for session_name in list(self._active_sessions):
            await self._cleanup_session(session_name)

    async def _cleanup_session(self, session_name: str) -> None:
        if session_name not in self._active_sessions:
            return
        self._active_sessions.discard(session_name)
        alive = await _tmux_has_session(session_name)
        if alive:
            await _tmux_kill_session(session_name)
            LOG.info(
                "tmux_session killed issue=%s session=%s",
                self._issue.identifier,
                session_name,
            )
        else:
            LOG.info(
                "tmux_session ended issue=%s session=%s",
                self._issue.identifier,
                session_name,
            )

    def _claude_env(self) -> dict[str, str] | None:
        import os

        env = dict(os.environ)
        if self._config.tracker.api_key:
            env["LINEAR_API_KEY"] = self._config.tracker.api_key
        return env

    def _tmux_command(self, done_path: Path, mcp_config_path: Path | None) -> str:
        claude_command = self._config.claude.command
        if mcp_config_path is not None:
            claude_command = f"{claude_command} --mcp-config {shlex.quote(str(mcp_config_path))}"
        quoted_done = shlex.quote(str(done_path))
        return (
            f"{claude_command}; "
            "rc=$?; "
            f"printf '%s' \"$rc\" > {quoted_done}; "
            'printf "\\n[symphony] Claude exited rc=%s\\n" "$rc"; '
            'exit "$rc"'
        )

    async def _drive_turn(
        self,
        *,
        prompt_path: Path,
        buffer_name: str,
        session_name: str,
        log_path: Path,
        done_path: Path,
        on_event: AgentEventCallback,
    ) -> None:
        await self._wait_repl_ready(session_name, done_path)
        if done_path.exists():
            await self._finalize_done(log_path, done_path, session_name, on_event)
            return
        await _tmux_run("load-buffer", "-b", buffer_name, str(prompt_path))
        try:
            await _tmux_run("paste-buffer", "-p", "-b", buffer_name, "-t", session_name)
        finally:
            await _tmux_run("delete-buffer", "-b", buffer_name, check=False)
        await asyncio.sleep(0.3)
        await _tmux_run("send-keys", "-t", session_name, "Enter")
        await self._stream_until_idle(
            log_path=log_path,
            done_path=done_path,
            session_name=session_name,
            on_event=on_event,
        )

    async def _wait_repl_ready(self, session_name: str, done_path: Path) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60.0
        last_snapshot: str | None = None
        stable_since: float | None = None
        dismissed_trust = False
        while loop.time() < deadline:
            await asyncio.sleep(0.4)
            if done_path.exists():
                return
            snapshot_text = _strip_ansi(await _tmux_capture(session_name))
            lowered = snapshot_text.lower()
            if not dismissed_trust and "trust this folder" in lowered:
                await _tmux_run("send-keys", "-t", session_name, "Enter")
                dismissed_trust = True
                last_snapshot = None
                stable_since = None
                await asyncio.sleep(1.0)
                continue
            if last_snapshot is not None and snapshot_text == last_snapshot:
                if stable_since is None:
                    stable_since = loop.time()
                elif loop.time() - stable_since >= 1.2:
                    return
            else:
                stable_since = None
                last_snapshot = snapshot_text
        raise AgentError("port_exit", "Claude REPL never became ready inside tmux")

    async def _stream_until_idle(
        self,
        *,
        log_path: Path,
        done_path: Path,
        session_name: str,
        on_event: AgentEventCallback,
    ) -> None:
        loop = asyncio.get_running_loop()
        idle_threshold = self._config.claude.stall_timeout_ms / 1000
        poll_interval = self._config.claude.read_timeout_ms / 1000
        offset = 0
        last_log_size = 0
        last_activity = loop.time()
        sent_exit = False
        exit_sent_at: float | None = None
        while True:
            await asyncio.sleep(poll_interval)
            if log_path.exists():
                size = log_path.stat().st_size
                if size > offset:
                    data = log_path.read_bytes()[offset:size]
                    offset = size
                    await self._emit_log_chunk(data, on_event)
                    last_activity = loop.time()
                    last_log_size = size
                else:
                    if loop.time() - last_activity >= 10.0:
                        await on_event(
                            "heartbeat",
                            {
                                "event": "heartbeat",
                                "timestamp": utc_now().isoformat(),
                                "log_size": size,
                                "idle_seconds": round(loop.time() - last_activity, 1),
                            },
                        )
            if done_path.exists():
                await self._finalize_done(log_path, done_path, session_name, on_event)
                return
            idle_for = loop.time() - last_activity
            if not sent_exit and idle_for >= idle_threshold and last_log_size > 0:
                await _tmux_run("send-keys", "-t", session_name, "/exit", "Enter")
                sent_exit = True
                exit_sent_at = loop.time()
                await on_event(
                    "turn_idle",
                    {
                        "event": "turn_idle",
                        "timestamp": utc_now().isoformat(),
                        "idle_seconds": round(idle_for, 1),
                    },
                )
            if sent_exit and exit_sent_at is not None and loop.time() - exit_sent_at >= 20.0:
                if done_path.exists():
                    await self._finalize_done(log_path, done_path, session_name, on_event)
                    return
                LOG.info(
                    "tmux_session idle_exit_forced issue=%s session=%s",
                    self._issue.identifier,
                    session_name,
                )
                await on_event(
                    "turn_completed",
                    {"event": "turn_completed", "timestamp": utc_now().isoformat()},
                )
                return

    async def _finalize_done(
        self,
        log_path: Path,
        done_path: Path,
        session_name: str,
        on_event: AgentEventCallback,
    ) -> None:
        await asyncio.sleep(0.2)
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
            message = f"{message}: {_strip_ansi(tail).strip()}"
        LOG.warning(
            "tmux_session claude_failed issue=%s session=%s rc=%d",
            self._issue.identifier,
            session_name,
            rc,
        )
        raise AgentError("port_exit", message)

    async def _emit_log_chunk(self, data: bytes, on_event: AgentEventCallback) -> None:
        if len(data) > _MAX_PROTOCOL_LINE_BYTES:
            data = data[-_MAX_PROTOCOL_LINE_BYTES:]
        text = _strip_ansi(data.decode(errors="replace")).strip()
        if not text:
            return
        await on_event(
            "message",
            {
                "event": "message",
                "timestamp": utc_now().isoformat(),
                "text": text[-4000:],
            },
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


ClaudePrintClient = ClaudeTmuxClient


WorkflowGetter = WorkflowDefinition | Callable[[], WorkflowDefinition]


def _coerce_workflow_getter(
    workflow: WorkflowGetter,
) -> Callable[[], WorkflowDefinition]:
    if isinstance(workflow, WorkflowDefinition):
        snapshot = workflow
        return lambda: snapshot
    if callable(workflow):
        return workflow
    raise TypeError(
        f"workflow must be WorkflowDefinition or callable; got {type(workflow).__name__}"
    )


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
        self._clients: dict[str, AgentClient] = {}
        if client is not None:
            self._clients[config.agent.harness] = client

    def _client_for(self, harness: str) -> AgentClient:
        existing = self._clients.get(harness)
        if existing is not None:
            return existing
        built = _build_client_for_harness(harness, self._config, self._tracker)
        self._clients[harness] = built
        return built

    async def startup_cleanup(self, workflow: WorkflowDefinition | None = None) -> None:
        harnesses = {self._config.agent.harness}
        if workflow is not None:
            for handler in workflow.state_handlers.values():
                if handler.harness is not None:
                    harnesses.add(handler.harness)
        # Eagerly build every referenced harness so its cleanup runs at startup,
        # not only the default — otherwise orphan tmux/app-server processes from a
        # prior run leak when a harness is used only by a state-level handler.
        for harness in harnesses:
            self._client_for(harness)
        for client in self._clients.values():
            cleanup = getattr(client, "startup_cleanup", None)
            if cleanup is None:
                continue
            try:
                await cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup must not abort startup
                LOG.warning("startup_cleanup failed: %s", exc)

    def _handler_for(
        self, workflow: WorkflowDefinition, state: str
    ) -> StateHandler:
        handler = workflow.state_handlers.get(state.lower())
        if handler is None:
            return StateHandler(
                harness=self._config.agent.harness,
                prompt_template=workflow.prompt_template,
            )
        if handler.harness is None:
            return StateHandler(
                harness=self._config.agent.harness,
                prompt_template=handler.prompt_template,
            )
        return handler

    async def run(
        self,
        issue: Issue,
        attempt: int | None,
        workflow: WorkflowGetter,
        on_event: AgentEventCallback,
    ) -> Path:
        workspace = await self._workspace_manager.create_for_issue(issue.identifier)
        await self._workspace_manager.before_run(workspace.path)
        try:
            get_workflow = _coerce_workflow_getter(workflow)
            handler = self._handler_for(get_workflow(), issue.state)
            client = self._client_for(handler.harness or self._config.agent.harness)
            session = await client.start_session(
                workspace=workspace.path, issue=issue, on_event=on_event
            )
            try:
                current_issue = issue
                initial_state = issue.state.lower()
                initial_handler = handler
                for turn_number in range(1, self._config.agent.max_turns + 1):
                    prompt = (
                        render_prompt(handler.prompt_template, current_issue, attempt)
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
                    if current_issue.state.lower() != initial_state:
                        # State changed mid-run; let re-dispatch pick the right handler.
                        break
                    # Re-resolve from the live workflow so hot reload takes effect: any
                    # change to harness OR prompt template breaks the loop and forces a
                    # fresh dispatch that re-renders turn 1 against the new handler.
                    latest = self._handler_for(get_workflow(), current_issue.state)
                    if latest != initial_handler:
                        break
            finally:
                await session.stop()
            return workspace.path
        finally:
            await self._workspace_manager.after_run(workspace.path)


def _build_client_for_harness(
    harness: str, config: ServiceConfig, tracker: IssueTracker
) -> AgentClient:
    if harness == "claude":
        return ClaudeTmuxClient(config)
    if harness == "codex":
        tools: list[Tool] = []
        if isinstance(tracker, LinearTracker):
            from symphony.tools import LinearGraphqlTool

            tools.append(LinearGraphqlTool(tracker))
        return CodexAppServerClient(config, tools)
    raise AgentError("unsupported_agent_harness", f"unsupported harness: {harness}")


def _resolve_turn_sandbox_policy(configured: str | None, workspace: Path) -> JsonObject | None:
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


_PID_PREFIX_RE = re.compile(r"^symphony-pid(\d+)-")


def _tmux_session_name(issue_identifier: str, turn_number: int) -> str:
    safe_issue = "".join(
        char if char.isalnum() or char in {"_", "-"} else "-" for char in issue_identifier
    )
    return f"symphony-pid{os.getpid()}-{safe_issue}-{turn_number}-{uuid.uuid4().hex[:8]}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


async def _tmux_has_session(session_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "has-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    return rc == 0


async def kill_orphan_tmux_sessions(prefix: str = TMUX_SESSION_PREFIX) -> list[str]:
    """Kill `symphony-pid<PID>-*` tmux sessions whose owning PID is gone.

    Sessions without the `pid<N>` marker (older format or unrelated) are left
    alone, as are sessions whose PID is still alive — that includes another
    live orchestrator instance.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    killed: list[str] = []
    for raw in stdout.decode(errors="replace").splitlines():
        name = raw.strip()
        if not name or not name.startswith(prefix):
            continue
        match = _PID_PREFIX_RE.match(name)
        if match is None:
            continue
        owner_pid = int(match.group(1))
        if _pid_alive(owner_pid):
            continue
        await _tmux_kill_session(name)
        LOG.info(
            "tmux_session orphan_killed session=%s owner_pid=%d",
            name,
            owner_pid,
        )
        killed.append(name)
    return killed


async def _tmux_run(*args: str, check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode or 0
    if check and rc != 0:
        raise AgentError(
            "port_exit",
            f"tmux {args[0]} failed rc={rc}: {stderr.decode(errors='replace').strip()}",
        )
    return rc, stdout.decode(errors="replace")


async def _tmux_capture(session_name: str) -> str:
    rc, output = await _tmux_run("capture-pane", "-p", "-t", session_name, check=False)
    if rc != 0:
        return ""
    return output


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text).replace("\r", "")


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
