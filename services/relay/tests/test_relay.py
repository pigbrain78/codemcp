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
        "source": "capture",
        "payload": {"n": 1},
        "ts": "2026-01-01T00:00:00+00:00",
    },
    {
        "id": "e2",
        "seq": 2,
        "type": "AgentCompleted",
        "source": "manus-prime",
        "payload": {"n": 2},
        "ts": "2026-01-01T00:01:00+00:00",
    },
    {
        "id": "e3",
        "seq": 3,
        "type": "CaptureCreated",
        "source": "capture",
        "payload": {"n": 3},
        "ts": "2026-01-01T00:02:00+00:00",
    },
]


def _respond(handler, status, obj):
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if self.headers.get("Authorization") != f"Bearer {self.server.expected_key}":
            _respond(self, 401, {"error": "unauthorized"})
            return
        if parts.path != "/events":
            _respond(self, 404, {"error": "not_found"})
            return
        query = parse_qs(parts.query)
        since = query.get("since", [None])[0]
        events = [e for e in self.server.events if since is None or e["ts"] >= since]
        _respond(self, 200, events)


class _FakeSubscriberHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.server.received.append(body)
        if self.server.mode == "ok":
            _respond(self, 200, {"ok": True})
        else:
            _respond(self, 500, {"error": "workflow exploded"})


def _start(handler_cls, **attrs):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    for key, value in attrs.items():
        setattr(server, key, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def fake_gateway():
    server, thread, url = _start(
        _FakeGatewayHandler, events=list(EVENTS), expected_key="relay-key"
    )
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def fake_subscriber():
    server, thread, url = _start(_FakeSubscriberHandler, received=[], mode="ok")
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.relay import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    gateway_server, gateway_url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("GATEWAY_API_KEY", gateway_server.expected_key)
    monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "relay.db"))

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, gateway_server


def test_health(client):
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_subscription_crud(client):
    test_client, _ = client
    created = test_client.post(
        "/subscriptions",
        json={"url": "http://n8n/webhook/x", "event_types": ["CaptureCreated"]},
    )
    assert created.status_code == 201
    sub = created.json()
    assert sub["event_types"] == ["CaptureCreated"]
    assert sub["cursor_ts"] == ""

    listing = test_client.get("/subscriptions").json()
    assert [s["id"] for s in listing] == [sub["id"]]

    assert test_client.delete(f"/subscriptions/{sub['id']}").status_code == 204
    assert test_client.get("/subscriptions").json() == []
    assert test_client.delete(f"/subscriptions/{sub['id']}").status_code == 404


def test_delivers_all_events_without_filter(client, fake_subscriber):
    test_client, _ = client
    subscriber_server, subscriber_url = fake_subscriber
    test_client.post("/subscriptions", json={"url": subscriber_url})

    result = test_client.post("/deliver").json()
    assert result == {"subscriptions": 1, "delivered": 3, "failed": 0}
    assert [e["id"] for e in subscriber_server.received] == ["e1", "e2", "e3"]


def test_event_type_filter(client, fake_subscriber):
    test_client, _ = client
    subscriber_server, subscriber_url = fake_subscriber
    test_client.post(
        "/subscriptions",
        json={"url": subscriber_url, "event_types": ["CaptureCreated"]},
    )

    result = test_client.post("/deliver").json()
    assert result["delivered"] == 2
    assert [e["id"] for e in subscriber_server.received] == ["e1", "e3"]


def test_deliver_is_incremental(client, fake_subscriber):
    test_client, gateway_server = client
    subscriber_server, subscriber_url = fake_subscriber
    test_client.post("/subscriptions", json={"url": subscriber_url})

    first = test_client.post("/deliver").json()
    assert first["delivered"] == 3

    second = test_client.post("/deliver").json()
    assert second["delivered"] == 0
    assert len(subscriber_server.received) == 3

    gateway_server.events.append(
        {
            "id": "e4",
            "seq": 4,
            "type": "FailureDetected",
            "source": "x",
            "payload": {},
            "ts": "2026-01-01T00:03:00+00:00",
        }
    )
    third = test_client.post("/deliver").json()
    assert third["delivered"] == 1
    assert subscriber_server.received[-1]["id"] == "e4"


def test_failed_delivery_stops_batch_and_retries(client, fake_subscriber):
    test_client, _ = client
    subscriber_server, subscriber_url = fake_subscriber
    sub = test_client.post("/subscriptions", json={"url": subscriber_url}).json()

    subscriber_server.mode = "fail"
    result = test_client.post("/deliver").json()
    assert result["delivered"] == 0
    assert result["failed"] == 1  # stopped at the first event

    deliveries = test_client.get(
        "/deliveries", params={"subscription_id": sub["id"]}
    ).json()
    assert deliveries[0]["status"] == "failed"
    assert deliveries[0]["response_code"] == 500

    # Subscriber recovers; the same events deliver in order from the start.
    subscriber_server.mode = "ok"
    retry = test_client.post("/deliver").json()
    assert retry["delivered"] == 3
    assert [e["id"] for e in subscriber_server.received[-3:]] == ["e1", "e2", "e3"]


def test_independent_cursors_per_subscription(client, fake_subscriber):
    test_client, _ = client
    subscriber_server, subscriber_url = fake_subscriber
    test_client.post("/subscriptions", json={"url": subscriber_url})
    test_client.post("/deliver")

    # A new subscriber starts from the beginning of the ledger.
    test_client.post("/subscriptions", json={"url": subscriber_url})
    result = test_client.post("/deliver").json()
    assert result["delivered"] == 3
    assert len(subscriber_server.received) == 6


def test_unreachable_subscriber_is_recorded(client):
    test_client, _ = client
    sub = test_client.post(
        "/subscriptions", json={"url": "http://127.0.0.1:1/webhook"}
    ).json()
    result = test_client.post("/deliver").json()
    assert result["failed"] == 1
    deliveries = test_client.get(
        "/deliveries", params={"subscription_id": sub["id"]}
    ).json()
    assert deliveries[0]["status"] == "failed"
    assert "unreachable" in deliveries[0]["error"]


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "relay.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        test_client.post("/subscriptions", json={"url": "http://x"})
        response = test_client.post("/deliver")
    assert response.status_code == 502
