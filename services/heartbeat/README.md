# Heartbeat

The platform's pulse: turns the individually-passive services into one
running organism. Every tick (default 5 minutes) it drives:

| Target | Endpoint | Effect |
|---|---|---|
| knowledge | `POST /sync` | new captures become searchable |
| twin | `POST /sync` | the structural model tracks reality |
| twin | `POST /probe` | health status stays current |
| ghost | `POST /observe` | the advisory council keeps watching |
| relay | `POST /deliver` | events fan out to n8n and friends |

and every `REFLECT_EVERY_TICKS` ticks (default 288 — daily at 5-minute
ticks) it triggers the Evolution Engine's `POST /reflect`.

Failure semantics: a failed call is recorded and retried on the next
tick — that retry *is* the scheduler's meaning, so one down service never
stops the pulse. The pulse itself is observable at `GET /status`:
per-target run counts, failure counts, last status, and last error
detail.

## Running

```sh
KNOWLEDGE_URL=http://localhost:8003 \
TWIN_URL=http://localhost:8009 \
GHOST_URL=http://localhost:8008 \
RELAY_URL=http://localhost:8006 \
EVOLUTION_URL=http://localhost:8007 \
uv run uvicorn services.heartbeat.app:app --port 8010
```

Config: `TICK_SECONDS` (default 300), `REFLECT_EVERY_TICKS` (default
288), `CALL_TIMEOUT_SECONDS` (default 60).

After first boot, declare the platform's topology to the twin once:

```sh
./scripts/declare_topology.sh                 # bare processes on localhost
TWIN_URL=http://localhost:8009 BASE_MODE=compose ./scripts/declare_topology.sh  # compose
```

## API

- `GET /status` — ticks so far, cadence config, and per-target run/failure history.
- `GET /health` — liveness.

## Testing

```sh
uv run pytest services/heartbeat/tests
```

Tests run the real loop at 50ms ticks against fake services and verify
per-tick cadence, the slower reflect cadence, and that failing or
unreachable targets are recorded without stopping the pulse.
