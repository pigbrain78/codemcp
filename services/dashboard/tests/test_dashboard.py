import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient


def _respond(handler, status, obj):
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _FakeBackendHandler(BaseHTTPRequestHandler):
    """One server standing in for gateway, twin, ghost, evolution, heartbeat."""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.requests.append(
            {"path": path, "auth": self.headers.get("Authorization")}
        )
        routes = self.server.routes
        if path in routes:
            _respond(self, 200, routes[path])
        else:
            _respond(self, 404, {"error": "not_found"})


ROUTES = {
    "/events": [
        {
            "id": f"e{i}",
            "seq": i,
            "type": "CaptureCreated",
            "source": "capture",
            "ts": f"t{i}",
        }
        for i in range(1, 31)
    ],
    "/verify": {"valid": True, "count": 30, "broken_at_seq": None},
    "/model": {
        "services": [
            {
                "name": "ledger",
                "kind": "service",
                "depends_on": [],
                "health": "healthy",
                "health_url": None,
                "last_probed": None,
                "declared": True,
                "event_count": 30,
                "last_seen": "t30",
            }
        ],
        "agents": [
            {
                "name": "kid",
                "endpoint": "http://kid",
                "capabilities": ["review"],
                "last_registered": "t3",
            }
        ],
        "capabilities": [
            {"name": "review", "providers": ["kid"], "sole_provider": True}
        ],
    },
    "/observations": [
        {
            "id": "o1",
            "observer": "dr-quinn",
            "kind": "recurring_failure",
            "summary": "seen before",
            "recommendation": "fix it",
            "evidence": ["h1"],
            "severity": "warning",
            "occurrences": 2,
            "status": "open",
            "first_seen": "t1",
            "last_seen": "t2",
        }
    ],
    "/proposals": [
        {
            "id": "p1",
            "reflection_id": "r1",
            "kind": "gene_candidate",
            "summary": "extract pattern",
            "evidence": ["h2"],
            "predicted_impact": "reuse",
            "confidence": "medium",
            "rollback": "n/a",
            "affected_systems": [],
            "status": "proposed",
            "created_at": "t9",
        }
    ],
    "/genes": [
        {
            "id": "g1",
            "name": "pattern:cursor-sync",
            "rationale": "r",
            "origin_record_id": None,
            "retired": False,
            "created_at": "t1",
            "reuse_count": 3,
            "successes": 2,
            "failures": 1,
            "reputation": 0.667,
        }
    ],
    "/status": {
        "ticks": 12,
        "tick_seconds": 300.0,
        "reflect_every_ticks": 288,
        "targets": [
            {
                "name": "knowledge-sync",
                "url": "http://k/sync",
                "cadence": "tick",
                "runs": 12,
                "failures": 0,
                "last_run": "t",
                "last_status": "ok",
                "last_detail": "",
            }
        ],
    },
}


@pytest.fixture
def fake_backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBackendHandler)
    server.routes = dict(ROUTES)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.dashboard import app as app_module

    importlib.reload(app_module)
    return app_module


def _client_with(monkeypatch, urls):
    for key, value in urls.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GATEWAY_API_KEY", "dash-key")
    app_module = _reload_app()
    return TestClient(app_module.app)


@pytest.fixture
def client(fake_backend, monkeypatch):
    _, url = fake_backend
    with _client_with(
        monkeypatch,
        {
            "GATEWAY_URL": url,
            "TWIN_URL": url,
            "GHOST_URL": url,
            "EVOLUTION_URL": url,
            "HEARTBEAT_URL": url,
        },
    ) as test_client:
        yield test_client, fake_backend[0]


def test_health(client):
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_index_serves_page(client):
    test_client, _ = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Project Sovereign" in response.text
    assert "/overview" in response.text


def test_overview_aggregates_all_sections(client):
    test_client, backend = client
    overview = test_client.get("/overview").json()

    assert overview["ledger"]["verify"]["valid"] is True
    assert overview["ledger"]["total_events_fetched"] == 30
    assert len(overview["events"]) == 25  # capped feed
    assert overview["events"][0]["seq"] == 30  # newest first
    assert overview["twin"]["capabilities"][0]["sole_provider"] is True
    assert overview["observations"][0]["observer"] == "dr-quinn"
    assert overview["proposals"][0]["status"] == "proposed"
    assert overview["genes"][0]["reputation"] == 0.667
    assert overview["heartbeat"]["ticks"] == 12

    # Gateway reads carried the API key; other services were called unauthenticated.
    by_path = {r["path"]: r for r in backend.requests}
    assert by_path["/events"]["auth"] == "Bearer dash-key"
    assert by_path["/verify"]["auth"] == "Bearer dash-key"
    assert by_path["/model"]["auth"] is None


def test_sections_degrade_independently(fake_backend, monkeypatch):
    _, url = fake_backend
    with _client_with(
        monkeypatch,
        {
            "GATEWAY_URL": url,
            "TWIN_URL": "http://127.0.0.1:1",  # twin is down
            "GHOST_URL": url,
            "EVOLUTION_URL": url,
            "HEARTBEAT_URL": url,
        },
    ) as test_client:
        overview = test_client.get("/overview").json()

    assert "error" in overview["twin"]
    assert "unreachable" in overview["twin"]["error"]
    # Everything else still rendered.
    assert overview["ledger"]["verify"]["valid"] is True
    assert overview["observations"][0]["observer"] == "dr-quinn"
    assert overview["heartbeat"]["ticks"] == 12


def test_backend_error_status_surfaces_in_section(fake_backend, monkeypatch):
    backend, url = fake_backend
    del backend.routes["/proposals"]  # evolution starts 404ing proposals
    with _client_with(
        monkeypatch,
        {
            "GATEWAY_URL": url,
            "TWIN_URL": url,
            "GHOST_URL": url,
            "EVOLUTION_URL": url,
            "HEARTBEAT_URL": url,
        },
    ) as test_client:
        overview = test_client.get("/overview").json()

    assert "404" in overview["proposals"]["error"]
    assert overview["genes"][0]["name"] == "pattern:cursor-sync"
