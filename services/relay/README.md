# Event Relay — the Enterprise Event Bus

Fans ledger events out to webhook subscribers, turning the append-only
ledger into the event bus from the architecture: services only ever
*append* events; anything that wants to *react* — an n8n workflow, a
dashboard, a notifier — subscribes here instead of being called directly.

Delivery semantics:

- **Ordered, at-least-once.** Events deliver oldest-first per
  subscription. The cursor only advances on a 2xx from the subscriber, so
  a failed delivery stops that subscription's batch and the same event is
  retried on the next `/deliver` run. Subscribers should be idempotent on
  the event `id`.
- **Filtered.** A subscription can name the `event_types` it wants
  (e.g. only `CaptureCreated`); empty means everything.
- **Logged.** Every attempt is recorded and queryable via `/deliveries`.

Reads come from the ledger through the gateway (reader key), same
governance as every other service.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=<reader-role key> \
uv run uvicorn services.relay.app:app --reload --port 8006
```

Trigger `POST /deliver` on a schedule (cron, or an n8n Schedule Trigger
that calls it — fitting).

## API

- `POST /subscriptions` — `{"url": "...", "event_types": ["CaptureCreated"]}` (empty list = all events).
- `GET /subscriptions` / `DELETE /subscriptions/{id}`.
- `POST /deliver` — fetch new ledger events and push them to every matching subscription. Returns `{"subscriptions", "delivered", "failed"}`.
- `GET /deliveries?subscription_id=` — delivery log, newest first.
- `GET /health`.

## n8n integration

n8n connects on both sides of the bus:

**Inbound (react to platform events):** create an n8n workflow starting
with a Webhook trigger, then subscribe it:

```sh
curl -X POST localhost:8006/subscriptions -H 'content-type: application/json' \
  -d '{"url": "http://localhost:5678/webhook/sovereign-capture", "event_types": ["CaptureCreated"]}'
```

Each matching ledger event arrives as the webhook's JSON body (`id`,
`type`, `source`, `payload`, `correlation_id`, `ts`, `hash`, ...).

**Outbound (act on the platform):** use ordinary n8n HTTP Request nodes
against the gateway (`POST /events`, with a service key), the capture API,
the knowledge service, or the orchestrator (`POST /tasks`).

Ready-made importable workflows live in [`examples/n8n/`](../../examples/n8n):

- `capture-to-github-issue.json` — every `CaptureCreated` becomes a GitHub
  issue (the last step of the architecture's "idea -> ... -> GitHub Issue"
  pipeline).
- `failure-to-diagnosis.json` — every `AgentFailed` dispatches a
  `diagnosis` task through Manus Prime (self-healing loop; pairs with a
  Failure Analyst / DRQuinn-style agent registered for `diagnosis`).

## Testing

```sh
uv run pytest services/relay/tests
```

Tests run against a real HTTP stand-in gateway and a real webhook
receiver, covering ordering, filtering, incremental cursors, failure
retry, and per-subscription independence.
