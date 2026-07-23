# Project Sovereign — Foundation Services

Standalone services for the Project Sovereign platform, kept separate from
the `codemcp` package itself (different lifecycle, different deployable).

- [`ledger/`](./ledger) — the append-only, hash-chained event ledger (the
  "Enterprise Memory Layer" ledger).

The matching **API Gateway** (auth, routing, audit logging, event dispatch)
lives in the `mcp-server-js` repo at `services/gateway/`, since it's a
Node/TypeScript-ecosystem component. The gateway talks to this ledger over
HTTP — it's the only thing allowed to write to it.

This covers the code-only parts of the roadmap's Phase 1 ("Foundation").
The rest of Phase 1 (provisioning Tailscale, a GCP project, and Cloud SQL)
is infrastructure setup outside this repo's scope, not something to
scaffold speculatively here.
