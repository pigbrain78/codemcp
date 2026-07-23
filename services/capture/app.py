"""Pocket OS capture API.

The backend Pocket OS calls to capture a thought, voice note, photo,
screenshot, document, or quick task. Pocket OS holds no authoritative data
itself: this service classifies the capture, does light project detection,
and hands it to the API Gateway to be written to the ledger. It never talks
to the ledger directly -- the gateway is the only thing allowed to do that.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")

CaptureKind = Literal["text", "voice_note", "photo", "screenshot", "document", "task"]

HASHTAG_RE = re.compile(r"#(\w[\w-]*)")


class CaptureIn(BaseModel):
    type: CaptureKind
    content: str = Field(min_length=1)
    project_hint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptureOut(BaseModel):
    event_id: str
    seq: int
    capture_type: CaptureKind
    project: str | None
    tags: list[str]


def classify(content: str, project_hint: str | None) -> dict[str, Any]:
    """Lightweight, deterministic stand-in for the doc's Classification and
    Project Detection steps: pull #hashtags out of the content, and treat an
    explicit project_hint (or the first hashtag) as the detected project.
    """
    tags = HASHTAG_RE.findall(content)
    project = project_hint or (tags[0] if tags else None)
    return {"project": project, "tags": tags}


app = FastAPI(title="Pocket OS Capture API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/captures", response_model=CaptureOut, status_code=201)
def create_capture(capture: CaptureIn) -> CaptureOut:
    classification = classify(capture.content, capture.project_hint)

    event = {
        "type": "CaptureCreated",
        "source": "pocket-os-capture-api",
        "payload": {
            "capture_type": capture.type,
            "content": capture.content,
            "project": classification["project"],
            "tags": classification["tags"],
            "metadata": capture.metadata,
        },
    }

    try:
        response = httpx.post(
            f"{GATEWAY_URL}/events",
            json=event,
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
            detail=f"gateway rejected capture event: {response.status_code} {response.text}",
        )

    stored = response.json()
    return CaptureOut(
        event_id=stored["id"],
        seq=stored["seq"],
        capture_type=capture.type,
        project=classification["project"],
        tags=classification["tags"],
    )
