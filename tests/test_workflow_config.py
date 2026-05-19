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
    config.validate_for_dispatch(definition)


def test_claude_harness_config() -> None:
    definition = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "x", "project_slug": "proj"},
            "agent": {"harness": "claude"},
            "claude": {"command": "claude", "stall_timeout_ms": 123},
        },
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.agent.harness == "claude"
    assert config.claude.command == "claude"
    assert config.stall_timeout_ms_for("claude") == 123
    config.validate_for_dispatch(definition)


def test_unknown_harness_rejected() -> None:
    definition = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "x", "project_slug": "proj"},
            "agent": {"harness": "other"},
        },
        prompt_template="body",
    )
    config = build_config(definition)
    with pytest.raises(SymphonyError, match="unsupported_agent_harness"):
        config.validate_for_dispatch(definition)


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


def test_assignee_explicit_value() -> None:
    definition = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "linear",
                "api_key": "k",
                "project_slug": "p",
                "assignee": "user-id-42",
            }
        },
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.tracker.assignee == "user-id-42"


def test_assignee_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_ASSIGNEE", "user-via-env")
    definition = WorkflowDefinition(
        config={"tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"}},
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.tracker.assignee == "user-via-env"


def test_assignee_envref_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_ASSIGNEE", "user-via-ref")
    definition = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "linear",
                "api_key": "k",
                "project_slug": "p",
                "assignee": "$MY_ASSIGNEE",
            }
        },
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.tracker.assignee == "user-via-ref"


def test_assignee_unset_when_no_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_ASSIGNEE", raising=False)
    definition = WorkflowDefinition(
        config={"tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"}},
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.tracker.assignee is None


def test_default_active_states_include_human_review_and_merging() -> None:
    definition = WorkflowDefinition(
        config={"tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"}},
        prompt_template="body",
    )
    config = build_config(definition)
    states = {s.lower() for s in config.tracker.active_states}
    assert "todo" in states
    assert "in progress" in states
    assert "human review" in states
    assert "merging" in states
    assert "rework" in states


def test_state_handlers_parsed_from_named_sections(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "states:\n"
        "  Todo: {harness: codex, prompt: implement}\n"
        "  \"In Review\": {harness: claude, prompt: review}\n"
        "---\n"
        "default body\n"
        "\n"
        "## prompt:implement\n"
        "Implement {{ issue.identifier }}.\n"
        "\n"
        "## prompt:review\n"
        "Review {{ issue.identifier }}.\n"
    )
    loaded = load_workflow(path)
    assert loaded.prompt_template == "default body"
    assert set(loaded.state_handlers.keys()) == {"todo", "in review"}
    assert loaded.state_handlers["todo"].harness == "codex"
    assert loaded.state_handlers["todo"].prompt_template == "Implement {{ issue.identifier }}."
    assert loaded.state_handlers["in review"].harness == "claude"
    assert loaded.state_handlers["in review"].prompt_template == "Review {{ issue.identifier }}."


def test_states_keys_union_into_active_states(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "  active_states: [Todo]\n"
        "states:\n"
        "  \"In Review\": {prompt: review}\n"
        "---\n"
        "## prompt:review\nbody\n"
    )
    loaded = load_workflow(path)
    config = build_config(loaded)
    assert "Todo" in config.tracker.active_states
    assert "In Review" in config.tracker.active_states


def test_unknown_prompt_section_rejected(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "states:\n  Todo: {prompt: missing}\n"
        "---\n"
        "## prompt:other\nbody\n"
    )
    with pytest.raises(SymphonyError, match="unknown_prompt_section"):
        load_workflow(path)


def test_invalid_state_handler_harness_rejected(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "states:\n  Todo: {harness: bogus, prompt: x}\n"
        "---\n"
        "## prompt:x\nbody\n"
    )
    with pytest.raises(SymphonyError, match="invalid_state_handler_harness"):
        load_workflow(path)


def test_back_compat_no_states_keeps_body_as_default(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "---\n"
        "Hello {{ issue.identifier }}\n"
    )
    loaded = load_workflow(path)
    assert loaded.state_handlers == {}
    assert loaded.prompt_template == "Hello {{ issue.identifier }}"


def test_validate_for_dispatch_checks_per_state_harness_commands(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "agent:\n  harness: codex\n"
        "claude:\n  command: ''\n"
        "states:\n  \"In Review\": {harness: claude, prompt: r}\n"
        "---\n"
        "## prompt:r\nbody\n"
    )
    loaded = load_workflow(path)
    config = build_config(loaded)
    with pytest.raises(SymphonyError, match="missing_claude_command"):
        config.validate_for_dispatch(loaded)


def test_prompt_header_inside_fence_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "states:\n  Todo: {prompt: real}\n"
        "---\n"
        "## prompt:real\n"
        "Render this.\n"
        "\n"
        "```md\n"
        "## prompt:fake\n"
        "this should be part of real, not its own section\n"
        "```\n"
        "After fence.\n"
    )
    loaded = load_workflow(path)
    assert set(loaded.state_handlers.keys()) == {"todo"}
    template = loaded.state_handlers["todo"].prompt_template
    assert "## prompt:fake" in template
    assert "After fence." in template


def test_duplicate_prompt_section_rejected(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "---\n"
        "## prompt:foo\nfirst\n"
        "## prompt:FOO\nsecond\n"
    )
    with pytest.raises(SymphonyError, match="duplicate_prompt_section"):
        load_workflow(path)


def test_duplicate_state_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "states:\n"
        "  Todo: {prompt: x}\n"
        "  todo: {prompt: x}\n"
        "---\n"
        "## prompt:x\nbody\n"
    )
    with pytest.raises(SymphonyError, match="duplicate_state_key"):
        load_workflow(path)


def test_states_terminal_overlap_rejected(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        "---\n"
        "tracker:\n  kind: linear\n  api_key: k\n  project_slug: p\n"
        "  terminal_states: [Done]\n"
        "states:\n  Done: {prompt: x}\n"
        "---\n"
        "## prompt:x\nbody\n"
    )
    loaded = load_workflow(path)
    with pytest.raises(SymphonyError, match="state_overlaps_terminal"):
        build_config(loaded)


def test_stall_timeout_per_harness() -> None:
    definition = WorkflowDefinition(
        config={
            "tracker": {"kind": "linear", "api_key": "k", "project_slug": "p"},
            "agent": {"harness": "codex"},
            "codex": {"stall_timeout_ms": 111},
            "claude": {"stall_timeout_ms": 222},
        },
        prompt_template="body",
    )
    config = build_config(definition)
    assert config.stall_timeout_ms_for("codex") == 111
    assert config.stall_timeout_ms_for("claude") == 222
