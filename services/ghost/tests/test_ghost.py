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
            self, 201, {"id": f"g{self.server.seq}", "seq": self.server.seq, **body}
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


def _event(i, type_, payload):
    return {
        "id": f"h{i}",
        "seq": i,
        "type": type_,
        "source": "test",
        "payload": payload,
        "ts": f"2026-01-01T00:{i:02d}:00+00:00",
    }


HISTORY = [
    _event(
        1, "AgentFailed", {"agent": "ralph5", "capability": "codegen", "error": "OOM"}
    ),
    _event(
        2, "AgentFailed", {"agent": "ralph5", "capability": "codegen", "error": "OOM"}
    ),
    _event(
        3,
        "AgentRequested",
        {"agent": "dr-quinn", "capability": "diagnosis", "input": {"x": 1}},
    ),
    _event(
        4,
        "AgentRequested",
        {"agent": "dr-quinn", "capability": "diagnosis", "input": {"x": 1}},
    ),
    _event(
        5,
        "EvolutionRecorded",
        {"problem": "p", "solution": "s", "reuse_potential": ""},
    ),
    _event(6, "GatewayRequestHandled", {"method": "GET", "path": "/events"}),
]


@pytest.fixture
def fake_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    server.appended = []
    server.history = list(HISTORY)
    server.seq = 0
    server.expected_key = "ghost-key"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.ghost import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    gateway_server, gateway_url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("GATEWAY_API_KEY", gateway_server.expected_key)
    monkeypatch.setenv("GHOST_DB_PATH", str(tmp_path / "ghost.db"))
    monkeypatch.setenv("UNUSED_AGENT_WINDOW", "3")

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, gateway_server


def _by_kind(observations):
    return {o["kind"]: o for o in observations}


def test_health(client):
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_observers_notice_and_ledger(client):
    test_client, gateway_server = client
    result = test_client.post("/observe").json()
    assert result["events_examined"] == len(HISTORY)
    assert result["new_observations"] == 3

    observations = _by_kind(test_client.get("/observations").json())

    failure = observations["recurring_failure"]
    assert failure["observer"] == "dr-quinn"
    assert failure["severity"] == "warning"
    assert failure["evidence"] == ["h1", "h2"]
    assert "ralph5/codegen" in failure["summary"]

    duplicate = observations["duplicate_work"]
    assert duplicate["observer"] == "ralph5"
    assert duplicate["evidence"] == ["h3", "h4"]

    docs = observations["missing_docs"]
    assert docs["observer"] == "kid"
    assert docs["evidence"] == ["h5"]

    ledgered = [e for e in gateway_server.appended if e["type"] == "GhostObservation"]
    assert {e["payload"]["kind"] for e in ledgered} == {
        "recurring_failure",
        "duplicate_work",
        "missing_docs",
    }


def test_observe_is_incremental_and_quiet_on_repeat(client):
    test_client, gateway_server = client
    test_client.post("/observe")
    before = len(gateway_server.appended)

    second = test_client.post("/observe").json()
    assert second["new_observations"] == 0
    assert second["updated_observations"] == 0
    # No new ledger noise from a quiet pass.
    assert len(gateway_server.appended) == before


def test_repeat_sighting_updates_without_new_ledger_event(client):
    test_client, gateway_server = client
    test_client.post("/observe")
    ledgered_before = len(
        [e for e in gateway_server.appended if e["type"] == "GhostObservation"]
    )

    gateway_server.history.append(
        _event(
            7,
            "AgentFailed",
            {"agent": "ralph5", "capability": "codegen", "error": "OOM"},
        )
    )
    result = test_client.post("/observe").json()
    assert result["new_observations"] == 0
    assert result["updated_observations"] == 1

    failure = _by_kind(test_client.get("/observations").json())["recurring_failure"]
    assert failure["occurrences"] == 2
    assert failure["evidence"] == ["h1", "h2", "h7"]

    ledgered_after = len(
        [e for e in gateway_server.appended if e["type"] == "GhostObservation"]
    )
    assert ledgered_after == ledgered_before


def test_unused_agent_detected_after_window(client):
    test_client, gateway_server = client
    test_client.post("/observe")

    # ghost-writer registers, then the platform moves on without it.
    events = [_event(10, "AgentRegistered", {"agent": "ghost-writer"})]
    for i in range(11, 15):
        events.append(_event(i, "CaptureCreated", {"content": f"c{i}"}))
    gateway_server.history.extend(events)

    test_client.post("/observe")
    observations = _by_kind(test_client.get("/observations").json())
    unused = observations["unused_agent"]
    assert unused["observer"] == "igor"
    assert "ghost-writer" in unused["summary"]


def test_dispatched_agent_is_not_flagged_unused(client):
    test_client, gateway_server = client
    gateway_server.history.extend(
        [
            _event(10, "AgentRegistered", {"agent": "busy-bee"}),
            _event(
                11,
                "AgentRequested",
                {"agent": "busy-bee", "capability": "x", "input": {}},
            ),
            _event(12, "CaptureCreated", {"content": "a"}),
            _event(13, "CaptureCreated", {"content": "b"}),
            _event(14, "CaptureCreated", {"content": "c"}),
        ]
    )
    test_client.post("/observe")
    kinds = [
        o["kind"]
        for o in test_client.get("/observations").json()
        if "busy-bee" in o["summary"]
    ]
    assert kinds == []


def test_acknowledge_and_dismiss(client):
    test_client, _ = client
    test_client.post("/observe")
    observations = test_client.get("/observations").json()

    acked = test_client.post(
        f"/observations/{observations[0]['id']}/acknowledge"
    ).json()
    assert acked["status"] == "acknowledged"
    dismissed = test_client.post(
        f"/observations/{observations[1]['id']}/dismiss"
    ).json()
    assert dismissed["status"] == "dismissed"

    open_only = test_client.get("/observations", params={"status": "open"}).json()
    assert len(open_only) == len(observations) - 2

    assert test_client.post("/observations/nope/dismiss").status_code == 404


def test_filter_by_observer(client):
    test_client, _ = client
    test_client.post("/observe")
    quinn_only = test_client.get(
        "/observations", params={"observer": "dr-quinn"}
    ).json()
    assert len(quinn_only) == 1
    assert quinn_only[0]["kind"] == "recurring_failure"


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("GHOST_DB_PATH", str(tmp_path / "ghost.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        assert test_client.post("/observe").status_code == 502
