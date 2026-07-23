"""Full-stack integration test for the Project Sovereign platform.

Boots every service as a real process -- ledger, gateway (node), capture,
knowledge, orchestrator -- plus a toy specialist agent, all wired together
over real HTTP on localhost, then drives one idea through the entire
pipeline the architecture describes:

    capture -> gateway -> ledger -> knowledge sync -> search
    agent registration -> dispatch -> ledger audit trail -> chain verify

Requires node on PATH and the mcp-server-js repo checked out as a sibling
of this repo (for the gateway); skips with an explicit reason otherwise.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO_ROOT.parent / "mcp-server-js" / "services" / "gateway"

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH"),
    pytest.mark.skipif(
        not (GATEWAY_DIR / "src" / "index.js").exists(),
        reason="mcp-server-js repo not checked out as a sibling of codemcp",
    ),
]

API_KEYS = {
    "e2e-admin-key": {"role": "admin", "name": "e2e-admin"},
    "e2e-capture-key": {"role": "service", "name": "pocket-os-capture-api"},
    "e2e-knowledge-key": {"role": "reader", "name": "knowledge-graph"},
    "e2e-manus-key": {"role": "service", "name": "manus-prime"},
}

STARTUP_TIMEOUT = 30.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(url: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise RuntimeError(
                f"process for {url} exited early with {proc.returncode}:\n{stderr.decode()}"
            )
        try:
            if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                return
        except httpx.RequestError:
            time.sleep(0.1)
    raise TimeoutError(f"{url} did not become healthy within {STARTUP_TIMEOUT}s")


def _spawn_uvicorn(
    app_path: str, port: int, env_extra: dict[str, str]
) -> subprocess.Popen:
    env = {**os.environ, **env_extra}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class _ToyAgentHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        result = {
            "output": {
                "plan": f"handled {body['capability']}",
                "task_id": body["task_id"],
            }
        }
        data = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stack(tmp_path):
    procs: list[subprocess.Popen] = []
    ledger_port = _free_port()
    gateway_port = _free_port()
    capture_port = _free_port()
    knowledge_port = _free_port()
    orchestrator_port = _free_port()

    ledger_url = f"http://127.0.0.1:{ledger_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"

    agent_server = ThreadingHTTPServer(("127.0.0.1", 0), _ToyAgentHandler)
    agent_thread = threading.Thread(target=agent_server.serve_forever, daemon=True)
    agent_thread.start()
    agent_url = f"http://127.0.0.1:{agent_server.server_address[1]}"

    try:
        procs.append(
            _spawn_uvicorn(
                "services.ledger.app:app",
                ledger_port,
                {"LEDGER_DB_PATH": str(tmp_path / "ledger.db")},
            )
        )

        procs.append(
            subprocess.Popen(
                ["node", "src/index.js"],
                cwd=GATEWAY_DIR,
                env={
                    **os.environ,
                    "GATEWAY_PORT": str(gateway_port),
                    "LEDGER_URL": ledger_url,
                    "GATEWAY_API_KEYS": json.dumps(API_KEYS),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

        procs.append(
            _spawn_uvicorn(
                "services.capture.app:app",
                capture_port,
                {"GATEWAY_URL": gateway_url, "GATEWAY_API_KEY": "e2e-capture-key"},
            )
        )
        procs.append(
            _spawn_uvicorn(
                "services.knowledge.app:app",
                knowledge_port,
                {
                    "GATEWAY_URL": gateway_url,
                    "GATEWAY_API_KEY": "e2e-knowledge-key",
                    "KNOWLEDGE_DB_PATH": str(tmp_path / "knowledge.db"),
                },
            )
        )
        procs.append(
            _spawn_uvicorn(
                "services.orchestrator.app:app",
                orchestrator_port,
                {
                    "GATEWAY_URL": gateway_url,
                    "GATEWAY_API_KEY": "e2e-manus-key",
                    "ORCHESTRATOR_DB_PATH": str(tmp_path / "orchestrator.db"),
                },
            )
        )

        _wait_healthy(ledger_url, procs[0])
        _wait_healthy(gateway_url, procs[1])
        _wait_healthy(f"http://127.0.0.1:{capture_port}", procs[2])
        _wait_healthy(f"http://127.0.0.1:{knowledge_port}", procs[3])
        _wait_healthy(f"http://127.0.0.1:{orchestrator_port}", procs[4])

        yield {
            "gateway": gateway_url,
            "capture": f"http://127.0.0.1:{capture_port}",
            "knowledge": f"http://127.0.0.1:{knowledge_port}",
            "orchestrator": f"http://127.0.0.1:{orchestrator_port}",
            "agent": agent_url,
        }
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)
        agent_server.shutdown()
        agent_thread.join()


def _admin_get(gateway_url: str, path: str, params: dict | None = None):
    return httpx.get(
        f"{gateway_url}{path}",
        params=params or {},
        headers={"Authorization": "Bearer e2e-admin-key"},
        timeout=10.0,
    )


def _wait_for_event_types(
    gateway_url: str, wanted: set[str], timeout: float = 10.0
) -> list[dict]:
    """Audit logging is fire-and-forget, so poll briefly until the ledger
    contains every wanted event type."""
    deadline = time.monotonic() + timeout
    while True:
        events = _admin_get(gateway_url, "/events", {"limit": 1000}).json()
        types = {e["type"] for e in events}
        if wanted <= types:
            return events
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"ledger never contained {wanted - types}; saw {sorted(types)}"
            )
        time.sleep(0.2)


def test_idea_flows_through_the_whole_platform(stack):
    # 1. Pocket OS captures a thought.
    capture = httpx.post(
        f"{stack['capture']}/captures",
        json={
            "type": "voice_note",
            "content": "ideas for #agriforge irrigation timing",
        },
        timeout=10.0,
    )
    assert capture.status_code == 201
    assert capture.json()["project"] == "agriforge"

    # 2. The event landed on the ledger via the gateway, and the gateway
    #    audit-logged the capture API's write.
    events = _wait_for_event_types(
        stack["gateway"], {"CaptureCreated", "GatewayRequestHandled"}
    )
    capture_events = [e for e in events if e["type"] == "CaptureCreated"]
    assert len(capture_events) == 1
    assert capture_events[0]["payload"]["project"] == "agriforge"

    # 3. The knowledge graph syncs from the ledger and can find the idea.
    sync = httpx.post(f"{stack['knowledge']}/sync", timeout=10.0)
    assert sync.status_code == 200
    assert sync.json()["indexed"] == 1

    hits = httpx.get(
        f"{stack['knowledge']}/search", params={"q": "irrigation"}, timeout=10.0
    ).json()
    assert len(hits) == 1
    assert hits[0]["project"] == "agriforge"

    projects = httpx.get(f"{stack['knowledge']}/projects", timeout=10.0).json()
    assert projects == [{"project": "agriforge", "capture_count": 1}]

    # 4. Manus Prime registers a specialist and dispatches work to it.
    register = httpx.post(
        f"{stack['orchestrator']}/agents",
        json={
            "name": "ralph5",
            "endpoint": stack["agent"],
            "capabilities": ["planning"],
        },
        timeout=10.0,
    )
    assert register.status_code == 201

    task = httpx.post(
        f"{stack['orchestrator']}/tasks",
        json={"capability": "planning", "input": {"project": "agriforge"}},
        timeout=10.0,
    ).json()
    assert task["status"] == "completed"
    assert task["output"]["plan"] == "handled planning"

    # 5. The full agent lifecycle is on the ledger, correlated by task id.
    events = _wait_for_event_types(
        stack["gateway"], {"AgentRegistered", "AgentRequested", "AgentCompleted"}
    )
    lifecycle = [e["type"] for e in events if e.get("correlation_id") == task["id"]]
    assert lifecycle == ["AgentRequested", "AgentCompleted"]

    # 6. The hash chain over everything that just happened is intact. (No
    #    exact count assertion: every authenticated gateway read appends its
    #    own audit event, so the total keeps growing as we observe it.)
    verify = _admin_get(stack["gateway"], "/verify").json()
    assert verify["valid"] is True
    assert verify["count"] >= 5
