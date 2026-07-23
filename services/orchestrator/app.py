"""Manus Prime -- the orchestration layer.

The "Orchestration Layer" from the Project Sovereign architecture: agents
(Dr. Quinn, Igor OS, Ralph5, ...) register here as HTTP endpoints with a
declared capability list, and work is dispatched to them through a single
front door. Every dispatch is recorded on the ledger -- through the
gateway, never directly -- as AgentRequested followed by AgentCompleted or
AgentFailed, correlated by task id, so agent activity is fully auditable.

Agent contract: an agent is any HTTP service exposing POST <endpoint>
accepting {"task_id": str, "capability": str, "input": object} and
returning 200 with {"output": object}. Anything else (non-200, network
failure) is a failed task, recorded as such.
"""

from __future__ import annotations

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
from pydantic import BaseModel, Field

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("ORCHESTRATOR_DB_PATH", "orchestrator.db")

AGENT_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TIMEOUT_SECONDS", "60"))


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
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                registered_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                capability TEXT NOT NULL,
                agent TEXT NOT NULL,
                status TEXT NOT NULL,
                input TEXT NOT NULL,
                output TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_append(
    event_type: str, payload: dict[str, Any], correlation_id: str
) -> None:
    """Record an orchestration event on the ledger via the gateway. A ledger
    that can't be written to is a governance failure, not a soft error --
    let it surface."""
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/events",
            json={
                "type": event_type,
                "source": "manus-prime",
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


class AgentRegistration(BaseModel):
    name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)


class AgentInfo(BaseModel):
    name: str
    endpoint: str
    capabilities: list[str]
    registered_at: str


class TaskIn(BaseModel):
    capability: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None


class TaskOut(BaseModel):
    id: str
    capability: str
    agent: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    created_at: str
    finished_at: str | None


def _row_to_task(row: sqlite3.Row) -> TaskOut:
    return TaskOut(
        id=row["id"],
        capability=row["capability"],
        agent=row["agent"],
        status=row["status"],
        input=json.loads(row["input"]),
        output=json.loads(row["output"]) if row["output"] else None,
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _row_to_agent(row: sqlite3.Row) -> AgentInfo:
    return AgentInfo(
        name=row["name"],
        endpoint=row["endpoint"],
        capabilities=json.loads(row["capabilities"]),
        registered_at=row["registered_at"],
    )


def _select_agent(
    conn: sqlite3.Connection, capability: str, agent_name: str | None
) -> AgentInfo:
    if agent_name is not None:
        row = conn.execute(
            "SELECT * FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"agent not found: {agent_name}"
            )
        agent = _row_to_agent(row)
        if capability not in agent.capabilities:
            raise HTTPException(
                status_code=409,
                detail=f"agent {agent_name} does not declare capability {capability}",
            )
        return agent

    rows = conn.execute("SELECT * FROM agents ORDER BY registered_at ASC").fetchall()
    for row in rows:
        agent = _row_to_agent(row)
        if capability in agent.capabilities:
            return agent
    raise HTTPException(
        status_code=404, detail=f"no agent registered for capability: {capability}"
    )


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Manus Prime Orchestrator", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agents", response_model=AgentInfo, status_code=201)
def register_agent(registration: AgentRegistration) -> AgentInfo:
    registered_at = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO agents (name, endpoint, capabilities, registered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                endpoint = excluded.endpoint,
                capabilities = excluded.capabilities,
                registered_at = excluded.registered_at
            """,
            (
                registration.name,
                registration.endpoint,
                json.dumps(registration.capabilities),
                registered_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _ledger_append(
        "AgentRegistered",
        {
            "agent": registration.name,
            "endpoint": registration.endpoint,
            "capabilities": registration.capabilities,
        },
        correlation_id=registration.name,
    )
    return AgentInfo(
        name=registration.name,
        endpoint=registration.endpoint,
        capabilities=registration.capabilities,
        registered_at=registered_at,
    )


@app.get("/agents", response_model=list[AgentInfo])
def list_agents() -> list[AgentInfo]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM agents ORDER BY name ASC").fetchall()
        return [_row_to_agent(row) for row in rows]
    finally:
        conn.close()


@app.post("/tasks", response_model=TaskOut, status_code=201)
def dispatch_task(task: TaskIn) -> TaskOut:
    task_id = str(uuid.uuid4())
    created_at = _now()

    conn = _connect()
    try:
        agent = _select_agent(conn, task.capability, task.agent)

        conn.execute(
            """
            INSERT INTO tasks (id, capability, agent, status, input, created_at)
            VALUES (?, ?, ?, 'dispatched', ?, ?)
            """,
            (task_id, task.capability, agent.name, json.dumps(task.input), created_at),
        )
        conn.commit()
    finally:
        conn.close()

    _ledger_append(
        "AgentRequested",
        {"agent": agent.name, "capability": task.capability, "input": task.input},
        correlation_id=task_id,
    )

    status = "completed"
    output: dict[str, Any] | None = None
    error: str | None = None
    try:
        response = httpx.post(
            agent.endpoint,
            json={
                "task_id": task_id,
                "capability": task.capability,
                "input": task.input,
            },
            timeout=AGENT_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            output = response.json().get("output")
        else:
            status = "failed"
            error = f"agent returned {response.status_code}: {response.text}"
    except httpx.RequestError as exc:
        status = "failed"
        error = f"agent unreachable: {exc}"

    finished_at = _now()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, output = ?, error = ?, finished_at = ? WHERE id = ?",
            (
                status,
                json.dumps(output) if output is not None else None,
                error,
                finished_at,
                task_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()

    if status == "completed":
        _ledger_append(
            "AgentCompleted",
            {"agent": agent.name, "capability": task.capability, "output": output},
            correlation_id=task_id,
        )
    else:
        _ledger_append(
            "AgentFailed",
            {"agent": agent.name, "capability": task.capability, "error": error},
            correlation_id=task_id,
        )

    return _row_to_task(row)


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(status: str | None = None) -> list[TaskOut]:
    conn = _connect()
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_task(row) for row in rows]
    finally:
        conn.close()


@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str) -> TaskOut:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _row_to_task(row)
    finally:
        conn.close()
