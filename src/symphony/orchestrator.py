from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from symphony.config import ServiceConfig
from symphony.errors import SymphonyError
from symphony.models import Issue, JsonObject, OrchestratorState, RetryEntry, RunningEntry, utc_now
from symphony.tracker import IssueTracker
from symphony.workspace import WorkspaceManager

LOG = logging.getLogger("symphony")
CONTINUATION_DELAY_MS = 1_000


class Runner(Protocol):
    async def run(
        self,
        issue: Issue,
        attempt: int | None,
        prompt_template: str,
        on_event: Callable[[str, JsonObject], Awaitable[None]],
    ) -> object: ...


class Orchestrator:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: IssueTracker,
        workspace_manager: WorkspaceManager,
        runner: Runner,
        prompt_template: str,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.workspace_manager = workspace_manager
        self.runner: Runner = runner
        self.prompt_template = prompt_template
        self.state = OrchestratorState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.config.validate_for_dispatch()
        await self.startup_terminal_workspace_cleanup()
        await self._runner_startup_cleanup()
        while not self._stop.is_set():
            await self.tick()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.state.poll_interval_ms / 1000)

    def stop(self) -> None:
        self._stop.set()
        for entry in self.state.running.values():
            worker = entry.worker
            if isinstance(worker, asyncio.Task):
                worker.cancel()

    async def tick(self) -> None:
        async with self._lock:
            await self.reconcile_running_issues()
            try:
                self.config.validate_for_dispatch()
            except SymphonyError as exc:
                LOG.error("validation failed code=%s message=%s", exc.code, exc.message)
                return
            try:
                issues = await self.tracker.fetch_candidate_issues()
            except SymphonyError as exc:
                LOG.error("candidate_fetch failed code=%s message=%s", exc.code, exc.message)
                return
            for issue in sort_for_dispatch(issues):
                if self.available_slots() <= 0:
                    break
                if not self.should_dispatch(issue):
                    continue
                revalidated = await self._revalidate_for_dispatch(issue)
                if revalidated is None:
                    continue
                self.dispatch_issue(revalidated, None)

    async def _revalidate_for_dispatch(self, issue: Issue) -> Issue | None:
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
        except SymphonyError as exc:
            LOG.warning(
                "revalidate failed issue_id=%s code=%s message=%s",
                issue.id,
                exc.code,
                exc.message,
            )
            return None
        if not refreshed:
            LOG.info("skip dispatch: issue not visible issue_id=%s", issue.id)
            return None
        current = refreshed[0]
        if not self.should_dispatch(current):
            LOG.info(
                "skip stale dispatch issue_id=%s state=%s assigned=%s",
                current.id,
                current.state,
                current.assigned_to_worker,
            )
            return None
        return current

    async def _runner_startup_cleanup(self) -> None:
        cleanup = getattr(self.runner, "startup_cleanup", None)
        if cleanup is None:
            return
        try:
            await cleanup()
        except Exception as exc:  # noqa: BLE001 - cleanup must not abort startup
            LOG.warning("runner_startup_cleanup failed: %s", exc)

    async def startup_terminal_workspace_cleanup(self) -> None:
        try:
            issues = await self.tracker.fetch_issues_by_states(
                list(self.config.tracker.terminal_states)
            )
        except SymphonyError as exc:
            LOG.warning("startup_cleanup failed code=%s message=%s", exc.code, exc.message)
            return
        for issue in issues:
            await self.workspace_manager.remove_for_issue(issue.identifier)

    async def reconcile_running_issues(self) -> None:
        now = utc_now()
        for issue_id, entry in list(self.state.running.items()):
            last = entry.live_session.last_codex_timestamp or entry.started_at
            elapsed_ms = (now - last).total_seconds() * 1000
            if (
                self.config.harness_stall_timeout_ms > 0
                and elapsed_ms > self.config.harness_stall_timeout_ms
            ):
                await self._terminate(issue_id, cleanup_workspace=False, reason="stalled")
        ids = list(self.state.running)
        if not ids:
            return
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids(ids)
        except SymphonyError as exc:
            LOG.debug("state_refresh failed code=%s message=%s", exc.code, exc.message)
            return
        seen_ids: set[str] = set()
        for issue in refreshed:
            seen_ids.add(issue.id)
            running_entry = self.state.running.get(issue.id)
            if running_entry is None:
                continue
            state = issue.state.lower()
            if state in self.config.terminal_states_normalized:
                await self._terminate(issue.id, cleanup_workspace=True, reason="terminal")
            elif not issue.assigned_to_worker:
                await self._terminate(issue.id, cleanup_workspace=False, reason="reassigned")
            elif state in self.config.active_states_normalized:
                running_entry.issue = issue
            else:
                await self._terminate(issue.id, cleanup_workspace=False, reason="non_active")
        for issue_id in ids:
            if issue_id in seen_ids:
                continue
            if issue_id not in self.state.running:
                continue
            await self._terminate(issue_id, cleanup_workspace=False, reason="vanished")

    def available_slots(self) -> int:
        return max(self.state.max_concurrent_agents - len(self.state.running), 0)

    def should_dispatch(self, issue: Issue) -> bool:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False
        if not issue.assigned_to_worker:
            return False
        state = issue.state.lower()
        if state not in self.config.active_states_normalized:
            return False
        if state in self.config.terminal_states_normalized:
            return False
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return False
        if self.available_slots() <= 0:
            return False
        state_limit = self.config.agent.max_concurrent_agents_by_state.get(
            state, self.state.max_concurrent_agents
        )
        running_by_state = Counter(
            entry.issue.state.lower() for entry in self.state.running.values()
        )
        if running_by_state[state] >= state_limit:
            return False
        if state == "todo":
            for blocker in issue.blocked_by:
                if (
                    blocker.state is None
                    or blocker.state.lower() not in self.config.terminal_states_normalized
                ):
                    return False
        return True

    def dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        task = asyncio.create_task(self._run_worker(issue, attempt))
        self.state.running[issue.id] = RunningEntry(
            issue=issue,
            worker=task,
            workspace_path=None,
            retry_attempt=attempt,
            started_at=utc_now(),
        )
        self.state.claimed.add(issue.id)
        retry = self.state.retry_attempts.pop(issue.id, None)
        if retry and isinstance(retry.task, asyncio.Task):
            retry.task.cancel()
        LOG.info("dispatch started issue_id=%s issue_identifier=%s", issue.id, issue.identifier)

    async def _run_worker(self, issue: Issue, attempt: int | None) -> None:
        reason = "normal"
        try:
            path = await self.runner.run(
                issue,
                attempt,
                self.prompt_template,
                lambda event, payload: self.on_agent_event(issue.id, event, payload),
            )
            entry = self.state.running.get(issue.id)
            if entry and isinstance(path, Path):
                entry.workspace_path = path
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as exc:
            reason = f"worker_error:{exc}"
        finally:
            await self.on_worker_exit(issue.id, reason)

    async def on_agent_event(self, issue_id: str, event: str, payload: JsonObject) -> None:
        async with self._lock:
            entry = self.state.running.get(issue_id)
            if entry is None:
                return
            live = entry.live_session
            live.last_codex_event = event
            live.last_codex_timestamp = utc_now()
            live.codex_app_server_pid = _str_or_none(
                payload.get("codex_app_server_pid") or payload.get("agent_process_pid")
            )
            live.thread_id = _str_or_none(payload.get("thread_id")) or live.thread_id
            live.turn_id = _str_or_none(payload.get("turn_id")) or live.turn_id
            live.session_id = _str_or_none(payload.get("session_id")) or (
                f"{live.thread_id}-{live.turn_id}"
                if live.thread_id and live.turn_id
                else live.session_id
            )
            live.last_codex_message = _summarize(payload)
            if event == "session_started":
                live.turn_count += 1
            self._apply_usage(entry, payload)

    async def on_worker_exit(self, issue_id: str, reason: str) -> None:
        async with self._lock:
            entry = self.state.running.pop(issue_id, None)
            if entry is None:
                return
            self.state.codex_totals.seconds_running += (
                utc_now() - entry.started_at
            ).total_seconds()
            if reason == "normal":
                self.state.completed.add(issue_id)
                self.schedule_retry(issue_id, entry.issue.identifier, 1, None, continuation=True)
            else:
                attempt = (entry.retry_attempt or 0) + 1
                self.schedule_retry(
                    issue_id, entry.issue.identifier, attempt, reason, continuation=False
                )
            LOG.info(
                "worker exited issue_id=%s issue_identifier=%s reason=%s",
                issue_id,
                entry.issue.identifier,
                reason,
            )

    def schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        error: str | None,
        *,
        continuation: bool,
    ) -> None:
        old = self.state.retry_attempts.get(issue_id)
        if old and isinstance(old.task, asyncio.Task):
            old.task.cancel()
        delay_ms = (
            CONTINUATION_DELAY_MS
            if continuation
            else min(10_000 * (2 ** max(attempt - 1, 0)), self.config.agent.max_retry_backoff_ms)
        )
        due_at_ms = time.monotonic() * 1000 + delay_ms
        task = asyncio.create_task(self._retry_later(issue_id, delay_ms))
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id, identifier, attempt, due_at_ms, error, task
        )

    async def _retry_later(self, issue_id: str, delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)
        async with self._lock:
            retry = self.state.retry_attempts.pop(issue_id, None)
            if retry is None:
                return
            try:
                candidates = await self.tracker.fetch_candidate_issues()
            except SymphonyError:
                self.schedule_retry(
                    issue_id,
                    retry.identifier,
                    retry.attempt + 1,
                    "retry poll failed",
                    continuation=False,
                )
                return
            issue = next((item for item in candidates if item.id == issue_id), None)
            if issue is None:
                self.state.claimed.discard(issue_id)
                return
            if self.available_slots() <= 0 or not self.should_dispatch_with_claim(issue):
                self.schedule_retry(
                    issue_id,
                    issue.identifier,
                    retry.attempt + 1,
                    "no available orchestrator slots",
                    continuation=False,
                )
                return
            self.state.claimed.discard(issue_id)
            self.dispatch_issue(issue, retry.attempt)

    def should_dispatch_with_claim(self, issue: Issue) -> bool:
        was_claimed = issue.id in self.state.claimed
        if was_claimed:
            self.state.claimed.remove(issue.id)
        try:
            return self.should_dispatch(issue)
        finally:
            if was_claimed:
                self.state.claimed.add(issue.id)

    async def _terminate(self, issue_id: str, *, cleanup_workspace: bool, reason: str) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        self.state.codex_totals.seconds_running += (utc_now() - entry.started_at).total_seconds()
        worker = entry.worker
        if isinstance(worker, asyncio.Task):
            worker.cancel()
        if reason == "stalled":
            attempt = (entry.retry_attempt or 0) + 1
            self.schedule_retry(
                issue_id,
                entry.issue.identifier,
                attempt,
                "stalled",
                continuation=False,
            )
        else:
            self.state.claimed.discard(issue_id)
        if cleanup_workspace:
            await self.workspace_manager.remove_for_issue(entry.issue.identifier)
        LOG.info(
            "run terminated issue_id=%s issue_identifier=%s reason=%s",
            issue_id,
            entry.issue.identifier,
            reason,
        )

    def snapshot(self) -> JsonObject:
        running: list[JsonObject] = []
        for issue_id, entry in self.state.running.items():
            live = entry.live_session
            running.append(
                {
                    "issue_id": issue_id,
                    "issue_identifier": entry.issue.identifier,
                    "state": entry.issue.state,
                    "session_id": live.session_id,
                    "turn_count": live.turn_count,
                    "last_event": live.last_codex_event,
                    "last_message": live.last_codex_message,
                    "started_at": entry.started_at.isoformat(),
                    "last_event_at": live.last_codex_timestamp.isoformat()
                    if live.last_codex_timestamp
                    else None,
                    "tokens": {
                        "input_tokens": live.codex_input_tokens,
                        "output_tokens": live.codex_output_tokens,
                        "total_tokens": live.codex_total_tokens,
                    },
                }
            )
        retrying: list[JsonObject] = [
            {
                "issue_id": entry.issue_id,
                "issue_identifier": entry.identifier,
                "attempt": entry.attempt,
                "due_at_ms": entry.due_at_ms,
                "error": entry.error,
            }
            for entry in self.state.retry_attempts.values()
        ]
        return cast(
            JsonObject,
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "counts": {"running": len(running), "retrying": len(retrying)},
                "running": running,
                "retrying": retrying,
                "codex_totals": {
                    "input_tokens": self.state.codex_totals.input_tokens,
                    "output_tokens": self.state.codex_totals.output_tokens,
                    "total_tokens": self.state.codex_totals.total_tokens,
                    "seconds_running": self.state.codex_totals.seconds_running,
                },
                "rate_limits": self.state.codex_rate_limits,
            },
        )

    def _apply_usage(self, entry: RunningEntry, payload: JsonObject) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = payload.get("total_token_usage")
        if not isinstance(usage, dict):
            return
        input_tokens = _int_from(cast(dict[object, object], usage), "input_tokens", "input")
        output_tokens = _int_from(cast(dict[object, object], usage), "output_tokens", "output")
        total_tokens = _int_from(cast(dict[object, object], usage), "total_tokens", "total")
        live = entry.live_session
        if input_tokens is not None:
            self.state.codex_totals.input_tokens += max(
                input_tokens - live.last_reported_input_tokens, 0
            )
            live.last_reported_input_tokens = input_tokens
            live.codex_input_tokens = input_tokens
        if output_tokens is not None:
            self.state.codex_totals.output_tokens += max(
                output_tokens - live.last_reported_output_tokens, 0
            )
            live.last_reported_output_tokens = output_tokens
            live.codex_output_tokens = output_tokens
        if total_tokens is not None:
            self.state.codex_totals.total_tokens += max(
                total_tokens - live.last_reported_total_tokens, 0
            )
            live.last_reported_total_tokens = total_tokens
            live.codex_total_tokens = total_tokens
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            self.state.codex_rate_limits = {str(k): v for k, v in rate_limits.items()}


def sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.priority if issue.priority is not None else 999,
            issue.created_at or datetime.max.replace(tzinfo=UTC),
            issue.identifier,
        ),
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _summarize(payload: JsonObject) -> str:
    for key in ("message", "text", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            return value[:500]
    return ""


def _int_from(payload: dict[object, object], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None
