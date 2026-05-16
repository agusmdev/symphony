from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SymphonyError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ConfigError(SymphonyError):
    pass


class WorkflowError(SymphonyError):
    pass


class WorkspaceError(SymphonyError):
    pass


class TrackerError(SymphonyError):
    pass


class AgentError(SymphonyError):
    pass
