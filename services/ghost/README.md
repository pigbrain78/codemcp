# Ghost Team

The constitution's quiet advisory council ([`SOVEREIGN.md`](../../SOVEREIGN.md),
Initiative 4): observers that continuously watch the platform's event
stream and *notice* — never interrupt, never change anything. Each notice
becomes an observation with evidence and a recommendation, and brand-new
observations are recorded on the ledger as `GhostObservation` events.

## The observers

| Observer | Kind | Notices |
|---|---|---|
| `dr-quinn` | `recurring_failure` | The same failure signature (agent + capability + error) happening again — "we've seen this before" (warning) |
| `ralph5` | `duplicate_work` | An identical task (capability + input) dispatched more than once — "you're redoing work" |
| `igor` | `unused_agent` | An agent registered but never routed to, after `UNUSED_AGENT_WINDOW` platform events pass it by |
| `kid` | `missing_docs` | An Evolution Record filed without reuse-potential notes — a lesson that can't become a gene |

Design rules, straight from the constitution:

- **They only recommend.** Observations carry a recommendation; humans
  acknowledge or dismiss. Nothing self-executes.
- **They don't nag.** Observations are deduplicated by signature: a
  repeat sighting bumps `occurrences` and appends evidence quietly — no
  new ledger event, no new row.
- **They don't watch themselves.** `GhostObservation` events and gateway
  audit noise are excluded from observation, so the council can't
  feedback-loop on its own output.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=<service-role key> \
uv run uvicorn services.ghost.app:app --reload --port 8008
```

Run `POST /observe` frequently (every few minutes via cron or an n8n
Schedule Trigger) — it's incremental (cursor over ledger history) and
cheap when nothing new happened.

## API

- `POST /observe` — pull new ledger events, run all observers. Returns `{"events_examined", "new_observations", "updated_observations"}`.
- `GET /observations?observer=&status=&severity=` — the council's current notices, most recent first.
- `POST /observations/{id}/acknowledge` / `POST /observations/{id}/dismiss`.
- `GET /health`.

## Testing

```sh
uv run pytest services/ghost/tests
```

Tests seed realistic ledger history and verify each observer's trigger,
signature deduplication (repeat sightings update quietly, no ledger
spam), the unused-agent window, incremental cursors, and the
acknowledge/dismiss flow.
