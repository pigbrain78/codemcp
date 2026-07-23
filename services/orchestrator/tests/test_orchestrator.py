import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient


def _respond(handler, status, obj):
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length)
    return json.loads(body) if body else {}


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        body = _read_json(self)
        self.server.received.append(body)
        if self.headers.get("Authorization") != f"Bearer {self.server.expected_key}":
            _respond(self, 401, {"error": "unauthorized"})
            return
        if self.path == "/events":
            self.server.seq += 1
            _respond(
                self,
                201,
                {"id": f"evt-{self.server.seq}", "seq": self.server.seq, **body},
            )
            return
        _respond(self, 404, {"error": "not_found"})


class _FakeAgentHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        body = _read_json(self)
        self.server.received.append({"path": self.path, "body": body})
        if self.server.mode == "ok":
            _respond(
                self,
                200,
                {"output": {"echo": body["input"], "agent_saw_task": body["task_id"]}},
            )
        else:
            _respond(self, 500, {"error": "agent exploded"})


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
        _FakeGatewayHandler, received=[], seq=0, expected_key="manus-key"
    )
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def fake_agent():
    server, thread, url = _start(_FakeAgentHandler, received=[], mode="ok")
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.orchestrator import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    gateway_server, gateway_url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("GATEWAY_API_KEY", gateway_server.expected_key)
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", str(tmp_path / "orchestrator.db"))

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, gateway_server


def _event_types(gateway_server):
    return [e["type"] for e in gateway_server.received]


def test_health(client):
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_and_list_agents(client, fake_agent):
    test_client, gateway_server = client
    _, agent_url = fake_agent

    response = test_client.post(
        "/agents",
        json={
            "name": "ralph5",
            "endpoint": agent_url,
            "capabilities": ["codegen", "refactor"],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "ralph5"
    assert body["capabilities"] == ["codegen", "refactor"]

    listing = test_client.get("/agents").json()
    assert [a["name"] for a in listing] == ["ralph5"]

    assert _event_types(gateway_server) == ["AgentRegistered"]
    assert gateway_server.received[0]["correlation_id"] == "ralph5"


def test_reregistration_updates_endpoint(client, fake_agent):
    test_client, _ = client
    _, agent_url = fake_agent

    test_client.post(
        "/agents",
        json={"name": "ralph5", "endpoint": "http://old", "capabilities": ["codegen"]},
    )
    test_client.post(
        "/agents",
        json={"name": "ralph5", "endpoint": agent_url, "capabilities": ["codegen"]},
    )

    listing = test_client.get("/agents").json()
    assert len(listing) == 1
    assert listing[0]["endpoint"] == agent_url


def test_dispatch_by_capability_success(client, fake_agent):
    test_client, gateway_server = client
    agent_server, agent_url = fake_agent

    test_client.post(
        "/agents",
        json={"name": "dr-quinn", "endpoint": agent_url, "capabilities": ["diagnosis"]},
    )

    response = test_client.post(
        "/tasks",
        json={"capability": "diagnosis", "input": {"symptom": "pod crashloop"}},
    )
    assert response.status_code == 201
    task = response.json()
    assert task["agent"] == "dr-quinn"
    assert task["status"] == "completed"
    assert task["output"]["echo"] == {"symptom": "pod crashloop"}
    assert task["output"]["agent_saw_task"] == task["id"]
    assert task["error"] is None
    assert task["finished_at"] is not None

    # The agent received the structured contract payload.
    assert agent_server.received[0]["body"]["capability"] == "diagnosis"

    # Ledger got the full lifecycle, correlated by task id.
    assert _event_types(gateway_server) == [
        "AgentRegistered",
        "AgentRequested",
        "AgentCompleted",
    ]
    requested, completed = gateway_server.received[1], gateway_server.received[2]
    assert requested["correlation_id"] == task["id"]
    assert completed["correlation_id"] == task["id"]
    assert completed["payload"]["output"]["echo"] == {"symptom": "pod crashloop"}


def test_dispatch_to_named_agent(client, fake_agent):
    test_client, _ = client
    _, agent_url = fake_agent

    test_client.post(
        "/agents",
        json={"name": "igor-os", "endpoint": agent_url, "capabilities": ["infra"]},
    )
    test_client.post(
        "/agents",
        json={"name": "igor-os-2", "endpoint": agent_url, "capabilities": ["infra"]},
    )

    task = test_client.post(
        "/tasks", json={"capability": "infra", "agent": "igor-os-2", "input": {}}
    ).json()
    assert task["agent"] == "igor-os-2"


def test_dispatch_no_agent_for_capability_404(client):
    test_client, _ = client
    response = test_client.post(
        "/tasks", json={"capability": "time-travel", "input": {}}
    )
    assert response.status_code == 404


def test_dispatch_named_agent_wrong_capability_409(client, fake_agent):
    test_client, _ = client
    _, agent_url = fake_agent
    test_client.post(
        "/agents",
        json={"name": "kid-os", "endpoint": agent_url, "capabilities": ["education"]},
    )
    response = test_client.post(
        "/tasks", json={"capability": "codegen", "agent": "kid-os", "input": {}}
    )
    assert response.status_code == 409


def test_agent_failure_recorded(client, fake_agent):
    test_client, gateway_server = client
    agent_server, agent_url = fake_agent
    agent_server.mode = "fail"

    test_client.post(
        "/agents",
        json={"name": "ralph5", "endpoint": agent_url, "capabilities": ["codegen"]},
    )
    task = test_client.post(
        "/tasks", json={"capability": "codegen", "input": {}}
    ).json()

    assert task["status"] == "failed"
    assert "500" in task["error"]
    assert task["output"] is None
    assert _event_types(gateway_server) == [
        "AgentRegistered",
        "AgentRequested",
        "AgentFailed",
    ]


def test_agent_unreachable_recorded(client):
    test_client, gateway_server = client
    test_client.post(
        "/agents",
        json={
            "name": "ghost",
            "endpoint": "http://127.0.0.1:1",
            "capabilities": ["haunting"],
        },
    )
    task = test_client.post(
        "/tasks", json={"capability": "haunting", "input": {}}
    ).json()

    assert task["status"] == "failed"
    assert "unreachable" in task["error"]
    assert _event_types(gateway_server) == [
        "AgentRegistered",
        "AgentRequested",
        "AgentFailed",
    ]


def test_task_listing_and_lookup(client, fake_agent):
    test_client, _ = client
    _, agent_url = fake_agent
    test_client.post(
        "/agents",
        json={"name": "ralph5", "endpoint": agent_url, "capabilities": ["codegen"]},
    )
    created = test_client.post(
        "/tasks", json={"capability": "codegen", "input": {}}
    ).json()

    listing = test_client.get("/tasks").json()
    assert [t["id"] for t in listing] == [created["id"]]

    completed_only = test_client.get("/tasks", params={"status": "completed"}).json()
    assert len(completed_only) == 1
    failed_only = test_client.get("/tasks", params={"status": "failed"}).json()
    assert failed_only == []

    fetched = test_client.get(f"/tasks/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    missing = test_client.get("/tasks/nope")
    assert missing.status_code == 404


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", str(tmp_path / "orchestrator.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        response = test_client.post(
            "/agents", json={"name": "x", "endpoint": "http://x", "capabilities": ["y"]}
        )
    assert response.status_code == 502


def test_gateway_auth_rejection_returns_502(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_API_KEY", "wrong-key")
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", str(tmp_path / "orchestrator2.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as bad_client:
        response = bad_client.post(
            "/agents", json={"name": "x", "endpoint": "http://x", "capabilities": ["y"]}
        )
    assert response.status_code == 502
