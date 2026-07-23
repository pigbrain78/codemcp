import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)
        self.server.received.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": payload,
            }
        )

        if self.headers.get("Authorization") != f"Bearer {self.server.expected_key}":
            self._respond(401, {"error": "unauthorized"})
            return

        if self.path == "/events":
            self.server.seq += 1
            self._respond(
                201, {"id": f"evt-{self.server.seq}", "seq": self.server.seq, **payload}
            )
            return

        self._respond(404, {"error": "not_found"})

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
    server.received = []
    server.seq = 0
    server.expected_key = "capture-api-key"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.capture import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch):
    server, url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", url)
    monkeypatch.setenv("GATEWAY_API_KEY", server.expected_key)

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, server


def test_health(client):
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_text_capture_no_project(client):
    test_client, server = client
    response = test_client.post(
        "/captures", json={"type": "text", "content": "buy milk"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["capture_type"] == "text"
    assert body["project"] is None
    assert body["tags"] == []
    assert body["seq"] == 1

    assert len(server.received) == 1
    forwarded = server.received[0]["body"]
    assert forwarded["type"] == "CaptureCreated"
    assert forwarded["source"] == "pocket-os-capture-api"
    assert forwarded["payload"]["content"] == "buy milk"
    assert forwarded["payload"]["capture_type"] == "text"


def test_hashtag_project_detection(client):
    test_client, _ = client
    response = test_client.post(
        "/captures",
        json={
            "type": "voice_note",
            "content": "ideas for #agriforge irrigation timing",
        },
    )
    body = response.json()
    assert body["project"] == "agriforge"
    assert body["tags"] == ["agriforge"]


def test_project_hint_overrides_hashtag(client):
    test_client, _ = client
    response = test_client.post(
        "/captures",
        json={
            "type": "task",
            "content": "follow up with #dreamweaver team",
            "project_hint": "kid-os",
        },
    )
    body = response.json()
    assert body["project"] == "kid-os"
    assert body["tags"] == ["dreamweaver"]


def test_metadata_is_forwarded(client):
    test_client, server = client
    response = test_client.post(
        "/captures",
        json={
            "type": "photo",
            "content": "whiteboard sketch",
            "metadata": {"url": "s3://x.jpg"},
        },
    )
    assert response.status_code == 201
    forwarded = server.received[0]["body"]
    assert forwarded["payload"]["metadata"] == {"url": "s3://x.jpg"}


def test_invalid_capture_type_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/captures", json={"type": "carrier_pigeon", "content": "x"}
    )
    assert response.status_code == 422


def test_gateway_rejection_bubbles_up_as_502(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "wrong-key")
    app_module = _reload_app()
    with TestClient(app_module.app) as bad_client:
        response = bad_client.post("/captures", json={"type": "text", "content": "hi"})
    assert response.status_code == 502


def test_gateway_unreachable_returns_502(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        response = test_client.post("/captures", json={"type": "text", "content": "hi"})
    assert response.status_code == 502
