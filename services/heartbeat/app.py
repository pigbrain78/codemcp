"""Heartbeat -- the platform's pulse.

Turns the individually-passive services into one running organism by
periodically driving every maintenance endpoint:

- knowledge  POST /sync      (index new captures)
- twin       POST /sync      (update the structural model)
- twin       POST /probe     (health-check declared services)
- ghost      POST /observe   (run the advisory council)
- relay      POST /deliver   (fan events out to n8n and friends)

and, every REFLECT_EVERY_TICKS ticks (default: daily at 5-minute ticks),
the Evolution Engine's POST /reflect.

A failed call is recorded and retried on the next tick -- that retry *is*
the scheduler's semantic, so the loop never dies because one service is
down. GET /status shows per-target run counts, failures, and the last
error detail, so the pulse itself is observable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "300"))
REFLECT_EVERY_TICKS = int(os.environ.get("REFLECT_EVERY_TICKS", "288"))
CALL_TIMEOUT_SECONDS = float(os.environ.get("CALL_TIMEOUT_SECONDS", "60"))


def _targets() -> list[dict[str, str]]:
    env = os.environ
    knowledge = env.get("KNOWLEDGE_URL", "http://localhost:8003")
    twin = env.get("TWIN_URL", "http://localhost:8009")
    ghost = env.get("GHOST_URL", "http://localhost:8008")
    relay = env.get("RELAY_URL", "http://localhost:8006")
    evolution = env.get("EVOLUTION_URL", "http://localhost:8007")
    return [
        {"name": "knowledge-sync", "url": f"{knowledge}/sync", "cadence": "tick"},
        {"name": "twin-sync", "url": f"{twin}/sync", "cadence": "tick"},
        {"name": "twin-probe", "url": f"{twin}/probe", "cadence": "tick"},
        {"name": "ghost-observe", "url": f"{ghost}/observe", "cadence": "tick"},
        {"name": "relay-deliver", "url": f"{relay}/deliver", "cadence": "tick"},
        {
            "name": "evolution-reflect",
            "url": f"{evolution}/reflect",
            "cadence": "reflect",
        },
    ]


class TargetStatus(BaseModel):
    name: str
    url: str
    cadence: str
    runs: int = 0
    failures: int = 0
    last_run: str | None = None
    last_status: str = "never"
    last_detail: str = ""


class Status(BaseModel):
    ticks: int
    tick_seconds: float
    reflect_every_ticks: int
    targets: list[TargetStatus]


class _State:
    def __init__(self) -> None:
        self.ticks = 0
        self.targets = {t["name"]: TargetStatus(**t) for t in _targets()}


state = _State()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _call(client: httpx.AsyncClient, target: TargetStatus) -> None:
    target.runs += 1
    target.last_run = _now()
    try:
        response = await client.post(target.url, timeout=CALL_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        target.failures += 1
        target.last_status = "error"
        target.last_detail = f"unreachable: {exc}"
        return
    if 200 <= response.status_code < 300:
        target.last_status = "ok"
        target.last_detail = response.text[:500]
    else:
        target.failures += 1
        target.last_status = "error"
        target.last_detail = f"{response.status_code}: {response.text[:500]}"


async def _beat_forever() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            state.ticks += 1
            for target in state.targets.values():
                if target.cadence == "tick" or (
                    target.cadence == "reflect"
                    and state.ticks % REFLECT_EVERY_TICKS == 0
                ):
                    await _call(client, target)
            await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_beat_forever())
    yield
    task.cancel()


app = FastAPI(title="Project Sovereign Heartbeat", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=Status)
def status() -> Status:
    return Status(
        ticks=state.ticks,
        tick_seconds=TICK_SECONDS,
        reflect_every_ticks=REFLECT_EVERY_TICKS,
        targets=list(state.targets.values()),
    )
