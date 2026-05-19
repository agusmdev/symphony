---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: your-project-slug
  # Optional. Set to a Linear user id or `me` to only handle issues
  # assigned to that user. Resolves `$LINEAR_ASSIGNEE` too.
  # assignee: me
  active_states:
    - Todo
    - In Progress
    - Human Review
    - Merging
    - Rework
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done
polling:
  interval_ms: 30000
workspace:
  root: ~/code/symphony-workspaces
hooks:
  after_create: |
    git clone --depth 1 git@github.com:your-org/your-repo.git .
agent:
  harness: codex          # default harness, used when a state doesn't specify one
  max_concurrent_agents: 3
  max_turns: 20
codex:
  command: codex app-server
claude:
  command: claude
# Optional per-state composition. Each state names a prompt section below
# (## prompt:<name>) and optionally overrides the harness. States not listed
# fall back to `agent.harness` and the unnamed body at the top of this file.
states:
  Todo:           { harness: codex,  prompt: implement }
  "In Progress":  { harness: codex,  prompt: implement }
  "Human Review": { harness: claude, prompt: review }
  Rework:         { harness: codex,  prompt: rework }
  Merging:        { harness: codex,  prompt: merge }
---

Default workflow body. This text applies to any active state without an entry
in the `states:` map above. Keeping it lets single-template `WORKFLOW.md` files
go on working unchanged; remove it once every active state has a section.

## prompt:implement

You are working on Linear issue {{ issue.identifier }}: {{ issue.title }}.

{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

Instructions:

1. This is an unattended orchestration session. Never ask a human to perform follow-up actions.
2. Only stop early for a true blocker (missing required auth/permissions/secrets). If blocked, record it in the workpad and move the issue according to workflow.
3. Final message must report completed actions and blockers only. Do not include "next steps for user".

Work only in the provided repository copy. Do not touch any other path.

### Prerequisite: Linear access

You can talk to Linear via the `linear_graphql` tool:

- Under Codex, it is exposed automatically by Symphony as a client-side dynamic tool.
- Under Claude, it is exposed automatically by Symphony as an MCP server tool (`symphony-linear`).

If neither is available, stop and report the missing prerequisite.

Tool input shape:

```json
{
  "query": "<graphql operation document>",
  "variables": { "optional": "graphql variables" }
}
```

Treat a top-level `errors` array on the response as a failed operation even if the tool call itself returned successfully.

### Default posture

- Open the persistent workpad comment first and bring it up to date before doing new implementation work.
- Spend extra effort on planning and verification design before implementation.
- Reproduce first: always confirm the current behavior/issue signal before changing code so the fix target is explicit.
- Treat one persistent Linear comment as the source of truth for progress; do not post separate "done"/summary comments.
- Treat any ticket-authored `Validation`, `Test Plan`, or `Testing` section as non-negotiable acceptance input.
- File a separate Linear `Backlog` issue when meaningful out-of-scope work is discovered; do not expand current scope.
- Operate autonomously end-to-end unless blocked.

### Step 0: Kickoff

1. Fetch the issue by explicit identifier using `linear_graphql`.
2. If state is `Todo`, transition to `In Progress` via `issueUpdate` before any code work.
3. Find or create the single `## Codex Workpad` bootstrap comment for the issue.
4. If a branch PR is already attached and is `CLOSED` or `MERGED`, treat prior branch work as non-reusable: create a fresh branch from `origin/main` and restart as a new attempt.

### Step 1: Plan

1. Reconcile the workpad before new edits: check off items already done, expand/fix the plan, refresh acceptance criteria and validation.
2. Write/update a hierarchical plan in the workpad, with explicit acceptance criteria and TODOs in checklist form.
3. Include a compact environment stamp at the top: `<host>:<abs-workdir>@<short-sha>`.
4. If the ticket includes `Validation`/`Test Plan`/`Testing`, copy them into the workpad's required checkboxes.
5. Capture a concrete reproduction signal before implementing and record it in workpad `Notes`.
6. Sync with `origin/main` and record the result in workpad `Notes`.

### Step 2: Execute

1. Treat the workpad comment as the active execution checklist; update it after each meaningful milestone.
2. Implement against the hierarchical TODOs.
3. Run all required validation/tests. Mandatory gate: execute every ticket-provided `Validation`/`Test Plan`/`Testing` item.
4. Re-check acceptance criteria and close any gaps.
5. Before every `git push`, confirm the required validation passes; commit and push.
6. Attach the PR URL to the issue.
7. Merge latest `origin/main` into the branch, resolve conflicts, rerun checks.
8. Update the workpad with final checklist status and validation notes.

### Step 3: Hand off to review

When the completion bar below is satisfied, transition the issue state to `Human Review`. From that point a separate review handler takes over (see `## prompt:review`).

Completion bar:

- Workpad reflects the completed plan, acceptance criteria, and validation results.
- Required validation/tests are green for the latest commit.
- PR checks are green, branch is pushed, PR is linked on the issue.

### Workpad template

````md
## Codex Workpad

```text
<hostname>:<abs-path>@<short-sha>
```

### Plan

- [ ] Top-level milestone
  - [ ] Sub-task

### Acceptance Criteria

- [ ] Concrete user-facing or behavior-level criterion

### Validation

- [ ] Required test command(s) and expected outcome

### Notes

- Reproduction evidence, pull/sync result, decisions

### Confusions

- Anything unclear during execution (omit when empty)
````

## prompt:review

You are reviewing the in-flight PR for Linear issue {{ issue.identifier }}: {{ issue.title }}.

The implementing agent has handed control over by moving the ticket to `Human Review`. Your job here is to do a rigorous second-opinion pass — not to keep coding.

1. Use `linear_graphql` to fetch the issue and the persistent `## Codex Workpad` comment so you have full context.
2. Identify the PR attached to the issue. Read the diff, the description, and every existing review/inline comment.
3. Verify the completion bar from `## prompt:implement` is actually met against the diff:
   - Acceptance criteria items match observable behavior.
   - `Validation`/`Test Plan`/`Testing` steps have evidence (CI runs, manual notes in workpad).
   - Tests cover the changed surface and would fail without the fix.
4. Look for the usual review red flags: silent failure paths, missing input validation, race conditions, dropped error handling, dead code, scope creep beyond the ticket.
5. Post one consolidated review comment on the PR summarizing findings. Use sections: `Looks Good`, `Must Fix Before Merge`, `Nice To Have`.
6. Append a short `### Review pass <UTC timestamp>` block to the existing `## Codex Workpad` comment with the same summary.
7. Decide the next state:
   - If anything sits under `Must Fix Before Merge`, transition the issue to `Rework`.
   - Otherwise leave the issue in `Human Review` for the human approver and stop. Do not move it to `Merging` yourself.

Do not edit code in this state. If you find a bug worth fixing, it must go through `Rework`.

## prompt:rework

The reviewer has moved {{ issue.identifier }} back to `Rework`. Treat this as a full approach reset, not incremental patching.

1. Re-read the full issue body, every human comment, and the latest review comment on the PR.
2. Use `linear_graphql` to remove the existing `## Codex Workpad` comment from the issue so the next implementation pass starts from a clean slate.
3. Close the existing PR tied to the issue.
4. Create a fresh branch from `origin/main`.
5. Transition the issue back to `In Progress` and restart from the kickoff flow described in `## prompt:implement`.

## prompt:merge

The human approver has moved {{ issue.identifier }} to `Merging`. Drive the PR to a merged state.

1. Confirm the PR is approved and all required checks are green.
2. If a `land` skill is available in this environment, use it instead of calling `gh pr merge` directly.
3. Otherwise merge the PR via `gh pr merge --squash --auto` (or the repo's documented merge style) and wait for the merge to land.
4. After merge, transition the issue to `Done` via `linear_graphql`.
5. Update the `## Codex Workpad` comment with a final `### Merged <UTC timestamp>` line referencing the merge commit SHA.

Stop once the issue is `Done` and the workpad reflects the merge.
