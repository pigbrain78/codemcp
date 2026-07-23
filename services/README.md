# Project Sovereign — Foundation Services

Standalone services for the Project Sovereign platform, kept separate from
the `codemcp` package itself (different lifecycle, different deployable).

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

The matching **API Gateway** (auth, routing, audit logging, event dispatch)
lives in the `mcp-server-js` repo at `services/gateway/`, since it's a
Node/TypeScript-ecosystem component. The gateway is the *only* thing
allowed to write to the ledger; `capture/` and `knowledge/` go through it
rather than calling the ledger directly. `notion/` sits downstream of
`knowledge/` and doesn't touch the ledger or gateway at all.

Phase 1's infra provisioning (Tailscale, a GCP project, Cloud SQL) is
outside this repo's scope, not something to scaffold speculatively here.
That's it for Phase 2's code-only pieces — dashboard sync would be next,
once there's a dashboard to sync to.
