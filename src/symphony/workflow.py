from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from symphony.errors import WorkflowError
from symphony.models import StateHandler, WorkflowDefinition

SUPPORTED_HARNESSES = ("codex", "claude")
_PROMPT_HEADER_RE = re.compile(r"^##\s+prompt:(\S+)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


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

    default_template, sections = _split_prompt_sections(body)
    state_handlers = _build_state_handlers(config.get("states"), sections, default_template)

    return WorkflowDefinition(
        config=config,
        prompt_template=default_template,
        state_handlers=state_handlers,
    )


def _split_prompt_sections(body: str) -> tuple[str, dict[str, str]]:
    sections: dict[str, str] = {}
    default_lines: list[str] = []
    current_name: str | None = None
    current_lines: list[str] = []
    fence_marker: str | None = None
    raw_names_seen: dict[str, str] = {}
    for line in body.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0] * 3
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker and line.lstrip().startswith(fence_marker):
                fence_marker = None
        header_match = None if fence_marker is not None else _PROMPT_HEADER_RE.match(line)
        if header_match:
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            raw_name = header_match.group(1).strip()
            key = raw_name.lower()
            if key in raw_names_seen:
                raise WorkflowError(
                    "duplicate_prompt_section",
                    f"prompt section '{raw_name}' duplicates '{raw_names_seen[key]}'",
                )
            raw_names_seen[key] = raw_name
            current_name = key
            current_lines = []
        elif current_name is None:
            default_lines.append(line)
        else:
            current_lines.append(line)
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()
    return "\n".join(default_lines).strip(), sections


def _build_state_handlers(
    raw: Any,
    sections: dict[str, str],
    default_template: str,
) -> dict[str, StateHandler]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkflowError("invalid_state_map", "states: must be a map of state -> handler")
    handlers: dict[str, StateHandler] = {}
    seen_keys: dict[str, str] = {}
    for state_name, entry in raw.items():
        if not isinstance(state_name, str) or not state_name.strip():
            raise WorkflowError("invalid_state_name", f"invalid state key: {state_name!r}")
        key = state_name.lower()
        if key in seen_keys:
            raise WorkflowError(
                "duplicate_state_key",
                f"states.{state_name} duplicates states.{seen_keys[key]} (case-insensitive)",
            )
        seen_keys[key] = state_name
        if not isinstance(entry, dict):
            raise WorkflowError(
                "invalid_state_handler",
                f"states.{state_name} must be a map with optional harness and prompt",
            )
        harness = entry.get("harness")
        if harness is not None and (
            not isinstance(harness, str) or harness not in SUPPORTED_HARNESSES
        ):
            raise WorkflowError(
                "invalid_state_handler_harness",
                f"states.{state_name}.harness must be one of {SUPPORTED_HARNESSES}",
            )
        prompt_value = entry.get("prompt")
        if prompt_value is None:
            template = default_template
        elif isinstance(prompt_value, str) and prompt_value.strip():
            section_key = prompt_value.strip().lower()
            if section_key not in sections:
                raise WorkflowError(
                    "unknown_prompt_section",
                    f"states.{state_name}.prompt references unknown section "
                    f"'## prompt:{prompt_value}'",
                )
            template = sections[section_key]
        else:
            raise WorkflowError(
                "invalid_state_handler_prompt",
                f"states.{state_name}.prompt must be a section name string",
            )
        handlers[key] = StateHandler(harness=harness, prompt_template=template)
    return handlers
