# Manus Prime — Orchestrator

The "Orchestration Layer" from the Project Sovereign architecture: agent
dispatch through a single front door, with every action persisted to the
ledger. This is the Phase 3 core — "Connect your existing Nebula AI agents
through a common orchestration interface... Centralize agent dispatch
through Manus Prime... Persist all agent activity to the ledger."

## How agents plug in

An agent is any HTTP service exposing a `POST` endpoint that accepts:

```json
{"task_id": "…", "capability": "…", "input": { }}
```

and returns `200` with `{"output": { }}`. Register it with:

```sh
curl -X POST localhost:8005/agents -H 'content-type: application/json' \
  -d '{"name": "ralph5", "endpoint": "http://ralph5.internal/run", "capabilities": ["codegen", "refactor"]}'
```

That's the whole contract — Dr. Quinn, Igor OS, Ralph5, Play OS, Kid OS,
Dreamweaver, AgriForge, or any future specialist joins the platform by
speaking it. Re-registering the same name updates the endpoint and
capability list in place.

## Dispatch

`POST /tasks` with `{"capability": "codegen", "input": {…}}` selects the
first registered agent declaring that capability (or a specific one via
`"agent": "name"`), calls it, and records the outcome. Every dispatch
writes a full lifecycle to the ledger via the gateway, correlated by task
id:

- `AgentRequested` → before the call
- `AgentCompleted` (with output) or `AgentFailed` (with error) → after

Agent registrations are also recorded (`AgentRegistered`). A gateway that
can't be reached is treated as a governance failure and surfaces as `502` —
no dispatch happens without its audit trail.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=manus-key \
uv run uvicorn services.orchestrator.app:app --reload --port 8005
```

The gateway needs a `service`-role key for Manus Prime, e.g.:

```json
{"manus-key": {"role": "service", "name": "manus-prime"}}
```

## API

- `POST /agents` — register (or update) an agent: `{"name", "endpoint", "capabilities"}`.
- `GET /agents` — list registered agents.
- `POST /tasks` — dispatch: `{"capability", "input", "agent"?}`. Returns the finished task with `status` `completed` or `failed`.
- `GET /tasks?status=` — list tasks, newest first.
- `GET /tasks/{id}` — fetch one task.
- `GET /health` — liveness check.

## Testing

```sh
uv run pytest services/orchestrator/tests
```

Tests run against real HTTP stand-ins for the gateway and for agents
(success, failure, and unreachable modes) — no mocking libraries.
