import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB_PATH", str(tmp_path / "ledger.db"))
    from services.ledger import app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_append_and_get_event(client):
    response = client.post(
        "/events",
        json={
            "type": "CaptureCreated",
            "source": "pocket-os",
            "payload": {"text": "hi"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "CaptureCreated"
    assert body["seq"] == 1
    assert body["prev_hash"] == "0" * 64
    assert len(body["hash"]) == 64

    fetched = client.get(f"/events/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_get_missing_event_404(client):
    response = client.get("/events/does-not-exist")
    assert response.status_code == 404


def test_chain_links_and_verifies(client):
    for i in range(5):
        client.post(
            "/events",
            json={"type": "TaskAssigned", "source": "manus-prime", "payload": {"i": i}},
        )

    events = client.get("/events").json()
    assert len(events) == 5
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    for prev, curr in zip(events, events[1:]):
        assert curr["prev_hash"] == prev["hash"]

    verify = client.get("/verify").json()
    assert verify == {"valid": True, "count": 5, "broken_at_seq": None}


def test_filter_by_type_and_source(client):
    client.post("/events", json={"type": "A", "source": "s1", "payload": {}})
    client.post("/events", json={"type": "B", "source": "s2", "payload": {}})
    client.post("/events", json={"type": "A", "source": "s2", "payload": {}})

    by_type = client.get("/events", params={"type": "A"}).json()
    assert {e["source"] for e in by_type} == {"s1", "s2"}

    by_source = client.get("/events", params={"source": "s2"}).json()
    assert {e["type"] for e in by_source} == {"A", "B"}


def test_verify_detects_tampering(client, tmp_path):
    import sqlite3

    client.post("/events", json={"type": "A", "source": "s", "payload": {}})
    client.post("/events", json={"type": "B", "source": "s", "payload": {}})

    db_path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE events SET payload = ? WHERE seq = 1", (json.dumps({"tampered": True}),)
    )
    conn.commit()
    conn.close()

    verify = client.get("/verify").json()
    assert verify["valid"] is False
    assert verify["broken_at_seq"] == 1
