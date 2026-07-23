import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient


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

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {self.server.expected_key}"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.server.appended.append(body)
        if not self._authed():
            _respond(self, 401, {"error": "unauthorized"})
            return
        self.server.seq += 1
        _respond(
            self, 201, {"id": f"t{self.server.seq}", "seq": self.server.seq, **body}
        )

    def do_GET(self):
        parts = urlsplit(self.path)
        if not self._authed():
            _respond(self, 401, {"error": "unauthorized"})
            return
        if parts.path != "/events":
            _respond(self, 404, {"error": "not_found"})
            return
        query = parse_qs(parts.query)
        since = query.get("since", [None])[0]
        events = [e for e in self.server.history if since is None or e["ts"] >= since]
        _respond(self, 200, events)


class _FakeHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):
        if self.server.mode == "ok":
            _respond(self, 200, {"status": "ok"})
        else:
            _respond(self, 500, {"status": "dying"})


def _event(i, type_, source, payload):
    return {
        "id": f"h{i}",
        "seq": i,
        "type": type_,
        "source": source,
        "payload": payload,
        "ts": f"2026-01-01T00:{i:02d}:00+00:00",
    }


HISTORY = [
    _event(1, "CaptureCreated", "pocket-os-capture-api", {"content": "a"}),
    _event(2, "CaptureCreated", "pocket-os-capture-api", {"content": "b"}),
    _event(
        3,
        "AgentRegistered",
        "manus-prime",
        {
            "agent": "failure-analyst",
            "endpoint": "http://fa",
            "capabilities": ["diagnosis"],
        },
    ),
    _event(
        4,
        "AgentRegistered",
        "manus-prime",
        {
            "agent": "software-engineer",
            "endpoint": "http://se",
            "capabilities": ["codegen", "review"],
        },
    ),
    _event(
        5,
        "AgentRegistered",
        "manus-prime",
        {"agent": "kid", "endpoint": "http://kid", "capabilities": ["review"]},
    ),
]


@pytest.fixture
def fake_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    server.appended = []
    server.history = list(HISTORY)
    server.seq = 0
    server.expected_key = "twin-key"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def fake_health():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHealthHandler)
    server.mode = "ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.twin import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    gateway_server, gateway_url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("GATEWAY_API_KEY", gateway_server.expected_key)
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "twin.db"))

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, gateway_server


def _declare_topology(test_client):
    # ledger <- gateway <- {capture, knowledge, orchestrator}; knowledge <- notion
    test_client.post("/services", json={"name": "ledger"})
    test_client.post("/services", json={"name": "gateway", "depends_on": ["ledger"]})
    test_client.post(
        "/services", json={"name": "pocket-os-capture-api", "depends_on": ["gateway"]}
    )
    test_client.post("/services", json={"name": "knowledge", "depends_on": ["gateway"]})
    test_client.post(
        "/services", json={"name": "manus-prime", "depends_on": ["gateway"]}
    )
    test_client.post("/services", json={"name": "notion", "depends_on": ["knowledge"]})


def test_health(client):
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_declare_service_is_ledgered_and_upserts(client):
    test_client, gateway_server = client
    first = test_client.post(
        "/services", json={"name": "knowledge", "depends_on": ["gateway"]}
    )
    assert first.status_code == 201
    assert first.json()["declared"] is True

    test_client.post(
        "/services", json={"name": "knowledge", "depends_on": ["gateway", "ledger"]}
    )
    model = test_client.get("/model").json()
    knowledge = next(s for s in model["services"] if s["name"] == "knowledge")
    assert knowledge["depends_on"] == ["gateway", "ledger"]

    declared_events = [
        e for e in gateway_server.appended if e["type"] == "TwinServiceDeclared"
    ]
    assert len(declared_events) == 2


def test_sync_discovers_agents_capabilities_and_activity(client):
    test_client, _ = client
    result = test_client.post("/sync").json()
    assert result["events_examined"] == len(HISTORY)
    assert result["agents_seen"] == 3
    assert result["sources_seen"] == 2  # pocket-os-capture-api, manus-prime

    model = test_client.get("/model").json()

    agent_names = {a["name"] for a in model["agents"]}
    assert agent_names == {"failure-analyst", "software-engineer", "kid"}

    capabilities = {c["name"]: c for c in model["capabilities"]}
    assert capabilities["diagnosis"]["sole_provider"] is True
    assert capabilities["review"]["sole_provider"] is False
    assert capabilities["review"]["providers"] == ["kid", "software-engineer"]

    capture = next(s for s in model["services"] if s["name"] == "pocket-os-capture-api")
    assert capture["kind"] == "observed"
    assert capture["declared"] is False
    assert capture["event_count"] == 2
    assert capture["last_seen"] == HISTORY[1]["ts"]


def test_sync_is_incremental(client):
    test_client, gateway_server = client
    test_client.post("/sync")
    second = test_client.post("/sync").json()
    assert second["events_examined"] <= 1

    gateway_server.history.append(
        _event(6, "CaptureCreated", "pocket-os-capture-api", {"content": "c"})
    )
    test_client.post("/sync")
    model = test_client.get("/model").json()
    capture = next(s for s in model["services"] if s["name"] == "pocket-os-capture-api")
    assert capture["event_count"] == 3


def test_declared_topology_survives_sync(client):
    test_client, _ = client
    _declare_topology(test_client)
    test_client.post("/sync")

    model = test_client.get("/model").json()
    capture = next(s for s in model["services"] if s["name"] == "pocket-os-capture-api")
    # Declared before observed: keeps its declared dependencies and kind.
    assert capture["declared"] is True
    assert capture["depends_on"] == ["gateway"]
    assert capture["event_count"] == 2


def test_impact_of_service_walks_reverse_dependencies(client):
    test_client, _ = client
    _declare_topology(test_client)

    impact = test_client.get("/impact/ledger").json()
    assert impact["node_type"] == "service"
    assert impact["affected_services"] == [
        "gateway",
        "knowledge",
        "manus-prime",
        "notion",
        "pocket-os-capture-api",
    ]

    leaf = test_client.get("/impact/notion").json()
    assert leaf["affected_services"] == []
    assert "No declared service depends on this node." in leaf["notes"]


def test_impact_of_agent_flags_sole_provider_capabilities(client):
    test_client, _ = client
    test_client.post("/sync")

    fa = test_client.get("/impact/failure-analyst").json()
    assert fa["node_type"] == "agent"
    assert fa["affected_capabilities"] == ["diagnosis"]
    assert any("no remaining provider" in note for note in fa["notes"])

    kid = test_client.get("/impact/kid").json()
    assert kid["affected_capabilities"] == ["review"]
    assert kid["notes"] == []  # software-engineer still provides review


def test_impact_of_capability_lists_providers(client):
    test_client, _ = client
    test_client.post("/sync")
    review = test_client.get("/impact/review").json()
    assert review["node_type"] == "capability"
    assert "kid" in review["notes"][0]


def test_impact_unknown_node_404(client):
    test_client, _ = client
    assert test_client.get("/impact/nonexistent").status_code == 404


def test_probe_updates_health(client, fake_health):
    test_client, _ = client
    health_server, health_url = fake_health

    test_client.post(
        "/services", json={"name": "probed-ok", "health_url": f"{health_url}/health"}
    )
    test_client.post(
        "/services",
        json={"name": "probed-dead", "health_url": "http://127.0.0.1:1/health"},
    )
    test_client.post("/services", json={"name": "unprobed"})

    result = test_client.post("/probe").json()
    assert result == {"probed": 2, "healthy": 1, "unhealthy": 1}

    model = test_client.get("/model").json()
    by_name = {s["name"]: s for s in model["services"]}
    assert by_name["probed-ok"]["health"] == "healthy"
    assert by_name["probed-dead"]["health"] == "unhealthy"
    assert by_name["unprobed"]["health"] == "unknown"

    health_server.mode = "fail"
    test_client.post("/probe")
    model = test_client.get("/model").json()
    by_name = {s["name"]: s for s in model["services"]}
    assert by_name["probed-ok"]["health"] == "unhealthy"


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "twin.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        assert test_client.post("/sync").status_code == 502
        assert test_client.post("/services", json={"name": "x"}).status_code == 502
