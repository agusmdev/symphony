from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import build_config
from symphony.errors import SymphonyError
from symphony.models import Issue, WorkflowDefinition
from symphony.prompt import render_prompt
from symphony.workflow import load_workflow, select_workflow_path


def test_load_workflow_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\n---\nHello {{ issue.identifier }}\n")
    loaded = load_workflow(path)
    assert loaded.config == {"tracker": {"kind": "linear"}}
    assert loaded.prompt_template == "Hello {{ issue.identifier }}"


def test_front_matter_must_be_map(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\n- nope\n---\nbody\n")
    with pytest.raises(SymphonyError, match="invalid_workflow_front_matter"):
        load_workflow(path)


def test_defaults_and_env_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_TOKEN", "secret")
    definition = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "$LINEAR_TOKEN", "project_slug": "proj"},
            "workspace": {"root": "~/symphony-test"},
            "agent": {"max_concurrent_agents_by_state": {"Todo": 2, "bad": 0}},
        },
        prompt_template="body",
    )
    config = build_config(definition, base_dir=tmp_path)
    assert config.tracker.api_key == "secret"
    assert config.tracker.endpoint == "https://api.linear.app/graphql"
    assert config.agent.max_concurrent_agents_by_state == {"todo": 2}
    config.validate_for_dispatch()


def test_prompt_strict_unknown_variable() -> None:
    issue = Issue("id", "ABC-1", "Title", None, None, "Todo", None, None)
    with pytest.raises(SymphonyError, match="prompt_render_failed"):
        render_prompt("{{ missing }}", issue, None)


def test_prompt_renders_issue_and_attempt() -> None:
    issue = Issue("id", "ABC-1", "Title", None, None, "Todo", None, None)
    assert render_prompt("{{ issue.identifier }} {{ attempt }}", issue, 3) == "ABC-1 3"


def test_select_default_workflow_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert select_workflow_path(None) == tmp_path / "WORKFLOW.md"
