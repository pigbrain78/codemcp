# Notion Sync

Synchronizes memory into Notion, per the architecture: "Notion becomes
your enterprise workspace... think of it as the user-facing workspace
rather than the database of record." This service is one-directional
(knowledge graph -> Notion): `POST /sync` reads project summaries from the
**knowledge service** and upserts one Notion page per project, so a
project's page always reflects what's actually in the ledger.

This service talks to the real Notion REST API using an integration token
you provide — it doesn't ship with or assume access to any particular
workspace. Tests run entirely against a fake stand-in HTTP server, no live
Notion account involved.

## Setting up the Notion side

1. Create a [Notion integration](https://www.notion.so/my-integrations) and copy its token.
2. Create (or pick) a Notion database and share it with that integration.
3. The database needs three properties:
   - A **title** property (default name `Name`; override with `NOTION_TITLE_PROPERTY` if yours is named differently — Notion databases don't always call it "Name").
   - A **number** property named `Captures`.
   - A **multi-select** property named `Tags`.

## Running

```sh
KNOWLEDGE_URL=http://localhost:8003 \
NOTION_API_TOKEN=secret_xxx \
NOTION_DATABASE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
uv run uvicorn services.notion.app:app --reload --port 8004
```

Call `POST /sync` periodically (cron, or manually) to push the latest
project/capture-count/tag state to Notion.

## API

- `POST /sync` — upsert a Notion page per project. Returns `{"projects_seen": int, "pages_created": int, "pages_updated": int}`.
- `GET /health` — liveness check.

A knowledge-service or Notion API failure surfaces as `502` — no silent
fallback.

## Testing

```sh
uv run pytest services/notion/tests
```
