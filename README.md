# Symphony

A typed Python implementation of the OpenAI Symphony service specification.

## Operational posture

This implementation is intended for trusted operator environments. Workspace isolation and path
validation are enforced, hooks are trusted shell scripts from `WORKFLOW.md`, and the default Codex
command is `codex app-server`. Approval and sandbox fields are read from workflow config and passed
to the generic JSON-lines app-server client when the configured server supports them.

## Usage

```sh
uv run symphony ./WORKFLOW.md
```

If no path is supplied, `./WORKFLOW.md` is used.
