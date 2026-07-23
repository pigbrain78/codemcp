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


class _FakeKnowledgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):
        if self.path == "/projects":
            _respond(self, 200, self.server.projects)
            return
        prefix = "/projects/"
        if self.path.startswith(prefix):
            name = self.path[len(prefix) :]
            _respond(self, 200, self.server.project_captures.get(name, []))
            return
        _respond(self, 404, {"error": "not_found"})


class _FakeNotionHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_POST(self):
        body = _read_json(self)
        self.server.received.append({"method": "POST", "path": self.path, "body": body})

        if self.path.startswith("/v1/databases/") and self.path.endswith("/query"):
            title = body["filter"]["title"]["equals"]
            page_id = self.server.pages.get(title)
            _respond(self, 200, {"results": [{"id": page_id}] if page_id else []})
            return

        if self.path == "/v1/pages":
            title = body["properties"][self.server.title_prop]["title"][0]["text"][
                "content"
            ]
            new_id = f"page-{len(self.server.pages) + 1}"
            self.server.pages[title] = new_id
            _respond(self, 200, {"id": new_id})
            return

        _respond(self, 404, {"error": "not_found"})

    def do_PATCH(self):
        body = _read_json(self)
        self.server.received.append(
            {"method": "PATCH", "path": self.path, "body": body}
        )
        _respond(self, 200, {"id": self.path.rsplit("/", 1)[-1]})


def _start(handler_cls, **attrs):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    for key, value in attrs.items():
        setattr(server, key, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def fake_knowledge():
    server, thread, url = _start(
        _FakeKnowledgeHandler,
        projects=[{"project": "agriforge", "capture_count": 2}],
        project_captures={
            "agriforge": [
                {"event_id": "e2", "tags": ["agriforge", "irrigation"]},
                {"event_id": "e4", "tags": ["agriforge"]},
            ]
        },
    )
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def fake_notion():
    server, thread, url = _start(
        _FakeNotionHandler, received=[], pages={}, title_prop="Name"
    )
    try:
        yield server, url
    finally:
        server.shutdown()
        thread.join()


def _reload_app():
    from services.notion import app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(fake_knowledge, fake_notion, monkeypatch):
    knowledge_server, knowledge_url = fake_knowledge
    notion_server, notion_url = fake_notion

    monkeypatch.setenv("KNOWLEDGE_URL", knowledge_url)
    monkeypatch.setenv("NOTION_API_URL", notion_url)
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db-1")

    app_module = _reload_app()
    with TestClient(app_module.app) as test_client:
        yield test_client, knowledge_server, notion_server


def test_health(client):
    test_client, _, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sync_creates_new_project_page(client):
    test_client, _, notion_server = client
    response = test_client.post("/sync")
    assert response.status_code == 200
    assert response.json() == {
        "projects_seen": 1,
        "pages_created": 1,
        "pages_updated": 0,
    }

    assert "agriforge" in notion_server.pages
    create_calls = [r for r in notion_server.received if r["path"] == "/v1/pages"]
    assert len(create_calls) == 1
    props = create_calls[0]["body"]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "agriforge"
    assert props["Captures"]["number"] == 2
    assert {t["name"] for t in props["Tags"]["multi_select"]} == {
        "agriforge",
        "irrigation",
    }


def test_sync_updates_existing_project_page(client):
    test_client, _, notion_server = client
    notion_server.pages["agriforge"] = "page-1"

    response = test_client.post("/sync")
    assert response.json() == {
        "projects_seen": 1,
        "pages_created": 0,
        "pages_updated": 1,
    }

    patch_calls = [r for r in notion_server.received if r["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0]["path"] == "/v1/pages/page-1"


def test_sync_with_no_projects_does_not_touch_notion(
    fake_knowledge, fake_notion, monkeypatch
):
    knowledge_server, knowledge_url = fake_knowledge
    notion_server, notion_url = fake_notion
    knowledge_server.projects = []

    monkeypatch.setenv("KNOWLEDGE_URL", knowledge_url)
    monkeypatch.setenv("NOTION_API_URL", notion_url)
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db-1")
    app_module = _reload_app()

    with TestClient(app_module.app) as test_client:
        response = test_client.post("/sync")

    assert response.json() == {
        "projects_seen": 0,
        "pages_created": 0,
        "pages_updated": 0,
    }
    assert notion_server.received == []


def test_knowledge_unavailable_returns_502(fake_notion, monkeypatch):
    _, notion_url = fake_notion
    monkeypatch.setenv("KNOWLEDGE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NOTION_API_URL", notion_url)
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db-1")
    app_module = _reload_app()

    with TestClient(app_module.app) as test_client:
        response = test_client.post("/sync")
    assert response.status_code == 502


def test_notion_unavailable_returns_502(fake_knowledge, monkeypatch):
    _, knowledge_url = fake_knowledge
    monkeypatch.setenv("KNOWLEDGE_URL", knowledge_url)
    monkeypatch.setenv("NOTION_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db-1")
    app_module = _reload_app()

    with TestClient(app_module.app) as test_client:
        response = test_client.post("/sync")
    assert response.status_code == 502
