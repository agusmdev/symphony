# Workflow Setup Guide

Use this guide when a human asks you to create a `WORKFLOW.md` for a repository that should be run by Symphony.

Suggested prompt:

```text
Create a WORKFLOW.md for this repository following this guide:
https://github.com/agusmdev/symphony/blob/main/docs/workflow-setup-guide.md
```

## Goal

Create a repository-specific `WORKFLOW.md` that Symphony can run directly.

The file must contain:

- YAML front matter with Symphony configuration.
- A prompt body that tells the coding agent how to work on each tracker issue in this repository.
- Repository-specific setup, verification, and delivery instructions.

Do not create a generic workflow. Inspect the repository first, then ask only the questions needed to fill gaps safely.

## Required Discovery

Before writing `WORKFLOW.md`, determine these items. Ask the human if you cannot infer them from the repository.

- Tracker project: the Linear project slug Symphony should dispatch from.
- Agent harness: `codex` or `claude`.
- Workspace root: where Symphony should create per-issue working directories.
- Active states: issue states Symphony may dispatch.
- Terminal states: issue states that mean work is complete or should be cleaned up.
- Verification command: the command agents must run before considering work complete.
- Branch and commit policy: whether agents should create branches, commit changes, push, open PRs, or stop after local edits.
- Completion behavior: what the agent should do to the Linear issue when done, if anything.

Required questions to ask the human when not already known:

```text
What Linear project slug should Symphony use?
Which harness should this repo use: codex or claude?
Where should Symphony create workspaces?
What command must pass before an agent marks work complete?
Should agents commit/push/open PRs, or leave changes local?
```

## Optional Discovery

Ask these only when they matter for the repository or cannot be inferred.

- Environment variables needed for setup, tests, builds, or tracker access.
- Package manager and install command.
- Dev server command and expected localhost URL.
- Database, migration, seed, or service startup requirements.
- Test subsets for faster issue loops.
- Formatting, linting, typechecking, and code generation commands.
- Required screenshots, browser checks, or manual QA steps.
- Protected files or directories agents should avoid.
- Review style, PR template, changelog, or release note requirements.
- State-specific concurrency limits.
- Hook commands for workspace creation, pre-run setup, post-run cleanup, or terminal cleanup.
- Harness-specific timeout or sandbox settings.

Optional questions can be grouped. Avoid making the human answer questions that repository files already answer.

## Repository Inspection Checklist

Inspect the repository before asking questions.

- Read `README.md`, package manifests, build files, test config, and existing agent instructions.
- Find install, lint, typecheck, test, build, and dev commands.
- Check whether the repo already has `.env.example`, CI config, PR templates, or contribution docs.
- Identify the default branch and whether branches should be created per issue.
- Check whether tests need services, secrets, browser automation, or generated assets.

## Output Format

Write `WORKFLOW.md` with YAML front matter first, followed by the agent prompt body.

Minimal shape:

```markdown
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: example-project
workspace:
  root: /tmp/symphony_workspaces/example-repo
agent:
  harness: codex
  max_concurrent_agents: 3
  max_turns: 20
polling:
  interval_ms: 30000
---

You are working on Linear issue {{ issue.identifier }}: {{ issue.title }}.

Repository workflow:
- Inspect the issue and the repository before editing.
- Make the smallest coherent change that fully addresses the issue.
- Run the required verification command before finishing.
- Report what changed, what was verified, and any remaining risk.
```

Use `agent.harness: claude` and a `claude` block when the repository should run Claude:

```yaml
agent:
  harness: claude
claude:
  command: claude -p
```

Use a `codex` block when the repository needs non-default Codex settings:

```yaml
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: danger-full-access
  turn_sandbox_policy: danger-full-access
```

## Prompt Body Requirements

The prompt body should be specific enough that an agent can act without another setup conversation.

Include:

- The issue context variables the agent should use: `{{ issue.identifier }}`, `{{ issue.title }}`, and when helpful `{{ issue.description }}`.
- The expected implementation workflow.
- The exact verification commands.
- Rules for commits, pushes, PRs, and issue updates.
- Instructions for handling blockers, missing secrets, flaky tests, or unclear requirements.
- Repository-specific quality expectations.

Avoid:

- Hardcoded secrets.
- Vague commands like "run tests" when the actual command is known.
- Instructions that conflict with the repository's existing agent or contributor docs.
- Asking the agent to mark work complete without verification.

## Front Matter Reference

Common sections:

- `tracker`: Linear configuration. `api_key` can reference an environment variable such as `$LINEAR_API_KEY`.
- `workspace`: per-issue workspace root.
- `hooks`: trusted shell commands run around workspace lifecycle events.
- `agent`: concurrency, turn count, retry, state limits, and harness selection.
- `codex`: Codex app-server command, timeout, sandbox, and approval settings.
- `claude`: Claude command and timeout settings.
- `polling`: dispatch polling interval.

Keep config conservative. Start with low concurrency until the repository workflow has been proven.

## Human Collaboration Rules

Ask required questions first. Then inspect the repo and draft the workflow.

If optional details are uncertain, either:

- Infer them from repository files and mention the assumption, or
- Ask a concise follow-up when a wrong assumption could cause failed runs, destructive behavior, or incorrect delivery.

Before finalizing, show the human:

- The proposed `WORKFLOW.md` path.
- The required environment variables.
- The commands Symphony agents will run.
- The delivery behavior agents will follow.

After approval, create or update `WORKFLOW.md`.
