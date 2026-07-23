"""Notion sync service.

Synchronizes memory into Notion, per the architecture: "Notion becomes
your enterprise workspace... think of it as the user-facing workspace
rather than the database of record." This service is one-directional
(knowledge graph -> Notion): it reads project/capture summaries from the
knowledge service and upserts one Notion page per project, so a project
page always reflects what's in the ledger without anyone copy-pasting.

It talks to the real Notion REST API using an integration token the
operator provides via NOTION_API_TOKEN -- this code never carries or
assumes access to any particular Notion workspace.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

KNOWLEDGE_URL = os.environ.get("KNOWLEDGE_URL", "http://localhost:8003")
NOTION_API_URL = os.environ.get("NOTION_API_URL", "https://api.notion.com")
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")

# Names of the properties this service expects to exist on the target
# Notion database. The operator must create a database with a title
# property matching NOTION_TITLE_PROPERTY, plus these two.
NOTION_TITLE_PROPERTY = os.environ.get("NOTION_TITLE_PROPERTY", "Name")
NOTION_CAPTURES_PROPERTY = "Captures"
NOTION_TAGS_PROPERTY = "Tags"


def _notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _knowledge_get(path: str) -> Any:
    try:
        response = httpx.get(f"{KNOWLEDGE_URL}{path}", timeout=10.0)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"knowledge service unavailable: {exc}"
        ) from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"knowledge service rejected read: {response.status_code} {response.text}",
        )
    return response.json()


def _notion_request(
    method: str, path: str, json_body: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = httpx.request(
            method,
            f"{NOTION_API_URL}{path}",
            headers=_notion_headers(),
            json=json_body,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"notion unavailable: {exc}"
        ) from exc
    if response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"notion rejected {method} {path}: {response.status_code} {response.text}",
        )
    return response.json()


def _build_properties(
    project: str, capture_count: int, tags: list[str]
) -> dict[str, Any]:
    return {
        NOTION_TITLE_PROPERTY: {"title": [{"text": {"content": project}}]},
        NOTION_CAPTURES_PROPERTY: {"number": capture_count},
        NOTION_TAGS_PROPERTY: {"multi_select": [{"name": tag} for tag in tags]},
    }


def _find_project_page(project: str) -> str | None:
    body = {"filter": {"property": NOTION_TITLE_PROPERTY, "title": {"equals": project}}}
    result = _notion_request("POST", f"/v1/databases/{NOTION_DATABASE_ID}/query", body)
    pages = result.get("results", [])
    return pages[0]["id"] if pages else None


def _upsert_project_page(project: str, capture_count: int, tags: list[str]) -> str:
    properties = _build_properties(project, capture_count, tags)
    existing_id = _find_project_page(project)
    if existing_id:
        _notion_request("PATCH", f"/v1/pages/{existing_id}", {"properties": properties})
        return "updated"

    body = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
    _notion_request("POST", "/v1/pages", body)
    return "created"


class SyncResult(BaseModel):
    projects_seen: int
    pages_created: int
    pages_updated: int


app = FastAPI(title="Project Sovereign Notion Sync", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sync", response_model=SyncResult)
def sync() -> SyncResult:
    projects = _knowledge_get("/projects")

    created = 0
    updated = 0
    for entry in projects:
        project = entry["project"]
        captures = _knowledge_get(f"/projects/{project}")
        tags = sorted({tag for capture in captures for tag in capture["tags"]})

        outcome = _upsert_project_page(project, len(captures), tags)
        if outcome == "created":
            created += 1
        else:
            updated += 1

    return SyncResult(
        projects_seen=len(projects), pages_created=created, pages_updated=updated
    )
