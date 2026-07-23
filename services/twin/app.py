"""Digital Twin -- the platform's living structural model.

Implements the constitution's Digital Twin (SOVEREIGN.md): a continuously
updated model of services, agents, capabilities, dependencies, activity,
and health, supporting impact analysis before changes are made.

The twin merges two sources of truth:

- **Declared topology**: operators register services with their
  dependency edges and optional health URLs (POST /services). Every
  declaration is recorded on the ledger as TwinServiceDeclared.
- **Observed reality**: POST /sync reads ledger history (through the
  gateway, incremental cursor) and discovers agents and their
  capabilities from AgentRegistered events, plus per-source activity
  (event counts, last seen) for every service that actually emits events.

GET /impact/{node} answers "if this changes or fails, what is affected?"
by walking the reverse dependency graph transitively, including which
capabilities lose their only provider when an agent goes away.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("TWIN_DB_PATH", "twin.db")

FETCH_LIMIT = 1000
PROBE_TIMEOUT_SECONDS = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "5"))


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
            CREATE TABLE IF NOT EXISTS services (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                depends_on TEXT NOT NULL DEFAULT '[]',
                health_url TEXT,
                health TEXT NOT NULL DEFAULT 'unknown',
                last_probed TEXT,
                declared_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity (
                source TEXT PRIMARY KEY,
                event_count INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                endpoint TEXT,
                capabilities TEXT NOT NULL DEFAULT '[]',
                last_registered TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cursor_ts TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO state (id, cursor_ts) VALUES (1, '')")
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_append(
    event_type: str, payload: dict[str, Any], correlation_id: str
) -> None:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/events",
            json={
                "type": event_type,
                "source": "digital-twin",
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
            detail=f"gateway rejected {event_type}: {response.status_code} {response.text}",
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


class ServiceIn(BaseModel):
    name: str = Field(min_length=1)
    kind: str = "service"
    depends_on: list[str] = Field(default_factory=list)
    health_url: str | None = None


class ServiceOut(BaseModel):
    name: str
    kind: str
    depends_on: list[str]
    health_url: str | None
    health: str
    last_probed: str | None
    declared: bool
    event_count: int
    last_seen: str | None


class AgentOut(BaseModel):
    name: str
    endpoint: str | None
    capabilities: list[str]
    last_registered: str | None


class CapabilityOut(BaseModel):
    name: str
    providers: list[str]
    sole_provider: bool


class Model(BaseModel):
    services: list[ServiceOut]
    agents: list[AgentOut]
    capabilities: list[CapabilityOut]


class SyncResult(BaseModel):
    events_examined: int
    agents_seen: int
    sources_seen: int


class ProbeResult(BaseModel):
    probed: int
    healthy: int
    unhealthy: int


class Impact(BaseModel):
    node: str
    node_type: str
    affected_services: list[str]
    affected_capabilities: list[str]
    notes: list[str]


def _service_out(conn: sqlite3.Connection, row: sqlite3.Row) -> ServiceOut:
    activity = conn.execute(
        "SELECT * FROM activity WHERE source = ?", (row["name"],)
    ).fetchone()
    return ServiceOut(
        name=row["name"],
        kind=row["kind"],
        depends_on=json.loads(row["depends_on"]),
        health_url=row["health_url"],
        health=row["health"],
        last_probed=row["last_probed"],
        declared=row["declared_at"] is not None,
        event_count=activity["event_count"] if activity else 0,
        last_seen=activity["last_seen"] if activity else None,
    )


def _capabilities(conn: sqlite3.Connection) -> list[CapabilityOut]:
    providers: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name, capabilities FROM agents").fetchall():
        for capability in json.loads(row["capabilities"]):
            providers.setdefault(capability, []).append(row["name"])
    return [
        CapabilityOut(name=name, providers=sorted(names), sole_provider=len(names) == 1)
        for name, names in sorted(providers.items())
    ]


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Project Sovereign Digital Twin", version="0.1.0", lifespan=_lifespan
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/services", response_model=ServiceOut, status_code=201)
def declare_service(service: ServiceIn) -> ServiceOut:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO services (name, kind, depends_on, health_url, declared_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind = excluded.kind,
                depends_on = excluded.depends_on,
                health_url = excluded.health_url,
                declared_at = excluded.declared_at
            """,
            (
                service.name,
                service.kind,
                json.dumps(service.depends_on),
                service.health_url,
                _now(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM services WHERE name = ?", (service.name,)
        ).fetchone()
        out = _service_out(conn, row)
    finally:
        conn.close()

    _ledger_append(
        "TwinServiceDeclared",
        {
            "service": service.name,
            "kind": service.kind,
            "depends_on": service.depends_on,
        },
        correlation_id=service.name,
    )
    return out


@app.post("/sync", response_model=SyncResult)
def sync() -> SyncResult:
    conn = _connect()
    try:
        state = conn.execute("SELECT cursor_ts FROM state WHERE id = 1").fetchone()
        cursor_ts = state["cursor_ts"]
        events = _ledger_events(cursor_ts)

        newest_ts = cursor_ts
        for event in events:
            if cursor_ts and event["ts"] <= cursor_ts:
                continue
            newest_ts = max(newest_ts, event["ts"])

            source = event.get("source")
            if source:
                conn.execute(
                    """
                    INSERT INTO activity (source, event_count, last_seen)
                    VALUES (?, 1, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        event_count = event_count + 1,
                        last_seen = excluded.last_seen
                    """,
                    (source, event["ts"]),
                )
                # Sources that emit events but were never declared appear in
                # the model as observed services with no dependency edges --
                # the twin reports them rather than inventing topology.
                conn.execute(
                    "INSERT OR IGNORE INTO services (name, kind) VALUES (?, 'observed')",
                    (source,),
                )

            if event["type"] == "AgentRegistered":
                payload = event["payload"]
                conn.execute(
                    """
                    INSERT INTO agents (name, endpoint, capabilities, last_registered)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        endpoint = excluded.endpoint,
                        capabilities = excluded.capabilities,
                        last_registered = excluded.last_registered
                    """,
                    (
                        payload.get("agent", "?"),
                        payload.get("endpoint"),
                        json.dumps(payload.get("capabilities", [])),
                        event["ts"],
                    ),
                )

        conn.execute("UPDATE state SET cursor_ts = ? WHERE id = 1", (newest_ts,))
        conn.commit()

        agents_count = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
        sources_count = conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()[
            "n"
        ]
        return SyncResult(
            events_examined=len(events),
            agents_seen=agents_count,
            sources_seen=sources_count,
        )
    finally:
        conn.close()


@app.get("/model", response_model=Model)
def model() -> Model:
    conn = _connect()
    try:
        service_rows = conn.execute(
            "SELECT * FROM services ORDER BY name ASC"
        ).fetchall()
        agent_rows = conn.execute("SELECT * FROM agents ORDER BY name ASC").fetchall()
        return Model(
            services=[_service_out(conn, row) for row in service_rows],
            agents=[
                AgentOut(
                    name=row["name"],
                    endpoint=row["endpoint"],
                    capabilities=json.loads(row["capabilities"]),
                    last_registered=row["last_registered"],
                )
                for row in agent_rows
            ],
            capabilities=_capabilities(conn),
        )
    finally:
        conn.close()


@app.post("/probe", response_model=ProbeResult)
def probe() -> ProbeResult:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, health_url FROM services WHERE health_url IS NOT NULL"
        ).fetchall()
        healthy = 0
        unhealthy = 0
        for row in rows:
            try:
                response = httpx.get(row["health_url"], timeout=PROBE_TIMEOUT_SECONDS)
                status = "healthy" if response.status_code == 200 else "unhealthy"
            except httpx.RequestError:
                status = "unhealthy"
            if status == "healthy":
                healthy += 1
            else:
                unhealthy += 1
            conn.execute(
                "UPDATE services SET health = ?, last_probed = ? WHERE name = ?",
                (status, _now(), row["name"]),
            )
        conn.commit()
        return ProbeResult(probed=len(rows), healthy=healthy, unhealthy=unhealthy)
    finally:
        conn.close()


@app.get("/impact/{node}", response_model=Impact)
def impact(node: str) -> Impact:
    conn = _connect()
    try:
        service = conn.execute(
            "SELECT * FROM services WHERE name = ?", (node,)
        ).fetchone()
        agent = conn.execute("SELECT * FROM agents WHERE name = ?", (node,)).fetchone()
        capabilities = {c.name: c for c in _capabilities(conn)}

        if service is not None:
            # Reverse dependency closure: who (transitively) depends on node?
            dependents: dict[str, list[str]] = {}
            for row in conn.execute("SELECT name, depends_on FROM services").fetchall():
                for dep in json.loads(row["depends_on"]):
                    dependents.setdefault(dep, []).append(row["name"])

            affected: list[str] = []
            seen = {node}
            queue = deque([node])
            while queue:
                current = queue.popleft()
                for dependent in dependents.get(current, []):
                    if dependent not in seen:
                        seen.add(dependent)
                        affected.append(dependent)
                        queue.append(dependent)

            notes = []
            if not affected:
                notes.append("No declared service depends on this node.")
            return Impact(
                node=node,
                node_type="service",
                affected_services=sorted(affected),
                affected_capabilities=[],
                notes=notes,
            )

        if agent is not None:
            agent_capabilities = json.loads(agent["capabilities"])
            at_risk = [
                name
                for name in agent_capabilities
                if name in capabilities and capabilities[name].sole_provider
            ]
            notes = [
                f"Capability '{name}' would have no remaining provider."
                for name in at_risk
            ]
            return Impact(
                node=node,
                node_type="agent",
                affected_services=[],
                affected_capabilities=agent_capabilities,
                notes=notes,
            )

        if node in capabilities:
            providers = capabilities[node].providers
            return Impact(
                node=node,
                node_type="capability",
                affected_services=[],
                affected_capabilities=[node],
                notes=[f"Provided by: {', '.join(providers)}"],
            )

        raise HTTPException(status_code=404, detail=f"unknown node: {node}")
    finally:
        conn.close()
