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
  harness: codex          # or `claude`
  max_concurrent_agents: 3
  max_turns: 20
codex:
  command: codex app-server
claude:
  command: claude
---

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

## Prerequisite: Linear access

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

## Default posture

- Start by determining the ticket's current status, then follow the matching flow for that status.
- Open the persistent workpad comment first and bring it up to date before doing new implementation work.
- Spend extra effort on planning and verification design before implementation.
- Reproduce first: always confirm the current behavior/issue signal before changing code so the fix target is explicit.
- Treat one persistent Linear comment as the source of truth for progress; do not post separate "done"/summary comments.
- Treat any ticket-authored `Validation`, `Test Plan`, or `Testing` section as non-negotiable acceptance input.
- File a separate Linear `Backlog` issue when meaningful out-of-scope work is discovered; do not expand current scope.
- Move status only when the matching quality bar is met.
- Operate autonomously end-to-end unless blocked.

## Status map

- `Backlog` -> out of scope; do not modify.
- `Todo` -> queued; immediately transition to `In Progress` before active work. If a PR is already attached, run the PR feedback sweep first.
- `In Progress` -> implementation actively underway.
- `Human Review` -> PR is attached and validated; waiting on human approval.
- `Merging` -> approved by human; run the merge flow (do not call `gh pr merge` directly when a `land` skill is provided).
- `Rework` -> reviewer requested changes; full approach reset required.
- `Done` -> terminal; no further action.

## Step 0: Determine current ticket state and route

1. Fetch the issue by explicit identifier using `linear_graphql`.
2. Read the current state.
3. Route to the matching flow.
4. For `Todo`: do startup sequencing in this exact order:
   - `issueUpdate` to `In Progress`
   - Find or create the `## Codex Workpad` bootstrap comment
   - Only then begin analysis/planning/implementation work.
5. If a branch PR exists and is `CLOSED` or `MERGED`, treat prior branch work as non-reusable: create a fresh branch from `origin/main` and restart as a new attempt.

## Step 1: Start/continue execution (Todo or In Progress)

1. Find or create a single persistent workpad comment for the issue (header: `## Codex Workpad`).
2. Reconcile the workpad before new edits: check off items already done, expand/fix the plan, refresh acceptance criteria and validation.
3. Write/update a hierarchical plan in the workpad, with explicit acceptance criteria and TODOs in checklist form.
4. Include a compact environment stamp at the top: `<host>:<abs-workdir>@<short-sha>`.
5. If the ticket includes `Validation`/`Test Plan`/`Testing`, copy them into the workpad's required checkboxes.
6. Capture a concrete reproduction signal before implementing and record it in workpad `Notes`.
7. Sync with `origin/main` and record the result in workpad `Notes`.

## Step 2: Execution phase (Todo -> In Progress -> Human Review)

1. Verify the kickoff sync result is recorded before implementation continues.
2. If state is `Todo`, move to `In Progress`.
3. Treat the workpad comment as the active execution checklist; update it after each meaningful milestone.
4. Implement against the hierarchical TODOs.
5. Run all required validation/tests. Mandatory gate: execute every ticket-provided `Validation`/`Test Plan`/`Testing` item.
6. Re-check acceptance criteria and close any gaps.
7. Before every `git push`, confirm the required validation passes; commit and push.
8. Attach the PR URL to the issue.
9. Merge latest `origin/main` into the branch, resolve conflicts, rerun checks.
10. Update the workpad with final checklist status and validation notes.
11. Before moving to `Human Review`:
    - Run the PR feedback sweep.
    - Confirm PR checks are green.
    - Confirm every required validation item is checked.
    - Re-open the workpad and reconcile so `Plan`/`Acceptance Criteria`/`Validation` match completed work.
12. Move issue to `Human Review`.

## PR feedback sweep protocol

When a ticket has an attached PR, run this before moving to `Human Review`:

1. Identify the PR number from issue links/attachments.
2. Gather feedback from all channels (top-level comments, inline review comments, review summaries).
3. Treat every actionable reviewer comment (human or bot) as blocking until resolved with code/test/docs updates OR an explicit, justified pushback reply.
4. Update the workpad plan/checklist with each feedback item and its resolution.
5. Re-run validation after feedback-driven changes and push updates.
6. Repeat until no outstanding actionable comments remain.

## Step 3: Human Review and merge

1. While in `Human Review`, do not code or change ticket content; poll for updates.
2. If review feedback requires changes, move the issue to `Rework` and follow the rework flow.
3. When approved, the human moves the issue to `Merging`.
4. While in `Merging`, run the merge flow until the PR is merged.
5. After merge, move the issue to `Done`.

## Step 4: Rework handling

1. Treat `Rework` as a full approach reset, not incremental patching.
2. Re-read the full issue body and all human comments; identify what will be done differently.
3. Close the existing PR tied to the issue.
4. Remove the existing `## Codex Workpad` comment from the issue.
5. Create a fresh branch from `origin/main`.
6. Start over from the normal kickoff flow.

## Completion bar before Human Review

- Workpad reflects the completed plan, acceptance criteria, and validation results.
- Required validation/tests are green for the latest commit.
- PR feedback sweep complete, no actionable comments remaining.
- PR checks are green, branch is pushed, PR is linked on the issue.

## Guardrails

- If the branch PR is already closed/merged, do not reuse it; restart on a fresh branch from `origin/main`.
- If issue state is `Backlog`, do not modify it.
- Do not edit the issue body/description for planning or progress tracking.
- Use exactly one persistent workpad comment (`## Codex Workpad`) per issue.
- Temporary proof edits are allowed only for local verification and must be reverted before commit.
- Do not move to `Human Review` unless the completion bar is satisfied.
- In `Human Review`, do not make changes; wait and poll.
- If state is terminal (`Done`), do nothing and shut down.

## Workpad template

Use this exact structure for the persistent workpad comment:

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
