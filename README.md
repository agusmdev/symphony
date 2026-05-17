# Symphony

A typed Python implementation of the OpenAI Symphony service specification.

## Operational posture

This implementation is intended for trusted operator environments. Workspace isolation and path
validation are enforced, hooks are trusted shell scripts from `WORKFLOW.md`, and the default Codex
command is `codex app-server`. Approval and sandbox fields are read from workflow config and passed
to the generic JSON-lines app-server client when the configured server supports them.

Claude Code can be used instead by selecting the Claude harness. The default Claude command is
`claude`; Symphony starts it in a detached tmux session, sends the rendered prompt on stdin, and
streams the tmux log back into the run state. The `session_started` event includes a
`tmux_attach_command` for operators who want to inspect the live session.

## Usage

```sh
uv run symphony ./WORKFLOW.md
```

If no path is supplied, `./WORKFLOW.md` is used.

A complete reference `WORKFLOW.md` that implements the full multi-state Linear
flow (Todo → In Progress → Human Review → Merging → Rework → Done) is in
[`examples/WORKFLOW.md`](examples/WORKFLOW.md).

To have an agent create a repository-specific workflow, ask it:

```text
Create a WORKFLOW.md for this repository following this guide:
https://github.com/agusmdev/symphony/blob/main/docs/workflow-setup-guide.md
```

## Linear access from the agent

The agent talks to Linear through a `linear_graphql` tool that takes a GraphQL
query/mutation and optional variables. Symphony wires the same tool name and
schema into both harnesses so prompts are portable:

- **Codex** — Symphony registers `linear_graphql` as a Codex app-server dynamic
  tool via `dynamicTools`. Auto-approves command and file-change requests when
  `codex.approval_policy: "never"` is set.
- **Claude** — Symphony writes a per-run MCP config and launches Claude with
  `--mcp-config`. The bundled `symphony.linear_mcp` stdio server proxies the
  tool to `api.linear.app` using `LINEAR_API_KEY`.

Minimal Claude harness config:

```yaml
agent:
  harness: claude
claude:
  command: claude
```

## Assignee filter (multi-instance safety)

To run multiple Symphony instances against the same Linear project, set
`tracker.assignee` so each instance only picks up issues assigned to its
configured user. Use a Linear user id, or the literal `me` (resolved at startup
via the viewer query). `$LINEAR_ASSIGNEE` is also honored.
