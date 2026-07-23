"""Append-only ledger service.

Implements the "Enterprise Memory Layer" ledger described in the Project
Sovereign architecture: every event in the ecosystem (CaptureCreated,
TaskAssigned, AgentCompleted, ...) is appended here and hash-chained so
tampering with history can be detected. Records are never updated or
deleted through this API.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

GENESIS_HASH = "0" * 64

DB_PATH = os.environ.get("LEDGER_DB_PATH", "ledger.db")

# Hash chaining requires reading the previous row and inserting the next one
# without another writer interleaving, so all writes go through this lock.
_write_lock = threading.Lock()


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
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload TEXT NOT NULL,
                correlation_id TEXT,
                ts TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _canonical_hash(prev_hash: str, record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


class EventIn(BaseModel):
    type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


class EventOut(BaseModel):
    seq: int
    id: str
    type: str
    source: str
    payload: dict[str, Any]
    correlation_id: str | None
    ts: str
    prev_hash: str
    hash: str


def _row_to_event(row: sqlite3.Row) -> EventOut:
    return EventOut(
        seq=row["seq"],
        id=row["id"],
        type=row["type"],
        source=row["source"],
        payload=json.loads(row["payload"]),
        correlation_id=row["correlation_id"],
        ts=row["ts"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Project Sovereign Ledger", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events", response_model=EventOut, status_code=201)
def append_event(event: EventIn) -> EventOut:
    with _write_lock:
        conn = _connect()
        try:
            last = conn.execute(
                "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["hash"] if last else GENESIS_HASH

            record = {
                "id": str(uuid.uuid4()),
                "type": event.type,
                "source": event.source,
                "payload": event.payload,
                "correlation_id": event.correlation_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            record_hash = _canonical_hash(prev_hash, record)

            conn.execute(
                """
                INSERT INTO events
                    (id, type, source, payload, correlation_id, ts, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["type"],
                    record["source"],
                    json.dumps(record["payload"]),
                    record["correlation_id"],
                    record["ts"],
                    prev_hash,
                    record_hash,
                ),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (record["id"],)
            ).fetchone()
            return _row_to_event(row)
        finally:
            conn.close()


@app.get("/events", response_model=list[EventOut])
def list_events(
    type: str | None = None,
    source: str | None = None,
    since: str | None = None,
    limit: int = Query(default=100, le=1000, gt=0),
) -> list[EventOut]:
    conn = _connect()
    try:
        clauses = []
        params: list[Any] = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        rows = conn.execute(
            f"SELECT * FROM events {where} ORDER BY seq ASC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_event(row) for row in rows]
    finally:
        conn.close()


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str) -> EventOut:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="event not found")
        return _row_to_event(row)
    finally:
        conn.close()


class VerifyResult(BaseModel):
    valid: bool
    count: int
    broken_at_seq: int | None = None


@app.get("/verify", response_model=VerifyResult)
def verify_chain() -> VerifyResult:
    """Recompute every hash in sequence and confirm the chain is intact."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
        prev_hash = GENESIS_HASH
        for row in rows:
            record = {
                "id": row["id"],
                "type": row["type"],
                "source": row["source"],
                "payload": json.loads(row["payload"]),
                "correlation_id": row["correlation_id"],
                "ts": row["ts"],
            }
            if row["prev_hash"] != prev_hash:
                return VerifyResult(
                    valid=False, count=len(rows), broken_at_seq=row["seq"]
                )
            expected_hash = _canonical_hash(prev_hash, record)
            if expected_hash != row["hash"]:
                return VerifyResult(
                    valid=False, count=len(rows), broken_at_seq=row["seq"]
                )
            prev_hash = row["hash"]
        return VerifyResult(valid=True, count=len(rows), broken_at_seq=None)
    finally:
        conn.close()
