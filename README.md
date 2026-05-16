# Symphony

A typed Python implementation of the OpenAI Symphony service specification.

## Operational posture

This implementation is intended for trusted operator environments. Workspace isolation and path
validation are enforced, hooks are trusted shell scripts from `WORKFLOW.md`, and the default Codex
command is `codex app-server`. Approval and sandbox fields are read from workflow config and passed
to the generic JSON-lines app-server client when the configured server supports them.

Claude Code can be used instead by selecting the Claude harness. The default Claude command is
`claude -p`; Symphony appends the rendered prompt as the next shell-quoted argument and streams
stdout back into the run state.

## Usage

```sh
uv run symphony ./WORKFLOW.md
```

If no path is supplied, `./WORKFLOW.md` is used.

To have an agent create a repository-specific workflow, ask it:

```text
Create a WORKFLOW.md for this repository following this guide:
https://github.com/agusmdev/symphony/blob/main/docs/workflow-setup-guide.md
```

Minimal Claude harness config:

```yaml
agent:
  harness: claude
claude:
  command: claude -p
```
