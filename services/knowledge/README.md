# Knowledge Graph / Search Service

The "Knowledge Graph" piece of the Enterprise Memory Layer. It pulls
`CaptureCreated` events from the ledger — read through the gateway, never
directly, same governance rule as every other service here — and builds:

- a full-text search index over capture content (SQLite FTS5)
- a light graph of `capture -> project` and `capture -> tag` relationships

Full-text search is a deliberate placeholder for real embeddings/vector
search (or Neo4j) later; the API shape (`/search`, `/projects`, `/tags`)
is meant to stay stable across that swap — only the storage/matching
underneath `/search` would change.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=knowledge-api-key \
uv run uvicorn services.knowledge.app:app --reload --port 8003
```

The gateway needs a `reader`-role key configured for this service, e.g.:

```json
{"knowledge-api-key": {"role": "reader", "name": "knowledge-graph"}}
```

Call `POST /sync` periodically (cron, or manually) to pull new events from
the ledger into the local index. Each call fetches events since the last
successful sync's cursor, so it's safe to call repeatedly.

## API

- `POST /sync` — pull new `CaptureCreated` events from the ledger and index them. Returns `{"fetched": int, "indexed": int, "last_ts": str}`.
- `GET /search?q=&project=&tag=&limit=` — full-text search over indexed captures, optionally filtered by project or tag.
- `GET /projects` — list projects with their capture counts.
- `GET /projects/{name}` — captures belonging to a project.
- `GET /tags` — list tags with their capture counts.
- `GET /tags/{tag}` — captures carrying a tag.
- `GET /health` — liveness check.

A gateway rejection or outage surfaces as `502`, same as the capture API —
no silent fallback.

## Testing

```sh
uv run pytest services/knowledge/tests
```

Tests run against a real HTTP stand-in for the gateway, not a mocking
library.
