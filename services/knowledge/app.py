"""Knowledge graph / search service.

The "Knowledge Graph" piece of the Enterprise Memory Layer. It pulls
CaptureCreated events from the ledger -- read through the gateway, never
directly, same governance rule as every other service here -- and builds:

- a full-text search index over capture content (SQLite FTS5)
- a light graph of capture -> project and capture -> tag relationships

Full-text search today is a deliberate placeholder for real
embeddings/vector search or Neo4j later; the API shape (`/search`,
`/projects`, `/tags`) is meant to stay stable across that swap.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "knowledge.db")

# Only these ledger event types represent something worth indexing into the
# knowledge graph; audit/governance events (e.g. GatewayRequestHandled) are
# deliberately excluded.
INDEXED_EVENT_TYPES = {"CaptureCreated"}

SYNC_PAGE_LIMIT = 1000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                event_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                capture_type TEXT,
                content TEXT NOT NULL,
                project TEXT,
                source TEXT,
                ts TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capture_tags (
                event_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (event_id, tag)
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
                event_id UNINDEXED, content, project, tags
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_ts TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO sync_state (id, last_ts) VALUES (1, '')")
        conn.commit()
    finally:
        conn.close()


def _gateway_get(path: str, params: dict[str, Any]) -> Any:
    try:
        response = httpx.get(
            f"{GATEWAY_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {GATEWAY_API_KEY}"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway unavailable: {exc}"
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"gateway rejected read: {response.status_code} {response.text}",
        )
    return response.json()


def _index_event(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    """Upserts one ledger event into the local index. Returns True if it was
    newly indexed (append-only mirror, so re-syncing the same event is a
    no-op)."""
    if event["type"] not in INDEXED_EVENT_TYPES:
        return False

    payload = event["payload"]
    tags = payload.get("tags") or []

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO captures
            (event_id, seq, capture_type, content, project, source, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["id"],
            event["seq"],
            payload.get("capture_type"),
            payload.get("content", ""),
            payload.get("project"),
            event["source"],
            event["ts"],
        ),
    )
    if cursor.rowcount == 0:
        return False

    conn.execute(
        "INSERT INTO captures_fts (event_id, content, project, tags) VALUES (?, ?, ?, ?)",
        (
            event["id"],
            payload.get("content", ""),
            payload.get("project") or "",
            " ".join(tags),
        ),
    )
    for tag in tags:
        conn.execute(
            "INSERT OR IGNORE INTO capture_tags (event_id, tag) VALUES (?, ?)",
            (event["id"], tag),
        )
    return True


def _tags_for(conn: sqlite3.Connection, event_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM capture_tags WHERE event_id = ? ORDER BY tag", (event_id,)
    ).fetchall()
    return [row["tag"] for row in rows]


def _capture_record(conn: sqlite3.Connection, row: sqlite3.Row) -> "CaptureRecord":
    return CaptureRecord(
        event_id=row["event_id"],
        seq=row["seq"],
        capture_type=row["capture_type"],
        content=row["content"],
        project=row["project"],
        source=row["source"],
        ts=row["ts"],
        tags=_tags_for(conn, row["event_id"]),
    )


class CaptureRecord(BaseModel):
    event_id: str
    seq: int
    capture_type: str | None
    content: str
    project: str | None
    source: str | None
    ts: str
    tags: list[str]


class SyncResult(BaseModel):
    fetched: int
    indexed: int
    last_ts: str


class ProjectSummary(BaseModel):
    project: str
    capture_count: int


class TagSummary(BaseModel):
    tag: str
    capture_count: int


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Project Sovereign Knowledge Graph", version="0.1.0", lifespan=_lifespan
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sync", response_model=SyncResult)
def sync() -> SyncResult:
    conn = _connect()
    try:
        state = conn.execute("SELECT last_ts FROM sync_state WHERE id = 1").fetchone()
        last_ts = state["last_ts"]

        params: dict[str, Any] = {"limit": SYNC_PAGE_LIMIT}
        if last_ts:
            params["since"] = last_ts
        events = _gateway_get("/events", params)

        indexed = 0
        newest_ts = last_ts
        for event in events:
            if _index_event(conn, event):
                indexed += 1
            if event["ts"] > newest_ts:
                newest_ts = event["ts"]

        conn.execute("UPDATE sync_state SET last_ts = ? WHERE id = 1", (newest_ts,))
        conn.commit()
        return SyncResult(fetched=len(events), indexed=indexed, last_ts=newest_ts)
    finally:
        conn.close()


@app.get("/search", response_model=list[CaptureRecord])
def search(
    q: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    limit: int = Query(default=20, le=200, gt=0),
) -> list[CaptureRecord]:
    conn = _connect()
    try:
        joins = ""
        clauses = []
        params: list[Any] = []

        if tag:
            joins += " JOIN capture_tags ct ON ct.event_id = c.event_id"
            clauses.append("ct.tag = ?")
            params.append(tag)
        if q:
            joins += " JOIN captures_fts ON captures_fts.event_id = c.event_id"
            clauses.append("captures_fts MATCH ?")
            params.append(q)
        if project:
            clauses.append("c.project = ?")
            params.append(project)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        rows = conn.execute(
            f"SELECT DISTINCT c.* FROM captures c {joins} {where_sql} ORDER BY c.seq DESC LIMIT ?",
            params,
        ).fetchall()
        return [_capture_record(conn, row) for row in rows]
    finally:
        conn.close()


@app.get("/projects", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT project, COUNT(*) as capture_count
            FROM captures
            WHERE project IS NOT NULL
            GROUP BY project
            ORDER BY capture_count DESC
            """
        ).fetchall()
        return [
            ProjectSummary(project=row["project"], capture_count=row["capture_count"])
            for row in rows
        ]
    finally:
        conn.close()


@app.get("/projects/{name}", response_model=list[CaptureRecord])
def get_project(name: str) -> list[CaptureRecord]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM captures WHERE project = ? ORDER BY seq DESC", (name,)
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="project not found")
        return [_capture_record(conn, row) for row in rows]
    finally:
        conn.close()


@app.get("/tags", response_model=list[TagSummary])
def list_tags() -> list[TagSummary]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT tag, COUNT(*) as capture_count FROM capture_tags GROUP BY tag ORDER BY capture_count DESC"
        ).fetchall()
        return [
            TagSummary(tag=row["tag"], capture_count=row["capture_count"])
            for row in rows
        ]
    finally:
        conn.close()


@app.get("/tags/{tag}", response_model=list[CaptureRecord])
def get_tag(tag: str) -> list[CaptureRecord]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT c.* FROM captures c
            JOIN capture_tags ct ON ct.event_id = c.event_id
            WHERE ct.tag = ?
            ORDER BY c.seq DESC
            """,
            (tag,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="tag not found")
        return [_capture_record(conn, row) for row in rows]
    finally:
        conn.close()
