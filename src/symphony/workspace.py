from __future__ import annotations

import asyncio
import re
import shutil
from contextlib import suppress
from pathlib import Path

from symphony.config import HooksConfig, WorkspaceConfig
from symphony.errors import WorkspaceError
from symphony.models import Workspace

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def workspace_key(identifier: str) -> str:
    return SAFE_NAME.sub("_", identifier)


def ensure_inside_root(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise WorkspaceError(
            "workspace_outside_root", f"{path_resolved} is outside {root_resolved}"
        )


class WorkspaceManager:
    def __init__(self, config: WorkspaceConfig, hooks: HooksConfig) -> None:
        self._config = config
        self._hooks = hooks

    @property
    def root(self) -> Path:
        return self._config.root

    async def create_for_issue(self, identifier: str) -> Workspace:
        key = workspace_key(identifier)
        path = (self.root / key).resolve()
        ensure_inside_root(self.root, path)
        created_now = not path.exists()
        if path.exists() and not path.is_dir():
            raise WorkspaceError(
                "workspace_path_not_directory", f"{path} exists and is not a directory"
            )
        path.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(path=path, workspace_key=key, created_now=created_now)
        if created_now and self._hooks.after_create:
            await run_hook("after_create", self._hooks.after_create, path, self._hooks.timeout_ms)
        return workspace

    async def before_run(self, path: Path) -> None:
        if self._hooks.before_run:
            await run_hook("before_run", self._hooks.before_run, path, self._hooks.timeout_ms)

    async def after_run(self, path: Path) -> None:
        if self._hooks.after_run:
            try:
                await run_hook("after_run", self._hooks.after_run, path, self._hooks.timeout_ms)
            except WorkspaceError:
                return

    async def remove_for_issue(self, identifier: str) -> None:
        path = (self.root / workspace_key(identifier)).resolve()
        ensure_inside_root(self.root, path)
        if self._hooks.before_remove and path.exists():
            with suppress(WorkspaceError):
                await run_hook(
                    "before_remove", self._hooks.before_remove, path, self._hooks.timeout_ms
                )
        if path.exists():
            shutil.rmtree(path)


async def run_hook(label: str, script: str, cwd: Path, timeout_ms: int) -> None:
    ensure_inside_root(cwd, cwd)
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-lc",
        script,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise WorkspaceError("hook_timeout", f"{label} timed out") from exc
    if proc.returncode != 0:
        out = stdout.decode(errors="replace")[-2000:]
        err = stderr.decode(errors="replace")[-2000:]
        raise WorkspaceError(
            "hook_failed", f"{label} failed rc={proc.returncode} stdout={out} stderr={err}"
        )
