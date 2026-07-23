# Evolution Engine

The platform's self-reflection subsystem — the constitution's Evolution
Ledger, Architecture Genome, and Evolution Engine initiatives
([`SOVEREIGN.md`](../../SOVEREIGN.md)) in working form. Most systems
record what happened; this records **how the platform became better**,
with evidence, and turns that record into human-approvable proposals.

## The three pieces

**Evolution Records** — first-class "the platform learned" artifacts.
`POST /records` requires the full constitutional schema: problem,
evidence (non-empty — evidence outweighs opinion), solution, confidence,
impact, rollback, affected systems, reuse potential. Every record is
mirrored to the hash-chained ledger as an `EvolutionRecorded` event, so
the story of how the platform improved is as tamper-evident as the
platform's activity.

**Architectural genes** — reusable patterns with lineage and reputation.
`POST /genes` registers a pattern (optionally linked to the Evolution
Record it originated from). `POST /genes/{id}/reuse` tracks adoption;
`POST /genes/{id}/outcomes` records success/failure, from which each
gene's **reputation** (success rate) is computed — ideas earn or lose
standing through evidence over time. Degraded genes are retired, not
deleted: lineage is permanent.

**Reflection** — `POST /reflect` reads new ledger history (through the
gateway, incremental cursor) and answers the five questions:

1. *What repeated?* — event types recurring ≥3x (audit noise excluded)
2. *What improved?* — Evolution Records and gene reuses in the window
3. *What failed repeatedly?* — same agent+capability failing ≥2x, with event-id evidence
4. *What should become reusable?* — capabilities completing ≥3x with no existing gene
5. *What should change next?* — evidence-backed proposals, each with predicted impact, confidence, rollback stance, and affected systems

Proposals are **never executed**. They sit in `proposed` status until a
human approves or rejects them (`POST /proposals/{id}/approve|reject`),
and every decision lands on the ledger. Governance precedes execution.

## Running

```sh
GATEWAY_URL=http://localhost:8080 \
GATEWAY_API_KEY=<service-role key> \
uv run uvicorn services.evolution.app:app --reload --port 8007
```

Run `POST /reflect` nightly (cron or an n8n Schedule Trigger). The
gateway needs a `service`-role key (it both reads and writes events):

```json
{"evolution-key": {"role": "service", "name": "evolution-engine"}}
```

## API summary

- `POST /records`, `GET /records` — the Evolution Ledger
- `POST /genes`, `GET /genes?include_retired=`, `POST /genes/{id}/reuse`, `POST /genes/{id}/outcomes`, `POST /genes/{id}/retire` — the Architecture Genome
- `POST /reflect` — run the five questions, produce proposals
- `GET /proposals?status=`, `POST /proposals/{id}/approve`, `POST /proposals/{id}/reject`
- `GET /health`

## Testing

```sh
uv run pytest services/evolution/tests
```

Tests seed a fake gateway with realistic ledger history and verify each
of the five questions' answers, gene reputation math, incremental
reflection cursors, and the approval flow.
