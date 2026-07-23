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

The matching **API Gateway** (auth, routing, audit logging, event dispatch)
lives in the `mcp-server-js` repo at `services/gateway/`, since it's a
Node/TypeScript-ecosystem component. The gateway is the *only* thing
allowed to write to the ledger, and reads from it are expected to flow
through the gateway too — every service here goes through it rather than
calling the ledger directly.

Phase 1's infra provisioning (Tailscale, a GCP project, Cloud SQL) is
outside this repo's scope, not something to scaffold speculatively here.
Phase 2's remaining piece is Notion sync — see the `mcp-server-js`
`services/README.md` for status on that.
