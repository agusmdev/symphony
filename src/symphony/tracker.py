from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Protocol, cast

from symphony.config import ServiceConfig
from symphony.errors import TrackerError
from symphony.models import BlockerRef, Issue, JsonObject, JsonValue


class IssueTracker(Protocol):
    async def fetch_candidate_issues(self) -> list[Issue]: ...
    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]: ...
    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...
    async def resolve_viewer_id(self) -> str | None: ...
    async def create_comment(self, issue_id: str, body: str) -> None: ...
    async def update_issue_state(self, issue_id: str, state_name: str) -> None: ...
    async def graphql(self, query: str, variables: JsonObject) -> JsonObject: ...


_ISSUE_FIELDS = """
id identifier title description priority branchName url createdAt updatedAt
state { name }
assignee { id }
labels { nodes { name } }
inverseRelations { nodes { type issue { id identifier state { name } } } }
"""


class LinearTracker:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._viewer_id: str | None = None
        self._assignee_match: str | None = None
        self._assignee_resolved: bool = False
        self._assignee_lock = asyncio.Lock()

    async def fetch_candidate_issues(self) -> list[Issue]:
        query = (
            "query SymphonyCandidateIssues("
            "$projectSlug: String!, $states: [String!], $after: String) {"
            "  issues(first: 50, after: $after, filter: {"
            "    project: { slugId: { eq: $projectSlug } },"
            "    state: { name: { in: $states } }"
            "  }) {"
            f"    nodes {{ {_ISSUE_FIELDS} }}"
            "    pageInfo { hasNextPage endCursor }"
            "  }"
            "}"
        )
        variables: JsonObject = {
            "projectSlug": self._config.tracker.project_slug or "",
            "states": list(self._config.tracker.active_states),
            "after": None,
        }
        return await self._fetch_paginated(query, variables)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        query = (
            "query SymphonyIssuesByStates("
            "$projectSlug: String!, $states: [String!], $after: String) {"
            "  issues(first: 50, after: $after, filter: {"
            "    project: { slugId: { eq: $projectSlug } },"
            "    state: { name: { in: $states } }"
            "  }) {"
            f"    nodes {{ {_ISSUE_FIELDS} }}"
            "    pageInfo { hasNextPage endCursor }"
            "  }"
            "}"
        )
        variables: JsonObject = {
            "projectSlug": self._config.tracker.project_slug or "",
            "states": cast(JsonValue, state_names),
            "after": None,
        }
        return await self._fetch_paginated(query, variables)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        query = (
            "query SymphonyIssueStates($ids: [ID!]) {"
            "  issues(filter: { id: { in: $ids } }) {"
            f"    nodes {{ {_ISSUE_FIELDS} }}"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"ids": cast(JsonValue, issue_ids)})
        issues = _path(data, ["data", "issues", "nodes"])
        if not isinstance(issues, list):
            raise TrackerError("linear_unknown_payload", "missing issue nodes")
        match = await self._resolve_assignee_match()
        return [_normalize_issue(item, match) for item in issues if isinstance(item, dict)]

    async def resolve_viewer_id(self) -> str | None:
        if self._viewer_id is not None:
            return self._viewer_id
        data = await self.graphql("query SymphonyViewer { viewer { id } }", {})
        viewer_id = _path(data, ["data", "viewer", "id"])
        if isinstance(viewer_id, str) and viewer_id:
            self._viewer_id = viewer_id
            return viewer_id
        raise TrackerError("missing_linear_viewer_identity", "viewer query returned no id")

    async def create_comment(self, issue_id: str, body: str) -> None:
        mutation = (
            "mutation SymphonyCreateComment($issueId: String!, $body: String!) {"
            "  commentCreate(input: { issueId: $issueId, body: $body }) { success }"
            "}"
        )
        data = await self.graphql(mutation, {"issueId": issue_id, "body": body})
        if _path(data, ["data", "commentCreate", "success"]) is not True:
            raise TrackerError("comment_create_failed", f"commentCreate failed for {issue_id}")

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        state_id = await self._resolve_state_id(issue_id, state_name)
        mutation = (
            "mutation SymphonyUpdateIssueState($issueId: String!, $stateId: String!) {"
            "  issueUpdate(id: $issueId, input: { stateId: $stateId }) { success }"
            "}"
        )
        data = await self.graphql(mutation, {"issueId": issue_id, "stateId": state_id})
        if _path(data, ["data", "issueUpdate", "success"]) is not True:
            raise TrackerError("issue_update_failed", f"issueUpdate failed for {issue_id}")

    async def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        query = (
            "query SymphonyResolveStateId($issueId: String!, $stateName: String!) {"
            "  issue(id: $issueId) {"
            "    team { states(filter: { name: { eq: $stateName } }, first: 1) { nodes { id } } }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"issueId": issue_id, "stateName": state_name})
        states = _path(data, ["data", "issue", "team", "states", "nodes"])
        if isinstance(states, list) and states:
            state = states[0]
            if isinstance(state, dict) and isinstance(state.get("id"), str):
                return cast(str, state["id"])
        raise TrackerError("state_not_found", f"no state {state_name!r} for issue {issue_id}")

    async def _fetch_paginated(self, query: str, variables: JsonObject) -> list[Issue]:
        match = await self._resolve_assignee_match()
        results: list[Issue] = []
        while True:
            data = await self.graphql(query, variables)
            issues = _path(data, ["data", "issues"])
            if not isinstance(issues, dict):
                raise TrackerError("linear_unknown_payload", "missing issues payload")
            nodes = issues.get("nodes")
            if not isinstance(nodes, list):
                raise TrackerError("linear_unknown_payload", "missing issue nodes")
            results.extend(
                _normalize_issue(item, match) for item in nodes if isinstance(item, dict)
            )
            page_info = issues.get("pageInfo")
            if not isinstance(page_info, dict) or not bool(page_info.get("hasNextPage")):
                return results
            end_cursor = page_info.get("endCursor")
            if not isinstance(end_cursor, str) or not end_cursor:
                raise TrackerError("linear_missing_end_cursor", "pagination missing endCursor")
            variables = dict(variables)
            variables["after"] = end_cursor

    async def _resolve_assignee_match(self) -> str | None:
        if self._assignee_resolved:
            return self._assignee_match
        async with self._assignee_lock:
            if self._assignee_resolved:
                return self._assignee_match
            configured = (self._config.tracker.assignee or "").strip() or None
            if configured is None:
                self._assignee_match = None
            elif configured.lower() == "me":
                self._assignee_match = await self.resolve_viewer_id()
            else:
                self._assignee_match = configured
            self._assignee_resolved = True
            return self._assignee_match

    async def graphql(self, query: str, variables: JsonObject) -> JsonObject:
        if not self._config.tracker.api_key:
            raise TrackerError("missing_tracker_api_key", "Linear API key is missing")
        return await asyncio.to_thread(
            _graphql_request_blocking,
            self._config.tracker.endpoint,
            self._config.tracker.api_key,
            query,
            variables,
        )


def _graphql_request_blocking(
    endpoint: str, api_key: str, query: str, variables: JsonObject
) -> JsonObject:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raise TrackerError("linear_api_status", f"Linear HTTP status {exc.code}") from exc
    except OSError as exc:
        raise TrackerError("linear_api_request", str(exc)) from exc
    if status != 200:
        raise TrackerError("linear_api_status", f"Linear HTTP status {status}")
    parsed = json.loads(payload.decode())
    if not isinstance(parsed, dict):
        raise TrackerError("linear_unknown_payload", "GraphQL response is not an object")
    if parsed.get("errors"):
        raise TrackerError("linear_graphql_errors", "Linear returned GraphQL errors")
    return cast(JsonObject, parsed)


def _normalize_issue(raw: dict[str, Any], assignee_match: str | None) -> Issue:
    state = raw.get("state")
    state_name = state.get("name") if isinstance(state, dict) else None
    label_nodes = _path(raw, ["labels", "nodes"])
    labels = label_nodes if isinstance(label_nodes, list) else []
    relations = _path(raw, ["inverseRelations", "nodes"])
    assignee = raw.get("assignee")
    assignee_id = assignee.get("id") if isinstance(assignee, dict) else None
    normalized_assignee_id = assignee_id if isinstance(assignee_id, str) else None
    if assignee_match is None:
        assigned_to_worker = True
    else:
        assigned_to_worker = normalized_assignee_id == assignee_match
    return Issue(
        id=_required_str(raw, "id"),
        identifier=_required_str(raw, "identifier"),
        title=_required_str(raw, "title"),
        description=raw.get("description") if isinstance(raw.get("description"), str) else None,
        priority=raw.get("priority") if isinstance(raw.get("priority"), int) else None,
        state=state_name if isinstance(state_name, str) else "",
        branch_name=raw.get("branchName") if isinstance(raw.get("branchName"), str) else None,
        url=raw.get("url") if isinstance(raw.get("url"), str) else None,
        assignee_id=normalized_assignee_id,
        assigned_to_worker=assigned_to_worker,
        labels=[
            item["name"].lower()
            for item in labels
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ],
        blocked_by=_blockers(relations),
        created_at=_parse_dt(raw.get("createdAt")),
        updated_at=_parse_dt(raw.get("updatedAt")),
    )


def _blockers(relations: Any) -> list[BlockerRef]:
    if not isinstance(relations, list):
        return []
    blockers: list[BlockerRef] = []
    for relation in relations:
        if not isinstance(relation, dict) or relation.get("type") != "blocks":
            continue
        issue = relation.get("issue")
        state = issue.get("state") if isinstance(issue, dict) else None
        blockers.append(
            BlockerRef(
                id=issue.get("id")
                if isinstance(issue, dict) and isinstance(issue.get("id"), str)
                else None,
                identifier=issue.get("identifier")
                if isinstance(issue, dict) and isinstance(issue.get("identifier"), str)
                else None,
                state=state.get("name")
                if isinstance(state, dict) and isinstance(state.get("name"), str)
                else None,
            )
        )
    return blockers


def _path(value: Any, keys: list[str]) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise TrackerError("linear_unknown_payload", f"missing required issue field {key}")
    return value


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
