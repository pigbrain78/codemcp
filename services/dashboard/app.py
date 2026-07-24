"""Dashboard -- the Human Workspace projection.

A read-only, single-page view of the whole platform: service health from
the Digital Twin, ledger hash-chain status, the agent fleet and its
capabilities, Ghost Team observations, Evolution proposals and gene
reputations, the recent event feed, and the heartbeat's pulse.

Per the constitution (SOVEREIGN.md, Layer 7): interfaces are projections,
never the source of truth. This service aggregates reads server-side (so
API keys stay out of the browser) and takes no write actions at all --
approvals, acknowledgements, and dispatches happen through the owning
services.

Unlike the platform's writing services, a partially-down platform is
exactly when a dashboard matters most, so /overview degrades per section:
an unreachable backend yields {"error": ...} for that section while the
rest render. The degradation is explicit in the payload, never silent.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
TWIN_URL = os.environ.get("TWIN_URL", "http://localhost:8009")
GHOST_URL = os.environ.get("GHOST_URL", "http://localhost:8008")
EVOLUTION_URL = os.environ.get("EVOLUTION_URL", "http://localhost:8007")
HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "http://localhost:8010")

EVENT_FEED_LIMIT = int(os.environ.get("EVENT_FEED_LIMIT", "25"))

app = FastAPI(title="Project Sovereign Dashboard", version="0.1.0")


async def _fetch(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    authed: bool = False,
) -> Any:
    headers = {"Authorization": f"Bearer {GATEWAY_API_KEY}"} if authed else {}
    try:
        response = await client.get(
            url, params=params or {}, headers=headers, timeout=10.0
        )
    except httpx.RequestError as exc:
        return {"error": f"unreachable: {exc}"}
    if response.status_code != 200:
        return {"error": f"{response.status_code}: {response.text[:200]}"}
    return response.json()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/overview")
async def overview() -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        (
            events,
            verify,
            model,
            observations,
            proposals,
            genes,
            heartbeat,
        ) = await asyncio.gather(
            _fetch(client, f"{GATEWAY_URL}/events", {"limit": 1000}, authed=True),
            _fetch(client, f"{GATEWAY_URL}/verify", authed=True),
            _fetch(client, f"{TWIN_URL}/model"),
            _fetch(client, f"{GHOST_URL}/observations", {"status": "open"}),
            _fetch(client, f"{EVOLUTION_URL}/proposals", {"status": "proposed"}),
            _fetch(client, f"{EVOLUTION_URL}/genes"),
            _fetch(client, f"{HEARTBEAT_URL}/status"),
        )

    if isinstance(events, list):
        recent = events[-EVENT_FEED_LIMIT:][::-1]
        event_feed: Any = recent
        total_events = len(events)
    else:
        event_feed = events
        total_events = None

    return {
        "ledger": {"verify": verify, "total_events_fetched": total_events},
        "events": event_feed,
        "twin": model,
        "observations": observations,
        "proposals": proposals,
        "genes": genes,
        "heartbeat": heartbeat,
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project Sovereign</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b0d12; color: #e6e8ee;
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  header { padding: 20px 24px; border-bottom: 1px solid #1c2230;
           display: flex; align-items: baseline; gap: 14px; }
  h1 { font-size: 18px; margin: 0; }
  h1 span { background: linear-gradient(90deg,#8b5cf6,#38bdf8);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  #chain { font-size: 13px; color: #9aa3b2; }
  #chain.ok { color: #4ade80; } #chain.bad { color: #f87171; }
  main { display: grid; gap: 16px; padding: 20px 24px 60px;
         grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
  section { background: #12151d; border: 1px solid #232837; border-radius: 12px;
            padding: 16px 18px; overflow-x: auto; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
       color: #9aa3b2; margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { padding: 5px 8px; border-bottom: 1px solid #1a1f2b; text-align: left;
           vertical-align: top; }
  th { color: #6b7383; font-weight: 600; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         margin-right: 7px; }
  .healthy { background: #4ade80; } .unhealthy { background: #f87171; }
  .unknown { background: #6b7383; }
  .warn { color: #facc15; } .muted { color: #6b7383; }
  .pill { font-size: 11px; border: 1px solid #2a3145; border-radius: 999px;
          padding: 1px 8px; color: #9aa3b2; margin-left: 6px; }
  .sole { color: #facc15; border-color: rgba(250,204,21,.4); }
  .err { color: #f87171; font-size: 13px; }
  .feed div { padding: 4px 0; border-bottom: 1px solid #1a1f2b; font-size: 12.5px; }
  .feed b { color: #38bdf8; font-weight: 600; }
  .updated { color: #6b7383; font-size: 12px; margin-left: auto; }
</style>
</head>
<body>
<header>
  <h1><span>Project Sovereign</span></h1>
  <div id="chain">checking ledger…</div>
  <div class="updated" id="updated"></div>
</header>
<main id="main"></main>
<script>
const el = (t, attrs, html) => { const e = document.createElement(t);
  Object.assign(e, attrs || {}); if (html !== undefined) e.innerHTML = html; return e; };
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function section(title, bodyHtml) {
  return `<section><h2>${title}</h2>${bodyHtml}</section>`;
}
function errBox(o) { return `<div class="err">${esc(o.error)}</div>`; }

function servicesHtml(twin) {
  if (twin.error) return errBox(twin);
  const rows = twin.services.map(s => `<tr>
    <td><span class="dot ${esc(s.health)}"></span>${esc(s.name)}</td>
    <td class="muted">${esc(s.kind)}</td>
    <td>${s.event_count}</td>
    <td class="muted">${esc((s.depends_on||[]).join(", "))}</td></tr>`).join("");
  return `<table><tr><th>service</th><th>kind</th><th>events</th><th>depends on</th></tr>${rows}</table>`;
}
function fleetHtml(twin) {
  if (twin.error) return errBox(twin);
  const agents = twin.agents.map(a =>
    `<tr><td>${esc(a.name)}</td><td class="muted">${esc(a.capabilities.join(", "))}</td></tr>`).join("");
  const caps = twin.capabilities.map(c =>
    `<tr><td>${esc(c.name)}${c.sole_provider ? '<span class="pill sole">sole provider</span>' : ""}</td>
     <td class="muted">${esc(c.providers.join(", "))}</td></tr>`).join("");
  return `<table><tr><th>agent</th><th>capabilities</th></tr>${agents}</table>
    <h2 style="margin-top:14px">capabilities</h2>
    <table><tr><th>capability</th><th>providers</th></tr>${caps}</table>`;
}
function obsHtml(obs) {
  if (obs.error) return errBox(obs);
  if (!obs.length) return '<div class="muted">The council sees nothing amiss.</div>';
  return obs.map(o => `<div style="margin-bottom:10px">
    <b>${esc(o.observer)}</b> <span class="pill">${esc(o.kind)}</span>
    ${o.severity === "warning" ? '<span class="pill warn">warning</span>' : ""}
    <span class="pill">x${o.occurrences}</span>
    <div>${esc(o.summary)}</div>
    <div class="muted">${esc(o.recommendation)}</div></div>`).join("");
}
function propsHtml(props) {
  if (props.error) return errBox(props);
  if (!props.length) return '<div class="muted">No proposals awaiting review.</div>';
  return props.map(p => `<div style="margin-bottom:10px">
    <span class="pill">${esc(p.kind)}</span> <span class="pill">${esc(p.confidence)}</span>
    <div>${esc(p.summary)}</div>
    <div class="muted">impact: ${esc(p.predicted_impact)}</div></div>`).join("");
}
function genesHtml(genes) {
  if (genes.error) return errBox(genes);
  if (!genes.length) return '<div class="muted">No architectural genes yet.</div>';
  const rows = genes.map(g => `<tr><td>${esc(g.name)}</td>
    <td>${g.reputation === null ? '<span class="muted">unproven</span>'
        : Math.round(g.reputation * 100) + "%"}</td>
    <td>${g.reuse_count}</td></tr>`).join("");
  return `<table><tr><th>gene</th><th>reputation</th><th>reuses</th></tr>${rows}</table>`;
}
function feedHtml(events) {
  if (events.error) return errBox(events);
  return '<div class="feed">' + events.map(e =>
    `<div><b>${esc(e.type)}</b> <span class="muted">from ${esc(e.source)} · seq ${e.seq}</span></div>`
  ).join("") + "</div>";
}
function pulseHtml(hb) {
  if (hb.error) return errBox(hb);
  const rows = hb.targets.map(t => `<tr>
    <td><span class="dot ${t.last_status === "ok" ? "healthy" : t.last_status === "never" ? "unknown" : "unhealthy"}"></span>${esc(t.name)}</td>
    <td>${t.runs}</td><td>${t.failures}</td>
    <td class="muted">${esc(t.last_status)}</td></tr>`).join("");
  return `<div class="muted" style="margin-bottom:8px">tick ${hb.ticks} · every ${hb.tick_seconds}s</div>
    <table><tr><th>target</th><th>runs</th><th>fails</th><th>last</th></tr>${rows}</table>`;
}

async function refresh() {
  const res = await fetch("/overview");
  const o = await res.json();

  const chain = document.getElementById("chain");
  if (o.ledger.verify && o.ledger.verify.valid === true) {
    chain.textContent = `ledger chain VALID · ${o.ledger.verify.count} events`;
    chain.className = "ok";
  } else if (o.ledger.verify && o.ledger.verify.valid === false) {
    chain.textContent = `LEDGER CHAIN BROKEN at seq ${o.ledger.verify.broken_at_seq}`;
    chain.className = "bad";
  } else {
    chain.textContent = "ledger unreachable";
    chain.className = "bad";
  }

  document.getElementById("main").innerHTML =
    section("Services", servicesHtml(o.twin)) +
    section("Fleet", fleetHtml(o.twin)) +
    section("Ghost Team — open observations", obsHtml(o.observations)) +
    section("Evolution — proposals awaiting review", propsHtml(o.proposals)) +
    section("Architecture genome", genesHtml(o.genes)) +
    section("Heartbeat", pulseHtml(o.heartbeat)) +
    section("Recent events", feedHtml(o.events));
  document.getElementById("updated").textContent =
    "updated " + new Date().toLocaleTimeString();
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE
