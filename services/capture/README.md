# Pocket OS Capture API

The backend Pocket OS calls whenever the user captures something — a
thought, voice note, photo, screenshot, document, or quick task. Per the
architecture: "Pocket OS never stores authoritative data by itself. It
communicates with your backend services." This is that service.

It does the first two steps of the capture workflow inline:

1. **Classification / project detection** — a deterministic stand-in for
   now: `#hashtag`s in the content become tags, and the first hashtag (or
   an explicit `project_hint` from the client) becomes the detected
   project. This is intentionally simple; swap in real NLP/embedding-based
   classification later without changing the API shape.
2. **Event dispatch** — forwards a `CaptureCreated` event to the **API
   Gateway** (`services/gateway` in the `mcp-server-js` repo), which is the
   only thing allowed to write to the ledger. This service never talks to
   the ledger directly.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=capture-api-key \
uv run uvicorn services.capture.app:app --reload --port 8002
```

The gateway must have a `service`-role key configured for this API, e.g. in
`GATEWAY_API_KEYS`:

```json
{"capture-api-key": {"role": "service", "name": "pocket-os-capture-api"}}
```

## API

- `POST /captures` — body: `{"type": "text"|"voice_note"|"photo"|"screenshot"|"document"|"task", "content": str, "project_hint": str | null, "metadata": object}`. Returns the resulting ledger event id/seq plus the detected `project` and `tags`.
- `GET /health` — liveness check.

A gateway rejection or outage surfaces as `502` — this service does not
silently swallow or fall back when it can't reach the gateway.

## Testing

```sh
uv run pytest services/capture/tests
```

Tests run against a real HTTP stand-in for the gateway (Python's
`http.server`), not a mocking library.
