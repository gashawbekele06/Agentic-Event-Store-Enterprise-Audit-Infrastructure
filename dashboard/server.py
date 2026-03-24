"""
Apex Financial Services — Loan Pipeline Dashboard API

Run:
    uv run uvicorn dashboard.server:app --reload --port 8000
Open: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import uuid as _uuid_mod
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from src.event_store import EventStore
from src.projections import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection

STATIC_DIR = Path(__file__).parent
DB_URL = os.getenv("DATABASE_URL", "")

_store: EventStore | None = None
_daemon: ProjectionDaemon | None = None
_daemon_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _daemon, _daemon_task

    _store = await EventStore.create(DB_URL)

    projections = [
        ApplicationSummaryProjection(),
        ComplianceAuditViewProjection(),
        AgentPerformanceLedgerProjection(),
    ]
    for p in projections:
        await p.ensure_schema(_store)

    _daemon = ProjectionDaemon(_store, projections)
    _daemon_task = asyncio.create_task(_daemon.run_forever(poll_interval_ms=2000))

    yield

    _daemon.stop()
    if _daemon_task:
        _daemon_task.cancel()
    await _store.close()


app = FastAPI(title="Apex Dashboard", lifespan=lifespan)


# ── JSON serialization ────────────────────────────────────────────────────────

def _serialize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    return obj


def _resp(data: Any) -> JSONResponse:
    return JSONResponse(content=_serialize(data))


# ── Static ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    try:
        async with _store._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                                AS total,
                    COUNT(*) FILTER (WHERE state = 'FINAL_APPROVED')       AS approved,
                    COUNT(*) FILTER (WHERE state = 'FINAL_DECLINED')       AS declined,
                    COUNT(*) FILTER (WHERE decision = 'REFER')             AS referred,
                    COUNT(*) FILTER (WHERE compliance_status = 'BLOCKED')  AS compliance_blocked,
                    COUNT(*) FILTER (WHERE fraud_score > 0.6)              AS high_fraud,
                    ROUND(AVG(fraud_score) FILTER (
                        WHERE fraud_score IS NOT NULL), 3)                 AS avg_fraud_score
                FROM projection_application_summary
                """
            )
        return _resp(dict(row) if row else {})
    except Exception as e:
        return _resp({"error": str(e), "total": 0})


# ── Applications ──────────────────────────────────────────────────────────────

@app.get("/api/applications")
async def list_applications(state: str | None = None):
    try:
        async with _store._pool.acquire() as conn:
            if state:
                rows = await conn.fetch(
                    "SELECT * FROM projection_application_summary "
                    "WHERE state=$1 ORDER BY created_at DESC",
                    state,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM projection_application_summary ORDER BY created_at DESC"
                )
        return _resp([dict(r) for r in rows])
    except Exception:
        return _resp([])


@app.get("/api/applications/{app_id}")
async def get_application(app_id: str):
    async with _store._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM projection_application_summary WHERE application_id=$1",
            app_id,
        )
    if not row:
        raise HTTPException(404, f"Application {app_id} not found")
    return _resp(dict(row))


# ── Events timeline ───────────────────────────────────────────────────────────

@app.get("/api/applications/{app_id}/events")
async def get_application_events(app_id: str):
    """All events across every stream related to this application, in global order."""
    async with _store._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT stream_id, stream_position, global_position,
                   event_type, event_version, payload, recorded_at
            FROM events
            WHERE stream_id = ANY(ARRAY[
                'loan-'        || $1,
                'docpkg-'      || $1,
                'fraud-'       || $1,
                'compliance-'  || $1
            ])
            OR (payload->>'application_id' = $1)
            ORDER BY global_position ASC
            """,
            app_id,
        )
    seen: set[tuple] = set()
    events = []
    for r in rows:
        key = (r["stream_id"], r["stream_position"])
        if key in seen:
            continue
        seen.add(key)
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass
        events.append({
            "stream_id":       r["stream_id"],
            "stream_position": r["stream_position"],
            "global_position": r["global_position"],
            "event_type":      r["event_type"],
            "event_version":   r["event_version"],
            "payload":         payload,
            "recorded_at":     r["recorded_at"].isoformat() if r["recorded_at"] else None,
        })
    return _resp(events)


# ── Compliance ────────────────────────────────────────────────────────────────

@app.get("/api/applications/{app_id}/compliance")
async def get_compliance(app_id: str):
    try:
        async with _store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projection_compliance_audit "
                "WHERE application_id=$1 ORDER BY rule_id",
                app_id,
            )
        return _resp([dict(r) for r in rows])
    except Exception:
        return _resp([])


# ── Agent performance ─────────────────────────────────────────────────────────

@app.get("/api/agents")
async def get_agents():
    try:
        async with _store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN ROUND(approve_count::numeric /
                              (approve_count + decline_count + refer_count) * 100, 1)
                         ELSE NULL END AS approve_pct,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN ROUND(decline_count::numeric /
                              (approve_count + decline_count + refer_count) * 100, 1)
                         ELSE NULL END AS decline_pct,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN ROUND(refer_count::numeric /
                              (approve_count + decline_count + refer_count) * 100, 1)
                         ELSE NULL END AS refer_pct
                FROM projection_agent_performance
                ORDER BY agent_id, model_version
                """
            )
        return _resp([dict(r) for r in rows])
    except Exception:
        return _resp([])


# ── Raw stream explorer ───────────────────────────────────────────────────────

@app.get("/api/stream/{stream_id:path}")
async def get_stream(stream_id: str, limit: int = 30, offset: int = 0):
    async with _store._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT stream_position, global_position, event_type, event_version,
                   payload, recorded_at
            FROM events
            WHERE stream_id = $1
            ORDER BY stream_position
            LIMIT $2 OFFSET $3
            """,
            stream_id, limit, offset,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE stream_id=$1", stream_id
        ) or 0
    events = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass
        events.append({
            "stream_position": r["stream_position"],
            "global_position": r["global_position"],
            "event_type":      r["event_type"],
            "event_version":   r["event_version"],
            "payload":         payload,
            "recorded_at":     r["recorded_at"].isoformat() if r["recorded_at"] else None,
        })
    return _resp({"stream_id": stream_id, "total": int(total), "events": events})


# ── Projection lag ────────────────────────────────────────────────────────────

@app.get("/api/lag")
async def get_lag():
    if _daemon:
        return _resp(await _daemon.get_all_lags())
    return _resp({})


# ── Available companies ───────────────────────────────────────────────────────

@app.get("/api/companies")
async def list_companies():
    docs_root = Path(_ROOT) / "documents"
    if not docs_root.is_dir():
        return _resp([])
    companies = sorted(p.name for p in docs_root.iterdir() if p.is_dir())
    return _resp(companies)


# ── Pipeline runner ───────────────────────────────────────────────────────────

# In-memory job store — keeps last 20 runs
_jobs: dict[str, dict] = {}
_MAX_JOBS = 20

# Load run_pipeline function once
_pl_spec = importlib.util.spec_from_file_location(
    "apex_run_pipeline",
    Path(_ROOT) / "scripts" / "run_pipeline.py",
)
_pl_module = importlib.util.module_from_spec(_pl_spec)
_pl_spec.loader.exec_module(_pl_module)
_run_pipeline_fn = _pl_module.run_pipeline


class _LineCapture:
    """Replaces sys.stdout to capture print() output line by line."""

    def __init__(self, lines: list[str], orig: Any) -> None:
        self._lines = lines
        self._orig = orig
        self._buf = ""

    def write(self, s: str) -> int:
        self._orig.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._lines.append(line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._lines.append(self._buf)
            self._buf = ""
        self._orig.flush()


async def _execute_pipeline(
    job_id: str, app_id: str, company_id: str, amount: float, docs_dir: str
) -> None:
    job = _jobs[job_id]
    orig_stdout = sys.stdout
    capture = _LineCapture(job["lines"], orig_stdout)
    try:
        sys.stdout = capture  # type: ignore[assignment]
        result = await _run_pipeline_fn(app_id, company_id, amount, docs_dir)
        job["result"] = _serialize(result)
        job["status"] = "done"
    except Exception as exc:
        job["error"] = str(exc)
        job["status"] = "error"
    finally:
        sys.stdout = orig_stdout
        capture.flush()


@app.post("/api/pipeline/run")
async def start_pipeline(body: dict):
    company_id = body.get("company_id", "COMP-002")
    amount     = float(body.get("amount", 500_000))
    app_id     = body.get("app_id") or f"APEX-DASH-{_uuid_mod.uuid4().hex[:6].upper()}"
    docs_dir   = f"documents/{company_id}"

    job_id = str(_uuid_mod.uuid4())

    # Evict oldest job if at capacity
    if len(_jobs) >= _MAX_JOBS:
        oldest = next(iter(_jobs))
        del _jobs[oldest]

    _jobs[job_id] = {
        "status":     "running",
        "lines":      [],
        "result":     None,
        "error":      None,
        "app_id":     app_id,
        "company_id": company_id,
        "amount":     amount,
    }

    asyncio.create_task(_execute_pipeline(job_id, app_id, company_id, amount, docs_dir))
    return _resp({"job_id": job_id, "app_id": app_id})


@app.get("/api/pipeline/status/{job_id}")
async def pipeline_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _resp(job)
