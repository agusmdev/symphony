from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from symphony.agent import AgentRunner
from symphony.config import ServiceConfig, build_config
from symphony.models import WorkflowDefinition
from symphony.orchestrator import Orchestrator
from symphony.tracker import LinearTracker
from symphony.workflow import load_workflow
from symphony.workspace import WorkspaceManager

LOG = logging.getLogger("symphony")


class SymphonyApp:
    def __init__(self, workflow_path: Path) -> None:
        self.workflow_path = workflow_path
        self.definition = load_workflow(workflow_path)
        self.config = build_config(self.definition, base_dir=workflow_path.parent)
        self.orchestrator = self._build_orchestrator(self.config, self.definition)
        self._last_mtime_ns = workflow_path.stat().st_mtime_ns

    def _build_orchestrator(
        self, config: ServiceConfig, definition: WorkflowDefinition
    ) -> Orchestrator:
        tracker = LinearTracker(config)
        workspace = WorkspaceManager(config.workspace, config.hooks)
        runner = AgentRunner(config, workspace, tracker)
        return Orchestrator(config, tracker, workspace, runner, definition.prompt_template)

    async def run(self) -> None:
        watcher = asyncio.create_task(self._watch_workflow())
        try:
            await self.orchestrator.start()
        finally:
            watcher.cancel()

    async def _watch_workflow(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                mtime = self.workflow_path.stat().st_mtime_ns
            except OSError:
                LOG.error("workflow_reload failed reason=missing_workflow_file")
                continue
            if mtime == self._last_mtime_ns:
                continue
            self._last_mtime_ns = mtime
            try:
                definition = load_workflow(self.workflow_path)
                config = build_config(definition, base_dir=self.workflow_path.parent)
                config.validate_for_dispatch()
            except Exception as exc:
                LOG.error("workflow_reload failed reason=%s", exc)
                continue
            self.definition = definition
            self.config = config
            self.orchestrator.config = config
            self.orchestrator.prompt_template = definition.prompt_template
            self.orchestrator.state.poll_interval_ms = config.polling.interval_ms
            self.orchestrator.state.max_concurrent_agents = config.agent.max_concurrent_agents
            LOG.info("workflow_reload completed path=%s", self.workflow_path)
