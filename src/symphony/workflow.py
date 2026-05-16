from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from symphony.errors import WorkflowError
from symphony.models import WorkflowDefinition


def select_workflow_path(explicit_path: str | None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path).expanduser()
    return Path.cwd() / "WORKFLOW.md"


def load_workflow(path: Path) -> WorkflowDefinition:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("missing_workflow_file", f"cannot read workflow file: {path}") from exc

    config: dict[str, Any]
    body: str
    if raw.startswith("---"):
        lines = raw.splitlines(keepends=True)
        end_index: int | None = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_index = index
                break
        if end_index is None:
            raise WorkflowError("invalid_workflow_front_matter", "missing closing ---")
        yaml_text = "".join(lines[1:end_index])
        body = "".join(lines[end_index + 1 :])
        try:
            loaded = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
        except yaml.YAMLError as exc:
            raise WorkflowError("invalid_workflow_front_matter", str(exc)) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise WorkflowError("invalid_workflow_front_matter", "front matter must be a map")
        config = cast(dict[str, Any], loaded)
    else:
        config = {}
        body = raw

    return WorkflowDefinition(config=config, prompt_template=body.strip())
