"""Event relay -- the Enterprise Event Bus.

Fans ledger events out to webhook subscribers (n8n workflows, Zapier,
custom services). A subscriber registers a URL and an optional event-type
filter; each POST /deliver run reads new events from the ledger (through
the gateway, read-only) and POSTs them to every matching subscription in
order, advancing a per-subscription cursor only on success so a failed
delivery is retried on the next run instead of being silently dropped.

This keeps the platform loosely coupled: services only ever append to the
ledger; anything that wants to *react* (n8n automation, dashboards,
notifications) subscribes here rather than being called directly.
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
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("RELAY_DB_PATH", "relay.db")

FETCH_LIMIT = 1000
DELIVERY_TIMEOUT_SECONDS = float(os.environ.get("DELIVERY_TIMEOUT_SECONDS", "15"))


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
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                event_types TEXT NOT NULL,
                cursor_ts TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                response_code INTEGER,
                error TEXT,
                attempted_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gateway_events(since: str) -> list[dict[str, Any]]:
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


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=1)
    event_types: list[str] = Field(default_factory=list)


class SubscriptionOut(BaseModel):
    id: str
    url: str
    event_types: list[str]
    cursor_ts: str
    created_at: str


class DeliveryRecord(BaseModel):
    subscription_id: str
    event_id: str
    event_type: str
    status: str
    response_code: int | None
    error: str | None
    attempted_at: str


class DeliverResult(BaseModel):
    subscriptions: int
    delivered: int
    failed: int


def _row_to_subscription(row: sqlite3.Row) -> SubscriptionOut:
    return SubscriptionOut(
        id=row["id"],
        url=row["url"],
        event_types=json.loads(row["event_types"]),
        cursor_ts=row["cursor_ts"],
        created_at=row["created_at"],
    )


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Project Sovereign Event Relay", version="0.1.0", lifespan=_lifespan
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/subscriptions", response_model=SubscriptionOut, status_code=201)
def create_subscription(subscription: SubscriptionIn) -> SubscriptionOut:
    record = SubscriptionOut(
        id=str(uuid.uuid4()),
        url=subscription.url,
        event_types=subscription.event_types,
        cursor_ts="",
        created_at=_now(),
    )
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions (id, url, event_types, cursor_ts, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.url,
                json.dumps(record.event_types),
                record.cursor_ts,
                record.created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return record


@app.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions() -> list[SubscriptionOut]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_subscription(row) for row in rows]
    finally:
        conn.close()


@app.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str) -> Response:
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="subscription not found")
        return Response(status_code=204)
    finally:
        conn.close()


def _deliver_one(
    url: str, event: dict[str, Any]
) -> tuple[bool, int | None, str | None]:
    try:
        response = httpx.post(url, json=event, timeout=DELIVERY_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        return False, None, f"unreachable: {exc}"
    if 200 <= response.status_code < 300:
        return True, response.status_code, None
    return False, response.status_code, f"subscriber returned {response.status_code}"


@app.post("/deliver", response_model=DeliverResult)
def deliver() -> DeliverResult:
    conn = _connect()
    try:
        subscriptions = [
            _row_to_subscription(row)
            for row in conn.execute("SELECT * FROM subscriptions").fetchall()
        ]

        delivered = 0
        failed = 0
        for subscription in subscriptions:
            events = _gateway_events(subscription.cursor_ts)
            cursor_ts = subscription.cursor_ts
            for event in events:
                # The gateway's `since` filter is inclusive, so the boundary
                # event comes back again on the next run; skip anything at or
                # before the cursor.
                if cursor_ts and event["ts"] <= cursor_ts:
                    continue
                if (
                    subscription.event_types
                    and event["type"] not in subscription.event_types
                ):
                    cursor_ts = event["ts"]
                    continue

                ok, code, error = _deliver_one(subscription.url, event)
                conn.execute(
                    """
                    INSERT INTO deliveries
                        (subscription_id, event_id, event_type, status, response_code, error, attempted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subscription.id,
                        event["id"],
                        event["type"],
                        "delivered" if ok else "failed",
                        code,
                        error,
                        _now(),
                    ),
                )
                if not ok:
                    failed += 1
                    # Stop this subscription's batch so ordering holds and the
                    # failed event is retried next run.
                    break
                delivered += 1
                cursor_ts = event["ts"]

            conn.execute(
                "UPDATE subscriptions SET cursor_ts = ? WHERE id = ?",
                (cursor_ts, subscription.id),
            )
            conn.commit()

        return DeliverResult(
            subscriptions=len(subscriptions), delivered=delivered, failed=failed
        )
    finally:
        conn.close()


@app.get("/deliveries", response_model=list[DeliveryRecord])
def list_deliveries(subscription_id: str | None = None) -> list[DeliveryRecord]:
    conn = _connect()
    try:
        if subscription_id is not None:
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE subscription_id = ? ORDER BY id DESC",
                (subscription_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()
        return [
            DeliveryRecord(
                subscription_id=row["subscription_id"],
                event_id=row["event_id"],
                event_type=row["event_type"],
                status=row["status"],
                response_code=row["response_code"],
                error=row["error"],
                attempted_at=row["attempted_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()
