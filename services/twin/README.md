# Digital Twin

The constitution's living structural model ([`SOVEREIGN.md`](../../SOVEREIGN.md)):
services, agents, capabilities, dependencies, activity, and health in one
continuously updated picture — built to answer "if this changes or fails,
what is affected?" *before* the change is made.

## Two sources of truth, merged

- **Declared topology** — `POST /services` registers a service with its
  dependency edges and optional health URL. Declarations are recorded on
  the ledger as `TwinServiceDeclared`.
- **Observed reality** — `POST /sync` reads ledger history (through the
  gateway, incremental cursor) and discovers agents + capabilities from
  `AgentRegistered` events, plus per-source activity (event counts, last
  seen). Sources that emit events but were never declared appear as
  `observed` services with **no invented dependency edges** — the twin
  reports reality; it does not fabricate topology.

## Impact analysis

`GET /impact/{node}` answers by node type:

- **service** — transitive reverse-dependency closure: every declared
  service that would be affected if this one changes or fails.
- **agent** — the capabilities it provides, with explicit notes for any
  capability that would be left with **no remaining provider**.
- **capability** — who provides it.

`GET /model` returns the whole twin. Capabilities are computed from agent
registrations and flag `sole_provider` — single points of failure in the
capability mesh are visible at a glance.

## Health

`POST /probe` checks every declared service's `health_url` and stores
`healthy` / `unhealthy` with a timestamp. Run it on a schedule alongside
`/sync`.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=<service-role key> \
uv run uvicorn services.twin.app:app --reload --port 8009
```

Declare the platform's own topology once (adjust hosts to your deploy):

```sh
for s in '{"name":"ledger","health_url":"http://localhost:8001/health"}' \
         '{"name":"gateway","depends_on":["ledger"],"health_url":"http://localhost:8080/health"}' \
         '{"name":"pocket-os-capture-api","depends_on":["gateway"],"health_url":"http://localhost:8002/health"}' \
         '{"name":"knowledge","depends_on":["gateway"],"health_url":"http://localhost:8003/health"}' \
         '{"name":"manus-prime","depends_on":["gateway"],"health_url":"http://localhost:8005/health"}' \
         '{"name":"relay","depends_on":["gateway"],"health_url":"http://localhost:8006/health"}' \
         '{"name":"evolution-engine","depends_on":["gateway"],"health_url":"http://localhost:8007/health"}' \
         '{"name":"ghost-team","depends_on":["gateway"],"health_url":"http://localhost:8008/health"}' \
         '{"name":"notion","depends_on":["knowledge"],"health_url":"http://localhost:8004/health"}'; do
  curl -sS -X POST localhost:8009/services -H 'content-type: application/json' -d "$s" > /dev/null
done
```

## API

- `POST /services` — declare/update a service: `{"name", "kind"?, "depends_on"?, "health_url"?}`.
- `POST /sync` — ingest new ledger events into the model.
- `POST /probe` — health-check all declared services with a `health_url`.
- `GET /model` — the full twin (services, agents, capabilities).
- `GET /impact/{node}` — impact analysis for a service, agent, or capability.
- `GET /health`.

## Testing

```sh
uv run pytest services/twin/tests
```

Tests cover declared-vs-observed merging, incremental sync, transitive
impact closure, sole-provider capability detection, health probing
(healthy, unhealthy, and unreachable), and unknown-node handling.
