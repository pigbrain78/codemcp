# Ledger Service

The append-only ledger from the Project Sovereign "Enterprise Memory Layer".
Every event in the ecosystem (`CaptureCreated`, `TaskAssigned`,
`AgentCompleted`, ...) is appended here. Records are hash-chained so
tampering with history is detectable via `GET /verify`, and the API has no
update or delete routes — history is immutable by construction.

This is a standalone FastAPI service, kept separate from the `codemcp`
package itself. It reuses `codemcp`'s existing `fastapi`/`uvicorn`/`httpx`
dependencies via the repo's `uv` environment, so no extra install step is
needed.

## Running

```sh
uv run uvicorn services.ledger.app:app --reload --port 8001
```

Set `LEDGER_DB_PATH` to control where the SQLite file is written (defaults
to `ledger.db` in the current directory).

## API

- `POST /events` — append an event. Body: `{"type": str, "source": str, "payload": object, "correlation_id": str | null}`. Returns the stored record including its `seq`, `prev_hash`, and `hash`.
- `GET /events?type=&source=&since=&limit=` — list events, oldest first.
- `GET /events/{id}` — fetch a single event by id.
- `GET /verify` — recompute the hash chain and report whether it's intact.
- `GET /health` — liveness check.

## Testing

```sh
uv run pytest services/ledger/tests
```
