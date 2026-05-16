from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from jinja2 import Environment, StrictUndefined

from symphony.errors import SymphonyError
from symphony.models import Issue


def render_prompt(template: str, issue: Issue, attempt: int | None) -> str:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        rendered = env.from_string(template).render(issue=_to_plain(issue), attempt=attempt)
    except Exception as exc:
        raise SymphonyError("prompt_render_failed", str(exc)) from exc
    return rendered.strip()


def continuation_prompt(issue: Issue, turn_number: int, max_turns: int) -> str:
    return (
        f"Continue work on {issue.identifier}: {issue.title}.\n"
        f"This is continuation turn {turn_number} of at most {max_turns}. "
        "Check current repository state and proceed according to the workflow."
    )


def _to_plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value
