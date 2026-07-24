# Dashboard

The Human Workspace projection ([`SOVEREIGN.md`](../../SOVEREIGN.md),
Layer 7): one page showing the whole organism at a glance —

- **Services** — health dots, activity, and dependencies from the Digital Twin
- **Fleet** — agents and capabilities, with sole-provider warnings
- **Ghost Team** — open observations with recommendations
- **Evolution** — proposals awaiting human review
- **Architecture genome** — genes with reputation percentages and reuse counts
- **Heartbeat** — the pulse's own run/failure history
- **Recent events** — the live ledger feed, plus a header banner showing
  hash-chain validity (green `VALID`, red `BROKEN at seq N`)

Constitution rules: this is a **projection, never the source of truth**,
and it is **read-only** — approvals, acknowledgements, and dispatches
happen through the owning services. Aggregation happens server-side so
the gateway API key never reaches the browser.

Degradation is deliberate: a dashboard matters most when something is
down, so `/overview` returns `{"error": ...}` for an unreachable section
while the rest render — explicitly, never silently.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=<reader-role key> \
TWIN_URL=http://localhost:8009 \
GHOST_URL=http://localhost:8008 \
EVOLUTION_URL=http://localhost:8007 \
HEARTBEAT_URL=http://localhost:8010 \
uv run uvicorn services.dashboard.app:app --port 8011
```

Open http://localhost:8011 — the page refreshes itself every 10 seconds.

## API

- `GET /` — the dashboard page (self-contained HTML, no build step, no CDN).
- `GET /overview` — the aggregated JSON the page renders.
- `GET /health` — liveness.

## Testing

```sh
uv run pytest services/dashboard/tests
```

Tests cover full aggregation, the capped newest-first event feed, API-key
scoping (only gateway calls carry the key), and per-section degradation
when a backend is down or erroring.
