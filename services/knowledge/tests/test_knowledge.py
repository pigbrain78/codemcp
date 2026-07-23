import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

EVENTS = [
    {
        "id": "e1",
        "seq": 1,
        "type": "CaptureCreated",
        "source": "pocket-os-capture-api",
        "payload": {
            "capture_type": "text",
            "content": "buy milk for the team",
            "project": None,
            "tags": [],
            "metadata": {},
        },
        "ts": "2026-01-01T00:00:00+00:00",
    },
    {
        "id": "e2",
        "seq": 2,
        "type": "CaptureCreated",
        "source": "pocket-os-capture-api",
        "payload": {
            "capture_type": "voice_note",
            "content": "ideas for irrigation timing",
            "project": "agriforge",
            "tags": ["agriforge", "irrigation"],
            "metadata": {},
        },
        "ts": "2026-01-01T00:01:00+00:00",
    },
    {
        "id": "e3",
        "seq": 3,
        "type": "GatewayRequestHandled",
        "source": "gateway",
        "payload": {
            "method": "POST",
            "path": "/events",
            "actor": "x",
            "role": "service",
            "status": 201,
        },
        "ts": "2026-01-01T00:02:00+00:00",
    },
    {
        "id": "e4",
        "seq": 4,
        "type": "CaptureCreated",
        "source": "pocket-os-capture-api",
        "payload": {
            "capture_type": "task",
            "content": "follow up on agriforge irrigation sensors",
            "project": "agriforge",
            "tags": ["agriforge"],
            "metadata": {},
        },
        "ts": "2026-01-01T00:03:00+00:00",
    },
]


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if self.headers.get("Authorization") != f"Bearer {self.server.expected_key}":
            self._respond(401, {"error": "unauthorized"})
            return

        if parts.path != "/events":
            self._respond(404, {"error": "not_found"})
            return

        query = parse_qs(parts.query)
        since = query.get("since", [None])[0]
        limit = int(query.get("limit", ["100"])[0])
        events = [e for e in self.server.events if since is None or e["ts"] >= since][
            :limit
        ]
        self._respond(200, events)

    def _respond(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    server.events = EVENTS
    server.expected_key = "knowledge-api-key"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.knowledge import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    server, url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", url)
    monkeypatch.setenv("GATEWAY_API_KEY", server.expected_key)
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(tmp_path / "knowledge.db"))

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, server


def test_health(client):
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sync_indexes_only_capture_events(client):
    test_client, _ = client
    response = test_client.post("/sync")
    assert response.status_code == 200
    body = response.json()
    assert body["fetched"] == 4
    assert body["indexed"] == 3  # e1, e2, e4 -- e3 is not a CaptureCreated event
    assert body["last_ts"] == EVENTS[-1]["ts"]


def test_sync_is_idempotent_and_incremental(client):
    test_client, server = client
    first = test_client.post("/sync").json()
    assert first["indexed"] == 3

    # Re-syncing asks the gateway for events since the last sync's cursor,
    # so the fake gateway should only hand back the boundary event again,
    # and nothing new should be indexed.
    second = test_client.post("/sync").json()
    assert second["indexed"] == 0
    assert second["fetched"] >= 1  # the boundary event (ts >= last_ts)


def test_search_full_text(client):
    test_client, _ = client
    test_client.post("/sync")

    response = test_client.get("/search", params={"q": "irrigation"})
    assert response.status_code == 200
    body = response.json()
    assert [c["event_id"] for c in body] == ["e4", "e2"]


def test_search_by_project(client):
    test_client, _ = client
    test_client.post("/sync")

    response = test_client.get("/search", params={"project": "agriforge"})
    body = response.json()
    assert {c["event_id"] for c in body} == {"e2", "e4"}


def test_search_by_tag(client):
    test_client, _ = client
    test_client.post("/sync")

    response = test_client.get("/search", params={"tag": "irrigation"})
    body = response.json()
    assert [c["event_id"] for c in body] == ["e2"]
    assert body[0]["tags"] == ["agriforge", "irrigation"]


def test_projects_listing_and_lookup(client):
    test_client, _ = client
    test_client.post("/sync")

    listing = test_client.get("/projects").json()
    assert listing == [{"project": "agriforge", "capture_count": 2}]

    detail = test_client.get("/projects/agriforge")
    assert detail.status_code == 200
    assert [c["event_id"] for c in detail.json()] == ["e4", "e2"]

    missing = test_client.get("/projects/does-not-exist")
    assert missing.status_code == 404


def test_tags_listing_and_lookup(client):
    test_client, _ = client
    test_client.post("/sync")

    listing = test_client.get("/tags").json()
    assert listing == [
        {"tag": "agriforge", "capture_count": 2},
        {"tag": "irrigation", "capture_count": 1},
    ]

    detail = test_client.get("/tags/agriforge")
    assert [c["event_id"] for c in detail.json()] == ["e4", "e2"]

    missing = test_client.get("/tags/does-not-exist")
    assert missing.status_code == 404


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(tmp_path / "knowledge.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        response = test_client.post("/sync")
    assert response.status_code == 502


def test_gateway_auth_rejection_returns_502(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "wrong-key")
    app_module = _reload_app()
    with TestClient(app_module.app) as bad_client:
        response = bad_client.post("/sync")
    assert response.status_code == 502
