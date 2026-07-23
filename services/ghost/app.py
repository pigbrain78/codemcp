"""Ghost Team -- the quiet advisory council.

Implements the constitution's Ghost Team initiative (SOVEREIGN.md):
specialists continuously observe the platform's event stream and *notice*
things -- duplicated work, recurring failure signatures, unused agents,
missing documentation. They never interrupt and never change anything:
each notice becomes an observation with evidence and a recommendation,
deduplicated by signature so the council doesn't nag, and recorded on the
ledger as a GhostObservation event.

Observers (named for the fleet personas they emulate):

- dr-quinn / recurring_failure  -- "we've seen this failure signature before"
- ralph5   / duplicate_work     -- "you're about to redo work already dispatched"
- igor     / unused_agent       -- "this agent registered but nothing routes to it"
- kid      / missing_docs       -- "this lesson was recorded without reuse notes"

POST /observe pulls new ledger events (through the gateway, incremental
cursor) and runs every observer. Humans acknowledge or dismiss
observations; both decisions are theirs alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("GHOST_DB_PATH", "ghost.db")

FETCH_LIMIT = 1000

# An agent counts as unused once this many meaningful events have passed
# since it registered without a single dispatch routed to it.
UNUSED_AGENT_WINDOW = int(os.environ.get("UNUSED_AGENT_WINDOW", "10"))

NOISE_EVENT_TYPES = {"GatewayRequestHandled"}


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
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cursor_ts TEXT NOT NULL DEFAULT '',
                meaningful_seq INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO state (id, cursor_ts, meaningful_seq) VALUES (1, '', 0)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signatures (
                observer TEXT NOT NULL,
                signature TEXT NOT NULL,
                count INTEGER NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (observer, signature)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents_seen (
                name TEXT PRIMARY KEY,
                registered_at_seq INTEGER NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY,
                observer TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT NOT NULL,
                summary TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                evidence TEXT NOT NULL,
                severity TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE (observer, signature)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _ledger_append(payload: dict[str, Any], correlation_id: str) -> None:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/events",
            json={
                "type": "GhostObservation",
                "source": "ghost-team",
                "payload": payload,
                "correlation_id": correlation_id,
            },
            headers={"Authorization": f"Bearer {GATEWAY_API_KEY}"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway unavailable: {exc}"
        ) from exc
    if response.status_code != 201:
        raise HTTPException(
            status_code=502,
            detail=f"gateway rejected GhostObservation: {response.status_code} {response.text}",
        )


def _ledger_events(since: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": FETCH_LIMIT}
    if since:
        params["since"] = since
    try:
        response = httpx.get(
            f"{GATEWAY_URL}/events",
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


class Observation(BaseModel):
    id: str
    observer: str
    kind: str
    summary: str
    recommendation: str
    evidence: list[str]
    severity: str
    occurrences: int
    status: str
    first_seen: str
    last_seen: str


class ObserveResult(BaseModel):
    events_examined: int
    new_observations: int
    updated_observations: int


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        id=row["id"],
        observer=row["observer"],
        kind=row["kind"],
        summary=row["summary"],
        recommendation=row["recommendation"],
        evidence=json.loads(row["evidence"]),
        severity=row["severity"],
        occurrences=row["occurrences"],
        status=row["status"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
    )


class _Notice(BaseModel):
    observer: str
    kind: str
    signature: str
    summary: str
    recommendation: str
    evidence: list[str]
    severity: str


def _bump_signature(
    conn: sqlite3.Connection, observer: str, signature: str, event_id: str
) -> tuple[int, list[str]]:
    row = conn.execute(
        "SELECT count, evidence FROM signatures WHERE observer = ? AND signature = ?",
        (observer, signature),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO signatures (observer, signature, count, evidence) VALUES (?, ?, 1, ?)",
            (observer, signature, json.dumps([event_id])),
        )
        return 1, [event_id]
    evidence = json.loads(row["evidence"])
    evidence.append(event_id)
    count = row["count"] + 1
    conn.execute(
        "UPDATE signatures SET count = ?, evidence = ? WHERE observer = ? AND signature = ?",
        (count, json.dumps(evidence), observer, signature),
    )
    return count, evidence


def _observe_event(conn: sqlite3.Connection, event: dict[str, Any]) -> list[_Notice]:
    """Run the per-event observers; returns notices triggered by this event."""
    notices: list[_Notice] = []
    payload = event.get("payload", {})

    if event["type"] == "AgentFailed":
        agent = payload.get("agent", "?")
        capability = payload.get("capability", "?")
        error = payload.get("error") or ""
        signature = _sig("failure", agent, capability, error)
        count, evidence = _bump_signature(conn, "dr-quinn", signature, event["id"])
        if count >= 2:
            notices.append(
                _Notice(
                    observer="dr-quinn",
                    kind="recurring_failure",
                    signature=signature,
                    summary=f"Failure signature seen {count}x: {agent}/{capability}: {error[:120]}",
                    recommendation="Diagnose the root cause once instead of paying for this failure repeatedly.",
                    evidence=evidence,
                    severity="warning",
                )
            )

    if event["type"] == "AgentRequested":
        capability = payload.get("capability", "?")
        input_json = json.dumps(payload.get("input", {}), sort_keys=True)
        signature = _sig("dispatch", capability, input_json)
        count, evidence = _bump_signature(conn, "ralph5", signature, event["id"])
        if count >= 2:
            notices.append(
                _Notice(
                    observer="ralph5",
                    kind="duplicate_work",
                    signature=signature,
                    summary=f"Identical task dispatched {count}x: capability '{capability}' with the same input.",
                    recommendation="Reuse the previous result or extract this into a cached/reusable pattern.",
                    evidence=evidence,
                    severity="info",
                )
            )

    if (
        event["type"] == "EvolutionRecorded"
        and not (payload.get("reuse_potential") or "").strip()
    ):
        signature = _sig("undocumented-lesson", event["id"])
        notices.append(
            _Notice(
                observer="kid",
                kind="missing_docs",
                signature=signature,
                summary="A lesson was recorded without any reuse-potential notes.",
                recommendation="Add reuse potential so the lesson can become an architectural gene.",
                evidence=[event["id"]],
                severity="info",
            )
        )

    return notices


def _track_agents(
    conn: sqlite3.Connection, event: dict[str, Any], meaningful_seq: int
) -> None:
    payload = event.get("payload", {})
    if event["type"] == "AgentRegistered":
        conn.execute(
            """
            INSERT INTO agents_seen (name, registered_at_seq, request_count)
            VALUES (?, ?, 0)
            ON CONFLICT(name) DO NOTHING
            """,
            (payload.get("agent", "?"), meaningful_seq),
        )
    if event["type"] == "AgentRequested":
        conn.execute(
            "UPDATE agents_seen SET request_count = request_count + 1 WHERE name = ?",
            (payload.get("agent", "?"),),
        )


def _unused_agent_notices(
    conn: sqlite3.Connection, meaningful_seq: int
) -> list[_Notice]:
    notices: list[_Notice] = []
    rows = conn.execute("SELECT * FROM agents_seen WHERE request_count = 0").fetchall()
    for row in rows:
        if meaningful_seq - row["registered_at_seq"] >= UNUSED_AGENT_WINDOW:
            notices.append(
                _Notice(
                    observer="igor",
                    kind="unused_agent",
                    signature=_sig("unused", row["name"]),
                    summary=(
                        f"Agent '{row['name']}' registered but nothing has been routed to it "
                        f"across {meaningful_seq - row['registered_at_seq']} platform events."
                    ),
                    recommendation="Route work to it, fold its capability into another agent, or retire it.",
                    evidence=[f"agent:{row['name']}"],
                    severity="info",
                )
            )
    return notices


def _record_notice(conn: sqlite3.Connection, notice: _Notice) -> tuple[str, bool]:
    """Upsert an observation for this notice. Returns (observation_id, created)."""
    now = _now()
    existing = conn.execute(
        "SELECT * FROM observations WHERE observer = ? AND signature = ?",
        (notice.observer, notice.signature),
    ).fetchone()
    if existing is None:
        observation_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO observations
                (id, observer, kind, signature, summary, recommendation, evidence,
                 severity, occurrences, status, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?, ?)
            """,
            (
                observation_id,
                notice.observer,
                notice.kind,
                notice.signature,
                notice.summary,
                notice.recommendation,
                json.dumps(notice.evidence),
                notice.severity,
                now,
                now,
            ),
        )
        return observation_id, True

    conn.execute(
        """
        UPDATE observations
        SET summary = ?, evidence = ?, occurrences = occurrences + 1, last_seen = ?
        WHERE id = ?
        """,
        (notice.summary, json.dumps(notice.evidence), now, existing["id"]),
    )
    return existing["id"], False


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Project Sovereign Ghost Team", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/observe", response_model=ObserveResult)
def observe() -> ObserveResult:
    conn = _connect()
    try:
        state = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        cursor_ts = state["cursor_ts"]
        meaningful_seq = state["meaningful_seq"]

        events = _ledger_events(cursor_ts)

        notices: list[_Notice] = []
        newest_ts = cursor_ts
        for event in events:
            if cursor_ts and event["ts"] <= cursor_ts:
                continue
            newest_ts = max(newest_ts, event["ts"])
            if event["type"] in NOISE_EVENT_TYPES:
                continue
            # The ghost team must not observe its own observations forever.
            if event["type"] == "GhostObservation":
                continue
            meaningful_seq += 1
            _track_agents(conn, event, meaningful_seq)
            notices.extend(_observe_event(conn, event))

        notices.extend(_unused_agent_notices(conn, meaningful_seq))

        created_payloads: list[tuple[dict[str, Any], str]] = []
        new_count = 0
        updated_count = 0
        for notice in notices:
            observation_id, created = _record_notice(conn, notice)
            if created:
                new_count += 1
                created_payloads.append(
                    (
                        {
                            "observer": notice.observer,
                            "kind": notice.kind,
                            "summary": notice.summary,
                            "recommendation": notice.recommendation,
                            "evidence": notice.evidence,
                            "severity": notice.severity,
                        },
                        observation_id,
                    )
                )
            else:
                updated_count += 1

        conn.execute(
            "UPDATE state SET cursor_ts = ?, meaningful_seq = ? WHERE id = 1",
            (newest_ts, meaningful_seq),
        )
        conn.commit()
    finally:
        conn.close()

    # Ledger the new observations after committing local state; only brand-new
    # notices are ledgered, so repeat sightings update quietly.
    for payload, observation_id in created_payloads:
        _ledger_append(payload, correlation_id=observation_id)

    return ObserveResult(
        events_examined=len(events),
        new_observations=new_count,
        updated_observations=updated_count,
    )


@app.get("/observations", response_model=list[Observation])
def list_observations(
    observer: str | None = None,
    status: str | None = None,
    severity: str | None = None,
) -> list[Observation]:
    conn = _connect()
    try:
        clauses = []
        params: list[Any] = []
        if observer:
            clauses.append("observer = ?")
            params.append(observer)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM observations {where} ORDER BY last_seen DESC", params
        ).fetchall()
        return [_row_to_observation(row) for row in rows]
    finally:
        conn.close()


def _set_status(observation_id: str, status: str) -> Observation:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="observation not found")
        conn.execute(
            "UPDATE observations SET status = ? WHERE id = ?", (status, observation_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        return _row_to_observation(row)
    finally:
        conn.close()


@app.post("/observations/{observation_id}/acknowledge", response_model=Observation)
def acknowledge(observation_id: str) -> Observation:
    return _set_status(observation_id, "acknowledged")


@app.post("/observations/{observation_id}/dismiss", response_model=Observation)
def dismiss(observation_id: str) -> Observation:
    return _set_status(observation_id, "dismissed")
