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
        if self.path == "/events":
            self.server.seq += 1
            _respond(
                self,
                201,
                {"id": f"evt-{self.server.seq}", "seq": self.server.seq, **body},
            )
            return
        _respond(self, 404, {"error": "not_found"})

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
    _event(1, "CaptureCreated", {"content": "a"}),
    _event(2, "CaptureCreated", {"content": "b"}),
    _event(3, "CaptureCreated", {"content": "c"}),
    _event(
        4, "AgentFailed", {"agent": "ralph5", "capability": "codegen", "error": "boom"}
    ),
    _event(
        5, "AgentFailed", {"agent": "ralph5", "capability": "codegen", "error": "boom"}
    ),
    _event(6, "AgentCompleted", {"agent": "dr-quinn", "capability": "diagnosis"}),
    _event(7, "AgentCompleted", {"agent": "dr-quinn", "capability": "diagnosis"}),
    _event(8, "AgentCompleted", {"agent": "dr-quinn", "capability": "diagnosis"}),
    _event(9, "GatewayRequestHandled", {"method": "GET", "path": "/events"}),
    _event(10, "GatewayRequestHandled", {"method": "GET", "path": "/events"}),
    _event(11, "GatewayRequestHandled", {"method": "GET", "path": "/events"}),
]


@pytest.fixture
def fake_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    server.appended = []
    server.history = list(HISTORY)
    server.seq = 100
    server.expected_key = "evolution-key"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.evolution import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_gateway, monkeypatch, tmp_path):
    gateway_server, gateway_url = fake_gateway
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("GATEWAY_API_KEY", gateway_server.expected_key)
    monkeypatch.setenv("EVOLUTION_DB_PATH", str(tmp_path / "evolution.db"))

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, gateway_server


def _appended_types(gateway_server):
    return [e["type"] for e in gateway_server.appended]


RECORD = {
    "problem": "Knowledge sync re-indexed everything on each run",
    "evidence": ["evt-12", "sync latency 4s -> 90ms"],
    "solution": "Timestamp cursor + INSERT OR IGNORE keyed by event id",
    "confidence": "high",
    "impact": "Sync cost now proportional to new events only",
    "rollback": "Remove cursor; full rescan is safe because indexing is idempotent",
    "affected_systems": ["knowledge"],
    "reuse_potential": "Any service that mirrors the ledger",
}


def test_health(client):
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_evolution_record_stored_and_ledgered(client):
    test_client, gateway_server = client
    response = test_client.post("/records", json=RECORD)
    assert response.status_code == 201
    record = response.json()
    assert record["confidence"] == "high"

    listing = test_client.get("/records").json()
    assert [r["id"] for r in listing] == [record["id"]]

    assert _appended_types(gateway_server) == ["EvolutionRecorded"]
    ledgered = gateway_server.appended[0]
    assert ledgered["correlation_id"] == record["id"]
    assert ledgered["payload"]["problem"] == RECORD["problem"]


def test_record_requires_evidence(client):
    test_client, _ = client
    bad = dict(RECORD, evidence=[])
    assert test_client.post("/records", json=bad).status_code == 422


def test_gene_lifecycle_and_reputation(client):
    test_client, gateway_server = client
    record = test_client.post("/records", json=RECORD).json()

    gene = test_client.post(
        "/genes",
        json={
            "name": "pattern:cursor-sync",
            "rationale": "Incremental idempotent mirror of an append-only log",
            "origin_record_id": record["id"],
        },
    ).json()
    assert gene["reputation"] is None
    assert gene["origin_record_id"] == record["id"]

    duplicate = test_client.post(
        "/genes", json={"name": "pattern:cursor-sync", "rationale": "x"}
    )
    assert duplicate.status_code == 409

    test_client.post(f"/genes/{gene['id']}/reuse", json={"project": "relay"})
    test_client.post(f"/genes/{gene['id']}/outcomes", json={"outcome": "success"})
    test_client.post(f"/genes/{gene['id']}/outcomes", json={"outcome": "success"})
    test_client.post(f"/genes/{gene['id']}/outcomes", json={"outcome": "failure"})

    listing = test_client.get("/genes").json()
    assert listing[0]["reuse_count"] == 1
    assert listing[0]["successes"] == 2
    assert listing[0]["failures"] == 1
    assert listing[0]["reputation"] == 0.667

    retired = test_client.post(f"/genes/{gene['id']}/retire").json()
    assert retired["retired"] is True
    assert test_client.get("/genes").json() == []
    assert len(test_client.get("/genes", params={"include_retired": True}).json()) == 1

    assert _appended_types(gateway_server) == [
        "EvolutionRecorded",
        "GeneCreated",
        "GeneReused",
        "GeneOutcomeRecorded",
        "GeneOutcomeRecorded",
        "GeneOutcomeRecorded",
        "GeneRetired",
    ]


def test_reflection_answers_five_questions(client):
    test_client, gateway_server = client
    report = test_client.post("/reflect").json()

    # 1. What repeated? CaptureCreated x3 and AgentCompleted x3; audit noise excluded.
    repeated_types = {r["event_type"] for r in report["what_repeated"]}
    assert "CaptureCreated" in repeated_types
    assert "GatewayRequestHandled" not in repeated_types

    # 3. What failed repeatedly? ralph5/codegen x2 with evidence ids.
    assert report["what_failed"] == [
        {
            "agent": "ralph5",
            "capability": "codegen",
            "count": 2,
            "evidence": ["h4", "h5"],
        }
    ]

    # 4. What should become reusable? diagnosis completed 3x.
    assert report["reuse_candidates"][0]["capability"] == "diagnosis"

    # 5. Proposals: one recurring_failure + one gene_candidate, all awaiting humans.
    kinds = sorted(p["kind"] for p in report["proposals"])
    assert kinds == ["gene_candidate", "recurring_failure"]
    assert all(p["status"] == "proposed" for p in report["proposals"])
    failure_proposal = next(
        p for p in report["proposals"] if p["kind"] == "recurring_failure"
    )
    assert failure_proposal["evidence"] == ["h4", "h5"]

    # The reflection itself is on the ledger.
    assert _appended_types(gateway_server) == ["EvolutionReflectionCompleted"]


def test_reflection_is_incremental(client):
    test_client, gateway_server = client
    first = test_client.post("/reflect").json()
    assert first["events_examined"] == len(HISTORY)

    second = test_client.post("/reflect").json()
    # Only the boundary event is re-fetched; nothing new to report.
    assert second["events_examined"] <= 1
    assert second["proposals"] == []


def test_existing_gene_suppresses_reuse_candidate(client):
    test_client, _ = client
    test_client.post(
        "/genes", json={"name": "pattern:diagnosis", "rationale": "already extracted"}
    )
    report = test_client.post("/reflect").json()
    assert report["reuse_candidates"] == []


def test_proposal_approval_flow(client):
    test_client, gateway_server = client
    report = test_client.post("/reflect").json()
    proposal = report["proposals"][0]

    approved = test_client.post(f"/proposals/{proposal['id']}/approve").json()
    assert approved["status"] == "approved"

    again = test_client.post(f"/proposals/{proposal['id']}/approve")
    assert again.status_code == 409

    other = report["proposals"][1]
    rejected = test_client.post(f"/proposals/{other['id']}/reject").json()
    assert rejected["status"] == "rejected"

    proposed_left = test_client.get("/proposals", params={"status": "proposed"}).json()
    assert proposed_left == []
    assert test_client.post("/proposals/nope/approve").status_code == 404

    types = _appended_types(gateway_server)
    assert types[-2:] == ["EvolutionProposalApproved", "EvolutionProposalRejected"]


def test_gateway_unreachable_returns_502(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GATEWAY_API_KEY", "whatever")
    monkeypatch.setenv("EVOLUTION_DB_PATH", str(tmp_path / "evolution.db"))
    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        assert test_client.post("/records", json=RECORD).status_code == 502
        assert test_client.post("/reflect").status_code == 502
