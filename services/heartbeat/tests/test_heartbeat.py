import importlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient


class _FakeServiceHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        self.server.calls.append(self.path)
        status = 500 if self.server.mode == "fail" else 200
        data = json.dumps({"ok": status == 200}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start(mode="ok"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeServiceHandler)
    server.calls = []
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def services():
    ok_server, ok_thread, ok_url = _start("ok")
    fail_server, fail_thread, fail_url = _start("fail")
    try:
        yield ok_server, ok_url, fail_server, fail_url
    finally:
        ok_server.shutdown()
        ok_thread.join()
        fail_server.shutdown()
        fail_thread.join()


def _reload_app():
    from services.heartbeat import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(services, monkeypatch):
    ok_server, ok_url, fail_server, fail_url = services
    monkeypatch.setenv("KNOWLEDGE_URL", ok_url)
    monkeypatch.setenv("TWIN_URL", ok_url)
    monkeypatch.setenv("GHOST_URL", fail_url)  # one target that keeps failing
    monkeypatch.setenv("RELAY_URL", ok_url)
    monkeypatch.setenv("EVOLUTION_URL", ok_url)
    monkeypatch.setenv("TICK_SECONDS", "0.05")
    monkeypatch.setenv("REFLECT_EVERY_TICKS", "3")

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, ok_server, fail_server


def _wait_for_ticks(test_client, minimum, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = test_client.get("/status").json()
        if status["ticks"] >= minimum:
            return status
        time.sleep(0.05)
    raise TimeoutError(f"never reached {minimum} ticks")


def test_health(client):
    test_client, _, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}


def test_tick_targets_run_every_tick(client):
    test_client, ok_server, _ = client
    status = _wait_for_ticks(test_client, 4)
    targets = {t["name"]: t for t in status["targets"]}

    for name in ["knowledge-sync", "twin-sync", "twin-probe", "relay-deliver"]:
        assert targets[name]["runs"] >= 4
        assert targets[name]["last_status"] == "ok"
        assert targets[name]["failures"] == 0

    # Each service was hit at its expected path.
    assert "/sync" in ok_server.calls
    assert "/probe" in ok_server.calls
    assert "/deliver" in ok_server.calls


def test_failing_target_recorded_but_does_not_stop_the_pulse(client):
    test_client, _, fail_server = client
    status = _wait_for_ticks(test_client, 4)
    targets = {t["name"]: t for t in status["targets"]}

    ghost = targets["ghost-observe"]
    assert ghost["runs"] >= 4
    assert ghost["failures"] == ghost["runs"]
    assert ghost["last_status"] == "error"
    assert "500" in ghost["last_detail"]

    # The failing target kept the healthy ones running.
    assert targets["knowledge-sync"]["last_status"] == "ok"
    assert len(fail_server.calls) >= 4


def test_reflect_runs_on_its_own_cadence(client):
    test_client, _, _ = client
    status = _wait_for_ticks(test_client, 7)
    targets = {t["name"]: t for t in status["targets"]}

    reflect = targets["evolution-reflect"]
    ticks = status["ticks"]
    # Runs once every 3 ticks, never every tick.
    assert reflect["runs"] >= 2
    assert reflect["runs"] <= ticks // 3 + 1
    assert reflect["runs"] < targets["knowledge-sync"]["runs"]
    assert reflect["last_status"] == "ok"


def test_unreachable_target_is_an_error_not_a_crash(services, monkeypatch):
    _, ok_url, _, _ = services
    monkeypatch.setenv("KNOWLEDGE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TWIN_URL", ok_url)
    monkeypatch.setenv("GHOST_URL", ok_url)
    monkeypatch.setenv("RELAY_URL", ok_url)
    monkeypatch.setenv("EVOLUTION_URL", ok_url)
    monkeypatch.setenv("TICK_SECONDS", "0.05")
    monkeypatch.setenv("REFLECT_EVERY_TICKS", "1000")

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        status = _wait_for_ticks(test_client, 3)
    targets = {t["name"]: t for t in status["targets"]}
    assert targets["knowledge-sync"]["last_status"] == "error"
    assert "unreachable" in targets["knowledge-sync"]["last_detail"]
    assert targets["twin-sync"]["last_status"] == "ok"
