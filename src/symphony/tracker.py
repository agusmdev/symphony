from __future__ import annotations

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


class LinearTracker:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    async def fetch_candidate_issues(self) -> list[Issue]:
        query = """
        query SymphonyCandidateIssues($projectSlug: String!, $states: [String!], $after: String) {
          issues(
            first: 50,
            after: $after,
            filter: {
              project: { slugId: { eq: $projectSlug } },
              state: { name: { in: $states } }
            }
          ) {
            nodes {
              id identifier title description priority branchName url createdAt updatedAt
              state { name }
              labels { nodes { name } }
              inverseRelations {
                nodes {
                  type
                  issue { id identifier state { name } }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        variables: JsonObject = {
            "projectSlug": self._config.tracker.project_slug or "",
            "states": list(self._config.tracker.active_states),
            "after": None,
        }
        return await self._fetch_paginated(query, variables)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        query = """
        query SymphonyIssuesByStates($projectSlug: String!, $states: [String!], $after: String) {
          issues(
            first: 50,
            after: $after,
            filter: {
              project: { slugId: { eq: $projectSlug } },
              state: { name: { in: $states } }
            }
          ) {
            nodes {
              id identifier title description priority branchName url createdAt updatedAt
              state { name }
              labels { nodes { name } }
              inverseRelations { nodes { type issue { id identifier state { name } } } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        variables: JsonObject = {
            "projectSlug": self._config.tracker.project_slug or "",
            "states": cast(JsonValue, state_names),
            "after": None,
        }
        return await self._fetch_paginated(query, variables)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        query = """
        query SymphonyIssueStates($ids: [ID!]) {
          issues(filter: { id: { in: $ids } }) {
            nodes {
              id identifier title description priority branchName url createdAt updatedAt
              state { name }
              labels { nodes { name } }
              inverseRelations { nodes { type issue { id identifier state { name } } } }
            }
          }
        }
        """
        data = await self._graphql(query, {"ids": cast(JsonValue, issue_ids)})
        issues = _path(data, ["data", "issues", "nodes"])
        if not isinstance(issues, list):
            raise TrackerError("linear_unknown_payload", "missing issue nodes")
        return [_normalize_issue(item) for item in issues if isinstance(item, dict)]

    async def _fetch_paginated(self, query: str, variables: JsonObject) -> list[Issue]:
        results: list[Issue] = []
        while True:
            data = await self._graphql(query, variables)
            issues = _path(data, ["data", "issues"])
            if not isinstance(issues, dict):
                raise TrackerError("linear_unknown_payload", "missing issues payload")
            nodes = issues.get("nodes")
            if not isinstance(nodes, list):
                raise TrackerError("linear_unknown_payload", "missing issue nodes")
            results.extend(_normalize_issue(item) for item in nodes if isinstance(item, dict))
            page_info = issues.get("pageInfo")
            if not isinstance(page_info, dict) or not bool(page_info.get("hasNextPage")):
                return results
            end_cursor = page_info.get("endCursor")
            if not isinstance(end_cursor, str) or not end_cursor:
                raise TrackerError("linear_missing_end_cursor", "pagination missing endCursor")
            variables = dict(variables)
            variables["after"] = end_cursor

    async def _graphql(self, query: str, variables: JsonObject) -> JsonObject:
        if not self._config.tracker.api_key:
            raise TrackerError("missing_tracker_api_key", "Linear API key is missing")
        body = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            self._config.tracker.endpoint,
            data=body,
            headers={
                "Authorization": self._config.tracker.api_key,
                "Content-Type": "application/json",
            },
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


def _normalize_issue(raw: dict[str, Any]) -> Issue:
    state = raw.get("state")
    state_name = state.get("name") if isinstance(state, dict) else None
    label_nodes = _path(raw, ["labels", "nodes"])
    labels = label_nodes if isinstance(label_nodes, list) else []
    relations = _path(raw, ["inverseRelations", "nodes"])
    return Issue(
        id=_required_str(raw, "id"),
        identifier=_required_str(raw, "identifier"),
        title=_required_str(raw, "title"),
        description=raw.get("description") if isinstance(raw.get("description"), str) else None,
        priority=raw.get("priority") if isinstance(raw.get("priority"), int) else None,
        state=state_name if isinstance(state_name, str) else "",
        branch_name=raw.get("branchName") if isinstance(raw.get("branchName"), str) else None,
        url=raw.get("url") if isinstance(raw.get("url"), str) else None,
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
