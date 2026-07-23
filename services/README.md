# Project Sovereign — Foundation Services

Standalone services for the Project Sovereign platform, kept separate from
the `codemcp` package itself (different lifecycle, different deployable).
The architectural constitution governing all of this lives at
[`SOVEREIGN.md`](../SOVEREIGN.md) — new components must align with it.

- [`ledger/`](./ledger) — the append-only, hash-chained event ledger (the
  "Enterprise Memory Layer" ledger). Phase 1.
- [`capture/`](./capture) — the Pocket OS capture API: classifies incoming
  captures and forwards them to the gateway as `CaptureCreated` events.
  Phase 2.
- [`knowledge/`](./knowledge) — the knowledge graph / search service:
  indexes captures from the ledger (read via the gateway) into full-text
  search plus a project/tag graph. Phase 2.
- [`notion/`](./notion) — Notion sync: upserts a Notion page per project
  from the knowledge service's data, so Notion reflects the ledger without
  manual copy-paste. Phase 2. Talks to the real Notion API using a token
  *you* provide when you run it — it has no access to any workspace by
  default.
- [`orchestrator/`](./orchestrator) — Manus Prime: agents register as HTTP
  endpoints with declared capabilities, tasks dispatch through a single
  front door, and every request/completion/failure is recorded on the
  ledger. Phase 3.
- [`relay/`](./relay) — the Enterprise Event Bus: fans ledger events out
  to webhook subscribers (n8n workflows and friends) with ordered,
  at-least-once delivery, per-subscription filters and cursors, and a
  delivery log. Importable n8n workflows live in
  [`examples/n8n/`](../examples/n8n).
- [`evolution/`](./evolution) — the Evolution Engine: Evolution Records
  (how the platform learned, with evidence), architectural genes with
  lineage and evidence-based reputation, and a nightly reflection that
  answers the constitution's five questions and produces human-approvable
  proposals. Nothing self-executes.
- [`ghost/`](./ghost) — the Ghost Team: quiet observers (recurring
  failures, duplicated work, unused agents, undocumented lessons) that
  notice and recommend, deduplicated by signature so they never nag.
  Nothing self-executes here either.

The matching **API Gateway** (auth, routing, audit logging, event dispatch)
lives in the `mcp-server-js` repo at `services/gateway/`, since it's a
Node/TypeScript-ecosystem component. The gateway is the *only* thing
allowed to write to the ledger; `capture/` and `knowledge/` go through it
rather than calling the ledger directly. `notion/` sits downstream of
`knowledge/` and doesn't touch the ledger or gateway at all.

Phase 1's infra provisioning (Tailscale, a GCP project, Cloud SQL) is
outside this repo's scope, not something to scaffold speculatively here.
Phases 2 and 3's code-only pieces are in place; Phase 3's remaining work
is pointing real specialist agents (Nebula AI or otherwise) at the
orchestrator's agent contract — see `orchestrator/README.md`.

## Running the whole stack

Two ways:

- **docker compose** — from the repo root (with `mcp-server-js` checked
  out as a sibling): `docker compose up --build`. See the comments in
  [`docker-compose.yml`](../docker-compose.yml) for the dev API keys and
  the optional `notion` profile.
- **Bare processes** — each service's README shows its `uv run uvicorn`
  invocation; the gateway runs with `node src/index.js`.

[`tests_e2e/`](./tests_e2e) boots the five core services as real
processes plus a toy specialist agent and drives one idea through the
entire pipeline (capture → ledger → knowledge search → agent dispatch →
hash-chain verify). It runs as part of the normal test suite when `node`
and the sibling `mcp-server-js` checkout are present, and skips with an
explicit reason otherwise.
