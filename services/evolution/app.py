"""Evolution Engine -- the platform's self-reflection subsystem.

Implements the constitution's Evolution Ledger, Architecture Genome, and
Evolution Engine initiatives (SOVEREIGN.md):

- **Evolution Records**: first-class "the platform learned something"
  artifacts (problem, evidence, solution, confidence, impact, rollback,
  affected systems, reuse potential), each mirrored to the append-only
  ledger as an EvolutionRecorded event.
- **Architectural genes**: reusable patterns with lineage (origin record),
  reuse history, and evidence-based reputation from recorded outcomes.
- **Reflection**: POST /reflect reads ledger history through the gateway
  and answers the five questions -- what repeated, what improved, what
  failed repeatedly, what should become reusable, what should change next
  -- producing evidence-backed proposals. Proposals are never executed;
  humans approve or reject them (governance precedes execution).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
DB_PATH = os.environ.get("EVOLUTION_DB_PATH", "evolution.db")

FETCH_LIMIT = 1000
REPEAT_THRESHOLD = 3
FAILURE_THRESHOLD = 2

# Governance/audit noise is not "work repeating"; reflection ignores it.
NOISE_EVENT_TYPES = {"GatewayRequestHandled"}

Confidence = Literal["low", "medium", "high"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                problem TEXT NOT NULL,
                evidence TEXT NOT NULL,
                solution TEXT NOT NULL,
                confidence TEXT NOT NULL,
                impact TEXT NOT NULL,
                rollback TEXT NOT NULL,
                affected_systems TEXT NOT NULL,
                reuse_potential TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS genes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                rationale TEXT NOT NULL,
                origin_record_id TEXT,
                retired INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gene_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gene_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                outcome TEXT,
                note TEXT,
                project TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                reflection_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence TEXT NOT NULL,
                predicted_impact TEXT NOT NULL,
                confidence TEXT NOT NULL,
                rollback TEXT NOT NULL,
                affected_systems TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reflections (
                id TEXT PRIMARY KEY,
                cursor_ts TEXT NOT NULL,
                report TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_append(
    event_type: str, payload: dict[str, Any], correlation_id: str
) -> None:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/events",
            json={
                "type": event_type,
                "source": "evolution-engine",
                "payload": payload,
                "correlation_id": correlation_id,
            },
            headers={"Authorization": f"Bearer {GATEWAY_API_KEY}"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway unavailable: {exc}"
        ) from exc
    if response.status_code != 201:
        raise HTTPException(
            status_code=502,
            detail=f"gateway rejected {event_type}: {response.status_code} {response.text}",
        )


def _ledger_events(since: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": FETCH_LIMIT}
    if since:
        params["since"] = since
    try:
        response = httpx.get(
            f"{GATEWAY_URL}/events",
            params=params,
            headers={"Authorization": f"Bearer {GATEWAY_API_KEY}"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway unavailable: {exc}"
        ) from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"gateway rejected read: {response.status_code} {response.text}",
        )
    return response.json()


class EvolutionRecordIn(BaseModel):
    problem: str = Field(min_length=1)
    evidence: list[str] = Field(
        min_length=1,
        description="Event ids, metrics, or observations backing this record",
    )
    solution: str = Field(min_length=1)
    confidence: Confidence
    impact: str = Field(min_length=1)
    rollback: str = Field(min_length=1)
    affected_systems: list[str] = Field(default_factory=list)
    reuse_potential: str = ""
    source: str = "human"


class EvolutionRecordOut(EvolutionRecordIn):
    id: str
    created_at: str


class GeneIn(BaseModel):
    name: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    origin_record_id: str | None = None


class GeneOut(BaseModel):
    id: str
    name: str
    rationale: str
    origin_record_id: str | None
    retired: bool
    created_at: str
    reuse_count: int
    successes: int
    failures: int
    reputation: float | None


class GeneReuse(BaseModel):
    project: str = Field(min_length=1)
    note: str = ""


class GeneOutcome(BaseModel):
    outcome: Literal["success", "failure"]
    note: str = ""


class Proposal(BaseModel):
    id: str
    reflection_id: str
    kind: str
    summary: str
    evidence: list[str]
    predicted_impact: str
    confidence: Confidence
    rollback: str
    affected_systems: list[str]
    status: str
    created_at: str


class ReflectionReport(BaseModel):
    id: str
    what_repeated: list[dict[str, Any]]
    what_improved: list[dict[str, Any]]
    what_failed: list[dict[str, Any]]
    reuse_candidates: list[dict[str, Any]]
    proposals: list[Proposal]
    events_examined: int
    created_at: str


def _row_to_record(row: sqlite3.Row) -> EvolutionRecordOut:
    return EvolutionRecordOut(
        id=row["id"],
        problem=row["problem"],
        evidence=json.loads(row["evidence"]),
        solution=row["solution"],
        confidence=row["confidence"],
        impact=row["impact"],
        rollback=row["rollback"],
        affected_systems=json.loads(row["affected_systems"]),
        reuse_potential=row["reuse_potential"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        reflection_id=row["reflection_id"],
        kind=row["kind"],
        summary=row["summary"],
        evidence=json.loads(row["evidence"]),
        predicted_impact=row["predicted_impact"],
        confidence=row["confidence"],
        rollback=row["rollback"],
        affected_systems=json.loads(row["affected_systems"]),
        status=row["status"],
        created_at=row["created_at"],
    )


def _gene_out(conn: sqlite3.Connection, row: sqlite3.Row) -> GeneOut:
    stats = conn.execute(
        """
        SELECT
            SUM(CASE WHEN kind = 'reuse' THEN 1 ELSE 0 END) AS reuses,
            SUM(CASE WHEN kind = 'outcome' AND outcome = 'success' THEN 1 ELSE 0 END) AS successes,
            SUM(CASE WHEN kind = 'outcome' AND outcome = 'failure' THEN 1 ELSE 0 END) AS failures
        FROM gene_events WHERE gene_id = ?
        """,
        (row["id"],),
    ).fetchone()
    successes = stats["successes"] or 0
    failures = stats["failures"] or 0
    total = successes + failures
    return GeneOut(
        id=row["id"],
        name=row["name"],
        rationale=row["rationale"],
        origin_record_id=row["origin_record_id"],
        retired=bool(row["retired"]),
        created_at=row["created_at"],
        reuse_count=stats["reuses"] or 0,
        successes=successes,
        failures=failures,
        reputation=round(successes / total, 3) if total else None,
    )


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Project Sovereign Evolution Engine", version="0.1.0", lifespan=_lifespan
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Evolution Records (the Evolution Ledger) ---


@app.post("/records", response_model=EvolutionRecordOut, status_code=201)
def create_record(record: EvolutionRecordIn) -> EvolutionRecordOut:
    out = EvolutionRecordOut(
        id=str(uuid.uuid4()), created_at=_now(), **record.model_dump()
    )
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO records
                (id, problem, evidence, solution, confidence, impact, rollback,
                 affected_systems, reuse_potential, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                out.id,
                out.problem,
                json.dumps(out.evidence),
                out.solution,
                out.confidence,
                out.impact,
                out.rollback,
                json.dumps(out.affected_systems),
                out.reuse_potential,
                out.source,
                out.created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _ledger_append("EvolutionRecorded", out.model_dump(), correlation_id=out.id)
    return out


@app.get("/records", response_model=list[EvolutionRecordOut])
def list_records() -> list[EvolutionRecordOut]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM records ORDER BY created_at DESC").fetchall()
        return [_row_to_record(row) for row in rows]
    finally:
        conn.close()


# --- Architectural Genes (the Architecture Genome) ---


@app.post("/genes", response_model=GeneOut, status_code=201)
def create_gene(gene: GeneIn) -> GeneOut:
    gene_id = str(uuid.uuid4())
    created_at = _now()
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM genes WHERE name = ?", (gene.name,)
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409, detail=f"gene already exists: {gene.name}"
            )
        conn.execute(
            "INSERT INTO genes (id, name, rationale, origin_record_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (gene_id, gene.name, gene.rationale, gene.origin_record_id, created_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM genes WHERE id = ?", (gene_id,)).fetchone()
        out = _gene_out(conn, row)
    finally:
        conn.close()

    _ledger_append(
        "GeneCreated",
        {
            "gene": gene.name,
            "rationale": gene.rationale,
            "origin_record_id": gene.origin_record_id,
        },
        correlation_id=gene_id,
    )
    return out


@app.get("/genes", response_model=list[GeneOut])
def list_genes(include_retired: bool = False) -> list[GeneOut]:
    conn = _connect()
    try:
        where = "" if include_retired else "WHERE retired = 0"
        rows = conn.execute(
            f"SELECT * FROM genes {where} ORDER BY created_at ASC"
        ).fetchall()
        return [_gene_out(conn, row) for row in rows]
    finally:
        conn.close()


def _get_gene_row(conn: sqlite3.Connection, gene_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM genes WHERE id = ?", (gene_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="gene not found")
    return row


@app.post("/genes/{gene_id}/reuse", response_model=GeneOut)
def reuse_gene(gene_id: str, reuse: GeneReuse) -> GeneOut:
    conn = _connect()
    try:
        row = _get_gene_row(conn, gene_id)
        conn.execute(
            "INSERT INTO gene_events (gene_id, kind, project, note, created_at) VALUES (?, 'reuse', ?, ?, ?)",
            (gene_id, reuse.project, reuse.note, _now()),
        )
        conn.commit()
        out = _gene_out(conn, row)
    finally:
        conn.close()

    _ledger_append(
        "GeneReused",
        {"gene": out.name, "project": reuse.project, "note": reuse.note},
        correlation_id=gene_id,
    )
    return out


@app.post("/genes/{gene_id}/outcomes", response_model=GeneOut)
def record_gene_outcome(gene_id: str, outcome: GeneOutcome) -> GeneOut:
    conn = _connect()
    try:
        row = _get_gene_row(conn, gene_id)
        conn.execute(
            "INSERT INTO gene_events (gene_id, kind, outcome, note, created_at) VALUES (?, 'outcome', ?, ?, ?)",
            (gene_id, outcome.outcome, outcome.note, _now()),
        )
        conn.commit()
        out = _gene_out(conn, row)
    finally:
        conn.close()

    _ledger_append(
        "GeneOutcomeRecorded",
        {"gene": out.name, "outcome": outcome.outcome, "note": outcome.note},
        correlation_id=gene_id,
    )
    return out


@app.post("/genes/{gene_id}/retire", response_model=GeneOut)
def retire_gene(gene_id: str) -> GeneOut:
    conn = _connect()
    try:
        _get_gene_row(conn, gene_id)
        conn.execute("UPDATE genes SET retired = 1 WHERE id = ?", (gene_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM genes WHERE id = ?", (gene_id,)).fetchone()
        out = _gene_out(conn, row)
    finally:
        conn.close()

    _ledger_append("GeneRetired", {"gene": out.name}, correlation_id=gene_id)
    return out


# --- Reflection (the five questions) ---


def _analyze(events: list[dict[str, Any]], known_genes: set[str]) -> dict[str, Any]:
    meaningful = [e for e in events if e["type"] not in NOISE_EVENT_TYPES]

    type_counts = Counter(e["type"] for e in meaningful)
    what_repeated = [
        {"event_type": t, "count": c}
        for t, c in type_counts.most_common()
        if c >= REPEAT_THRESHOLD
    ]

    what_improved = [
        {
            "kind": e["type"],
            "event_id": e["id"],
            "summary": e["payload"].get("problem") or e["payload"].get("gene") or "",
        }
        for e in meaningful
        if e["type"] in {"EvolutionRecorded", "GeneReused"}
    ]

    failure_counter: Counter[tuple[str, str]] = Counter()
    failure_evidence: dict[tuple[str, str], list[str]] = {}
    for e in meaningful:
        if e["type"] == "AgentFailed":
            key = (e["payload"].get("agent", "?"), e["payload"].get("capability", "?"))
            failure_counter[key] += 1
            failure_evidence.setdefault(key, []).append(e["id"])
    what_failed = [
        {
            "agent": agent,
            "capability": capability,
            "count": count,
            "evidence": failure_evidence[(agent, capability)],
        }
        for (agent, capability), count in failure_counter.most_common()
        if count >= FAILURE_THRESHOLD
    ]

    completed_counter: Counter[str] = Counter()
    completed_evidence: dict[str, list[str]] = {}
    for e in meaningful:
        if e["type"] == "AgentCompleted":
            capability = e["payload"].get("capability", "?")
            completed_counter[capability] += 1
            completed_evidence.setdefault(capability, []).append(e["id"])
    reuse_candidates = [
        {
            "capability": capability,
            "count": count,
            "evidence": completed_evidence[capability],
        }
        for capability, count in completed_counter.most_common()
        if count >= REPEAT_THRESHOLD and f"pattern:{capability}" not in known_genes
    ]

    return {
        "what_repeated": what_repeated,
        "what_improved": what_improved,
        "what_failed": what_failed,
        "reuse_candidates": reuse_candidates,
        "events_examined": len(events),
    }


def _proposals_from_analysis(
    reflection_id: str, analysis: dict[str, Any]
) -> list[Proposal]:
    created_at = _now()
    proposals: list[Proposal] = []

    for failure in analysis["what_failed"]:
        proposals.append(
            Proposal(
                id=str(uuid.uuid4()),
                reflection_id=reflection_id,
                kind="recurring_failure",
                summary=(
                    f"Agent '{failure['agent']}' failed {failure['count']}x on capability "
                    f"'{failure['capability']}'. Investigate root cause or route the "
                    f"capability to a different implementation."
                ),
                evidence=failure["evidence"],
                predicted_impact=f"Eliminate a recurring failure ({failure['count']} occurrences in window)",
                confidence="high"
                if failure["count"] >= 2 * FAILURE_THRESHOLD
                else "medium",
                rollback="Proposal only; no change until approved and implemented",
                affected_systems=[failure["agent"], "orchestrator"],
                status="proposed",
                created_at=created_at,
            )
        )

    for candidate in analysis["reuse_candidates"]:
        proposals.append(
            Proposal(
                id=str(uuid.uuid4()),
                reflection_id=reflection_id,
                kind="gene_candidate",
                summary=(
                    f"Capability '{candidate['capability']}' completed {candidate['count']}x. "
                    f"Extract the repeated solution as architectural gene "
                    f"'pattern:{candidate['capability']}'."
                ),
                evidence=candidate["evidence"],
                predicted_impact="Repeated work becomes a validated, reusable pattern",
                confidence="medium",
                rollback="Retire the gene if its reputation degrades",
                affected_systems=["evolution-engine"],
                status="proposed",
                created_at=created_at,
            )
        )

    return proposals


@app.post("/reflect", response_model=ReflectionReport)
def reflect() -> ReflectionReport:
    conn = _connect()
    try:
        last = conn.execute(
            "SELECT cursor_ts FROM reflections ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        since = last["cursor_ts"] if last else ""
        events = _ledger_events(since)

        gene_rows = conn.execute("SELECT name FROM genes").fetchall()
        known_genes = {row["name"] for row in gene_rows}

        analysis = _analyze(events, known_genes)
        reflection_id = str(uuid.uuid4())
        proposals = _proposals_from_analysis(reflection_id, analysis)

        for proposal in proposals:
            conn.execute(
                """
                INSERT INTO proposals
                    (id, reflection_id, kind, summary, evidence, predicted_impact,
                     confidence, rollback, affected_systems, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.id,
                    proposal.reflection_id,
                    proposal.kind,
                    proposal.summary,
                    json.dumps(proposal.evidence),
                    proposal.predicted_impact,
                    proposal.confidence,
                    proposal.rollback,
                    json.dumps(proposal.affected_systems),
                    proposal.status,
                    proposal.created_at,
                ),
            )

        newest_ts = max((e["ts"] for e in events), default=since)
        report = ReflectionReport(
            id=reflection_id,
            proposals=proposals,
            created_at=_now(),
            **analysis,
        )
        conn.execute(
            "INSERT INTO reflections (id, cursor_ts, report, created_at) VALUES (?, ?, ?, ?)",
            (reflection_id, newest_ts, report.model_dump_json(), report.created_at),
        )
        conn.commit()
    finally:
        conn.close()

    _ledger_append(
        "EvolutionReflectionCompleted",
        {
            "events_examined": report.events_examined,
            "proposals": len(report.proposals),
            "repeated": len(report.what_repeated),
            "failures": len(report.what_failed),
        },
        correlation_id=reflection_id,
    )
    return report


@app.get("/proposals", response_model=list[Proposal])
def list_proposals(status: str | None = None) -> list[Proposal]:
    conn = _connect()
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_proposal(row) for row in rows]
    finally:
        conn.close()


def _set_proposal_status(proposal_id: str, status: str) -> Proposal:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if row["status"] != "proposed":
            raise HTTPException(
                status_code=409, detail=f"proposal already {row['status']}"
            )
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return _row_to_proposal(row)
    finally:
        conn.close()


@app.post("/proposals/{proposal_id}/approve", response_model=Proposal)
def approve_proposal(proposal_id: str) -> Proposal:
    proposal = _set_proposal_status(proposal_id, "approved")
    _ledger_append(
        "EvolutionProposalApproved",
        {"kind": proposal.kind, "summary": proposal.summary},
        correlation_id=proposal.id,
    )
    return proposal


@app.post("/proposals/{proposal_id}/reject", response_model=Proposal)
def reject_proposal(proposal_id: str) -> Proposal:
    proposal = _set_proposal_status(proposal_id, "rejected")
    _ledger_append(
        "EvolutionProposalRejected",
        {"kind": proposal.kind, "summary": proposal.summary},
        correlation_id=proposal.id,
    )
    return proposal
