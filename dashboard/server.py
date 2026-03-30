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
import logging
import os
import sys
import uuid as _uuid_mod
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("apex.dashboard")

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from src.event_store import EventStore
from src.projections import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.commands.handlers import (
    SubmitApplicationCommand, RequestCreditAnalysisCommand,
    StartAgentSessionCommand, CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand, StartComplianceReviewCommand,
    RecordComplianceCheckCommand, GenerateDecisionCommand,
    HumanReviewCompletedCommand, ApproveApplicationCommand,
    DeclineApplicationCommand,
    handle_submit_application, handle_request_credit_analysis,
    handle_start_agent_session, handle_credit_analysis_completed,
    handle_fraud_screening_completed, handle_start_compliance_review,
    handle_record_compliance_check, handle_generate_decision,
    handle_human_review_completed, handle_approve_application,
    handle_decline_application,
)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        logger.error("get_stats failed: %s", e)
        return _resp({
            "total": 0, "approved": 0, "declined": 0, "referred": 0,
            "compliance_blocked": 0, "high_fraud": 0, "avg_fraud_score": None,
            "error": str(e),
        })


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
    except Exception as e:
        logger.error("list_applications failed: %s", e)
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


# ── Health / readiness ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Liveness + readiness probe.

    Returns:
      status     "ok" | "degraded" | "down"
      db         True if a DB ping round-trips successfully
      daemon     True if the projection daemon task is alive
      lags       per-projection event lag counts
      event_count total events in the store
    """
    result: dict[str, Any] = {
        "status":      "ok",
        "db":          False,
        "daemon":      False,
        "lags":        {},
        "event_count": 0,
    }

    # DB ping
    try:
        async with _store._pool.acquire() as conn:
            result["event_count"] = await conn.fetchval("SELECT COUNT(*) FROM events") or 0
        result["db"] = True
    except Exception as e:
        logger.error("health: DB ping failed: %s", e)
        result["status"] = "down"

    # Daemon check
    if _daemon_task and not _daemon_task.done():
        result["daemon"] = True
        try:
            result["lags"] = await _daemon.get_all_lags()
        except Exception:
            pass
    else:
        if result["status"] == "ok":
            result["status"] = "degraded"

    return _resp(result)


# ── Projection checkpoints ────────────────────────────────────────────────────

@app.get("/api/checkpoints")
async def get_checkpoints():
    """Live projection checkpoint positions and lag from public.projection_checkpoints."""
    async with _store._pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                projection_name,
                last_position,
                updated_at,
                (SELECT COALESCE(MAX(global_position), 0) FROM events) AS latest_global,
                (SELECT COALESCE(MAX(global_position), 0) FROM events) - last_position AS lag_events
            FROM projection_checkpoints
            ORDER BY projection_name
        """)
        result = []
        for r in rows:
            result.append({
                "projection_name": r["projection_name"],
                "last_position":   int(r["last_position"]),
                "latest_global":   int(r["latest_global"]),
                "lag_events":      int(r["lag_events"]),
                "updated_at":      r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return _resp(result)


# ── Available companies ───────────────────────────────────────────────────────

@app.get("/api/companies")
async def list_companies():
    docs_root = Path(_ROOT) / "documents"
    if not docs_root.is_dir():
        return _resp([])
    companies = sorted(p.name for p in docs_root.iterdir() if p.is_dir())
    return _resp(companies)


# ── Document upload ───────────────────────────────────────────────────────────

_ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv"}
_ALLOWED_STEMS = ["income_statement", "balance_sheet", "financial_statements", "financial_summary", "application_proposal"]


@app.get("/api/documents/{company_id}")
async def list_documents(company_id: str):
    """List files already present for a company."""
    docs_dir = Path(_ROOT) / "documents" / company_id
    if not docs_dir.is_dir():
        return _resp([])
    files = [
        {"name": f.name, "size": f.stat().st_size, "ext": f.suffix.lower()}
        for f in sorted(docs_dir.iterdir())
        if f.is_file() and f.suffix.lower() in _ALLOWED_EXTENSIONS
    ]
    return _resp(files)


@app.post("/api/documents/{company_id}/upload")
async def upload_document(company_id: str, file: UploadFile = File(...)):
    """Upload a PDF/Excel/CSV document for a company."""
    if not company_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid company_id")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type '{suffix}' not allowed. Use: {sorted(_ALLOWED_EXTENSIONS)}")

    docs_dir = Path(_ROOT) / "documents" / company_id
    docs_dir.mkdir(parents=True, exist_ok=True)

    dest = docs_dir / (file.filename or "upload")
    contents = await file.read()
    dest.write_bytes(contents)

    return _resp({"saved": str(dest.relative_to(_ROOT)), "size": len(contents)})


@app.delete("/api/documents/{company_id}/{filename}")
async def delete_document(company_id: str, filename: str):
    """Delete a document file for a company."""
    if not company_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid company_id")
    target = Path(_ROOT) / "documents" / company_id / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    target.unlink()
    return _resp({"deleted": filename})


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


# ── Demo runner ───────────────────────────────────────────────────────────────

_DEMO_STEPS = {
    "1": ("DEMO STEP 1 — The Week Standard",       "demo/step1_week_standard.py"),
    "2": ("DEMO STEP 2 — Concurrency Under Pressure", "demo/step2_concurrency.py"),
    "3": ("DEMO STEP 3 — Temporal Compliance Query",  "demo/step3_temporal_query.py"),
    "4": ("DEMO STEP 4 — Schema Upcasting",           "demo/step4_upcasting.py"),
    "5": ("DEMO STEP 5 — Gas Town Integrity",         "demo/step5_gas_town.py"),
    "6": ("DEMO STEP 6 — What-If Counterfactual",     "demo/step6_what_if.py"),
}

# Shared job store for demo runs (re-uses _jobs dict and _MAX_JOBS limit)


async def _execute_demo(job_id: str, step: str) -> None:
    job = _jobs[job_id]
    _, script_rel = _DEMO_STEPS[step]
    script_path = Path(_ROOT) / script_rel

    orig_stdout = sys.stdout
    capture = _LineCapture(job["lines"], orig_stdout)
    try:
        sys.stdout = capture  # type: ignore[assignment]

        spec = importlib.util.spec_from_file_location(f"demo_step{step}", script_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # defines main()
        await mod.main()                      # run it

        job["status"] = "done"
    except Exception as exc:
        job["error"] = str(exc)
        job["status"] = "error"
    finally:
        sys.stdout = orig_stdout
        capture.flush()


@app.post("/api/demo/run/{step}")
async def start_demo(step: str):
    if step not in _DEMO_STEPS:
        raise HTTPException(400, f"Unknown demo step '{step}'. Valid: {list(_DEMO_STEPS)}")

    title, _ = _DEMO_STEPS[step]
    job_id = str(_uuid_mod.uuid4())

    if len(_jobs) >= _MAX_JOBS:
        oldest = next(iter(_jobs))
        del _jobs[oldest]

    _jobs[job_id] = {
        "status": "running",
        "lines":  [],
        "result": None,
        "error":  None,
        "step":   step,
        "title":  title,
    }

    asyncio.create_task(_execute_demo(job_id, step))
    return _resp({"job_id": job_id, "step": step, "title": title})


@app.get("/api/demo/status/{job_id}")
async def demo_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _resp(job)


# ── Seed data ─────────────────────────────────────────────────────────────────

_SEED_SCENARIOS = [
    # (label, risk_tier, confidence, fraud_score, compliance_pass, outcome)
    ("APEX-SEED-APPROVED-1",  "LOW",    0.91, 0.03, True,  "APPROVE"),
    ("APEX-SEED-APPROVED-2",  "LOW",    0.87, 0.06, True,  "APPROVE"),
    ("APEX-SEED-APPROVED-3",  "MEDIUM", 0.78, 0.12, True,  "APPROVE"),
    ("APEX-SEED-DECLINED-1",  "HIGH",   0.82, 0.71, True,  "DECLINE"),
    ("APEX-SEED-DECLINED-2",  "HIGH",   0.65, 0.58, False, "DECLINE"),
    ("APEX-SEED-REFER-1",     "MEDIUM", 0.48, 0.22, True,  "REFER"),
    ("APEX-SEED-PARTIAL-1",   "MEDIUM", 0.75, 0.19, None,  "COMPLIANCE_REVIEW"),
    ("APEX-SEED-SUBMITTED-1", None,     None, None, None,  "SUBMITTED"),
]

_APPLICANTS = [
    ("apex-corp-001", 2_500_000, "commercial_real_estate"),
    ("beacon-tech-002", 850_000,  "equipment_finance"),
    ("summit-retail-003", 1_200_000, "working_capital"),
    ("harbor-logistics-004", 3_000_000, "acquisition"),
    ("delta-manufacturing-005", 500_000, "equipment_finance"),
    ("crestview-holdings-006", 1_750_000, "commercial_real_estate"),
    ("nexus-services-007", 650_000,  "working_capital"),
    ("ironbridge-capital-008", 4_000_000, "acquisition"),
]


async def _seed_one(store: EventStore, scenario_idx: int) -> dict:
    """Create one application following the lifecycle defined by the scenario."""
    label, risk_tier, confidence, fraud_score, compliance_pass, outcome = _SEED_SCENARIOS[scenario_idx]
    applicant_id, amount, purpose = _APPLICANTS[scenario_idx % len(_APPLICANTS)]

    app_id    = f"{label}-{_uuid_mod.uuid4().hex[:6].upper()}"
    agent_id  = f"seed-credit-{_uuid_mod.uuid4().hex[:6]}"
    sess_id   = f"seed-sess-{_uuid_mod.uuid4().hex[:6]}"
    orch_id   = f"seed-orch-{_uuid_mod.uuid4().hex[:6]}"
    orch_sess = f"seed-osess-{_uuid_mod.uuid4().hex[:6]}"
    corr_id   = _uuid_mod.uuid4().hex

    # ── 1. Submit ─────────────────────────────────────────────────────────────
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id=applicant_id,
            requested_amount_usd=amount, loan_purpose=purpose,
            submission_channel="seed", correlation_id=corr_id,
        ), store,
    )

    if outcome == "SUBMITTED":
        return {"app_id": app_id, "final_state": "SUBMITTED"}

    # ── 2. Agent sessions ─────────────────────────────────────────────────────
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=sess_id,
            context_source="fresh", model_version="credit-model-v2.3",
        ), store,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=orch_id, session_id=orch_sess,
            context_source="fresh", model_version="orchestrator-v3.0",
        ), store,
    )

    # ── 3. Credit analysis ────────────────────────────────────────────────────
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=sess_id,
            model_version="credit-model-v2.3",
            confidence_score=confidence, risk_tier=risk_tier,
            recommended_limit_usd=amount * 0.9,
            duration_ms=1_200, correlation_id=corr_id,
        ), store,
    )

    # ── 4. Fraud screening ────────────────────────────────────────────────────
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=sess_id,
            fraud_score=fraud_score,
            anomaly_flags=["velocity_flag"] if fraud_score and fraud_score > 0.5 else [],
            screening_model_version="fraud-model-v1.5",
            correlation_id=corr_id,
        ), store,
    )

    # ── 5. Compliance ─────────────────────────────────────────────────────────
    await handle_start_compliance_review(
        StartComplianceReviewCommand(
            application_id=app_id,
            checks_required=["KYC-001", "AML-002", "BSA-003"],
            regulation_set_version="reg-v2026-q1",
        ), store,
    )

    if compliance_pass is None:
        # Partial — leave in COMPLIANCE_REVIEW with only first rule done
        await handle_record_compliance_check(
            RecordComplianceCheckCommand(
                application_id=app_id, rule_id="KYC-001",
                rule_version="v3", passed=True,
            ), store,
        )
        return {"app_id": app_id, "final_state": "COMPLIANCE_REVIEW"}

    for rule_id, rule_ver in [("KYC-001", "v3"), ("AML-002", "v2"), ("BSA-003", "v1")]:
        await handle_record_compliance_check(
            RecordComplianceCheckCommand(
                application_id=app_id, rule_id=rule_id,
                rule_version=rule_ver, passed=compliance_pass,
                failure_reason="" if compliance_pass else f"{rule_id} threshold not met",
            ), store,
        )

    # ── 6. Decision ───────────────────────────────────────────────────────────
    credit_stream = f"agent-{agent_id}-{sess_id}"
    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id, orchestrator_agent_id=orch_id,
            recommendation=outcome if outcome in ("APPROVE", "DECLINE") else "REFER",
            confidence_score=confidence,
            contributing_agent_sessions=[credit_stream],
            decision_basis_summary=f"Seeded: risk={risk_tier}, fraud={fraud_score}.",
            model_versions={"credit": "credit-model-v2.3"},
            correlation_id=corr_id,
        ), store,
    )

    if outcome == "REFER":
        return {"app_id": app_id, "final_state": "PENDING_DECISION (REFER)"}

    # ── 7. Human review ───────────────────────────────────────────────────────
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=app_id, reviewer_id="seed-officer-001",
            override=False,
            final_decision="APPROVE" if outcome == "APPROVE" else "DECLINE",
            correlation_id=corr_id,
        ), store,
    )

    # ── 8. Finalise ───────────────────────────────────────────────────────────
    if outcome == "APPROVE":
        await handle_approve_application(
            ApproveApplicationCommand(
                application_id=app_id,
                approved_amount_usd=amount * 0.9,
                interest_rate=0.065 if risk_tier == "LOW" else 0.085,
                conditions=["standard_covenants"],
                approved_by="seed-officer-001",
            ), store,
        )
        return {"app_id": app_id, "final_state": "FINAL_APPROVED"}
    else:
        await handle_decline_application(
            DeclineApplicationCommand(
                application_id=app_id,
                decline_reasons=["high_fraud_score" if fraud_score and fraud_score > 0.5
                                  else "high_risk_tier"],
                declined_by="seed-officer-001",
            ), store,
        )
        return {"app_id": app_id, "final_state": "FINAL_DECLINED"}


async def _run_seed(job_id: str) -> None:
    job = _jobs[job_id]
    orig_stdout = sys.stdout
    capture = _LineCapture(job["lines"], orig_stdout)
    results = []
    try:
        sys.stdout = capture  # type: ignore[assignment]
        print("Seeding database with sample applications…\n")
        for i, (label, *_) in enumerate(_SEED_SCENARIOS):
            print(f"  [{i+1}/{len(_SEED_SCENARIOS)}] {label} …", flush=True)
            try:
                r = await _seed_one(_store, i)
                results.append(r)
                print(f"         → {r['app_id']}  state={r['final_state']}")
            except Exception as e:
                print(f"         ✗ FAILED: {e}")
                results.append({"app_id": label, "error": str(e)})
        print(f"\n✅ Seed complete — {len(results)} applications created.")
        job["result"] = results
        job["status"] = "done"
    except Exception as exc:
        job["error"] = str(exc)
        job["status"] = "error"
    finally:
        sys.stdout = orig_stdout
        capture.flush()


@app.post("/api/seed")
async def seed_database():
    """Populate the database with sample applications in a variety of states."""
    job_id = str(_uuid_mod.uuid4())
    if len(_jobs) >= _MAX_JOBS:
        oldest = next(iter(_jobs))
        del _jobs[oldest]
    _jobs[job_id] = {"status": "running", "lines": [], "result": None, "error": None}
    asyncio.create_task(_run_seed(job_id))
    return _resp({"job_id": job_id})


# ── Global Event Store viewer ─────────────────────────────────────────────────

@app.get("/api/events/global")
async def get_global_events(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    stream_prefix: str | None = None,
):
    """
    Read-only chronological view of ALL events in the store,
    ordered by global_position ASC.  Supports pagination and optional filters.
    """
    try:
        async with _store._pool.acquire() as conn:
            # Build WHERE clause
            conditions: list[str] = []
            params: list[Any] = []

            if event_type:
                params.append(event_type)
                conditions.append(f"event_type = ${len(params)}")
            if stream_prefix:
                params.append(f"{stream_prefix}%")
                conditions.append(f"stream_id LIKE ${len(params)}")

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            total: int = await conn.fetchval(
                f"SELECT COUNT(*) FROM events {where}", *params
            ) or 0

            params_page = params + [limit, offset]
            rows = await conn.fetch(
                f"""
                SELECT stream_id, stream_position, global_position,
                       event_type, event_version, payload, recorded_at
                FROM events
                {where}
                ORDER BY global_position ASC
                LIMIT ${len(params_page) - 1} OFFSET ${len(params_page)}
                """,
                *params_page,
            )

        events = []
        for r in rows:
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

        return _resp({"total": int(total), "limit": limit, "offset": offset, "events": events})
    except Exception as e:
        return _resp({"total": 0, "limit": limit, "offset": offset, "events": [], "error": str(e)})


@app.get("/api/events/global/types")
async def get_global_event_types():
    """Return distinct event types present in the store (for filter dropdown)."""
    try:
        async with _store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT event_type FROM events ORDER BY event_type"
            )
        return _resp([r["event_type"] for r in rows])
    except Exception:
        return _resp([])


# ── Analysis report ───────────────────────────────────────────────────────────

async def _safe_fetch(conn, query: str, *args) -> list:
    """Run a query and return rows as dicts; return [] on any error."""
    try:
        rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("analysis query skipped: %s", exc)
        return []


async def _safe_fetchrow(conn, query: str, *args) -> dict:
    """Run a single-row query; return {} on any error."""
    try:
        row = await conn.fetchrow(query, *args)
        return dict(row) if row else {}
    except Exception as exc:
        logger.warning("analysis query skipped: %s", exc)
        return {}


def _ts(val) -> str | None:
    return val.isoformat() if val else None


@app.get("/api/analysis")
async def get_analysis():
    """
    Multi-schema analysis report covering all 12 tables:

    Public schema (event store):
      events, event_streams, outbox, projection_checkpoints,
      projection_agent_performance, projection_application_summary,
      projection_compliance_audit, projection_compliance_audit_snapshots

    applicant_registry schema:
      companies, financial_history, compliance_flags, loan_relationships
    """
    async with _store._pool.acquire() as conn:

        # ════════════════════════════════════════════════════════════
        # SECTION 1 — EVENT STORE  (public.events)
        # ════════════════════════════════════════════════════════════

        summary = await _safe_fetchrow(conn, """
            SELECT
                COUNT(*)                                                    AS total_events,
                COUNT(DISTINCT stream_id)                                   AS total_streams,
                COUNT(*) FILTER (WHERE recorded_at::date = CURRENT_DATE)   AS today_events,
                COUNT(*) FILTER (WHERE recorded_at >= NOW() - INTERVAL '1 hour') AS last_hour_events,
                MAX(recorded_at)                                            AS last_event_at,
                MIN(recorded_at)                                            AS first_event_at
            FROM events
        """)
        summary["last_event_at"]  = _ts(summary.pop("last_event_at",  None))
        summary["first_event_at"] = _ts(summary.pop("first_event_at", None))

        event_type_dist = await _safe_fetch(conn, """
            SELECT event_type,
                   COUNT(*) AS count,
                   ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS pct
            FROM events
            GROUP BY event_type
            ORDER BY count DESC
        """)
        for r in event_type_dist:
            r["pct"] = float(r["pct"] or 0)

        daily_volume = await _safe_fetch(conn, """
            SELECT TO_CHAR(DATE_TRUNC('day', recorded_at), 'YYYY-MM-DD') AS day,
                   COUNT(*) AS count
            FROM events
            WHERE recorded_at >= NOW() - INTERVAL '14 days'
            GROUP BY day ORDER BY day
        """)

        hourly_volume = await _safe_fetch(conn, """
            SELECT TO_CHAR(DATE_TRUNC('hour', recorded_at), 'HH24:MI') AS hour,
                   COUNT(*) AS count
            FROM events
            WHERE recorded_at >= NOW() - INTERVAL '24 hours'
            GROUP BY hour ORDER BY hour
        """)

        version_dist = await _safe_fetch(conn, """
            SELECT event_version AS version, COUNT(*) AS count
            FROM events GROUP BY event_version ORDER BY event_version
        """)

        top_streams = await _safe_fetch(conn, """
            SELECT stream_id,
                   COUNT(*) AS event_count,
                   MIN(recorded_at) AS first_event,
                   MAX(recorded_at) AS last_event
            FROM events
            GROUP BY stream_id
            ORDER BY event_count DESC LIMIT 15
        """)
        for r in top_streams:
            r["first_event"] = _ts(r.pop("first_event", None))
            r["last_event"]  = _ts(r.pop("last_event",  None))

        stream_type_dist = await _safe_fetch(conn, """
            SELECT
                CASE
                    WHEN stream_id LIKE 'loan-%'  THEN 'loan'
                    WHEN stream_id LIKE 'agent-%' THEN 'agent'
                    WHEN stream_id LIKE 'audit-%' THEN 'audit'
                    ELSE 'other'
                END AS stream_type,
                COUNT(DISTINCT stream_id) AS stream_count,
                COUNT(*)                  AS event_count
            FROM events
            GROUP BY stream_type ORDER BY event_count DESC
        """)

        # ════════════════════════════════════════════════════════════
        # SECTION 2 — STREAM REGISTRY  (public.event_streams)
        # ════════════════════════════════════════════════════════════

        stream_registry = await _safe_fetchrow(conn, """
            SELECT
                COUNT(*)                                       AS total_streams,
                COUNT(*) FILTER (WHERE archived_at IS NULL)   AS active_streams,
                COUNT(*) FILTER (WHERE archived_at IS NOT NULL) AS archived_streams,
                MAX(created_at)                                AS latest_stream_at
            FROM event_streams
        """)
        stream_registry["latest_stream_at"] = _ts(stream_registry.pop("latest_stream_at", None))

        aggregate_type_dist = await _safe_fetch(conn, """
            SELECT aggregate_type,
                   COUNT(*) AS stream_count,
                   SUM(current_version) AS total_events_in_streams
            FROM event_streams
            GROUP BY aggregate_type ORDER BY stream_count DESC
        """)

        # ════════════════════════════════════════════════════════════
        # SECTION 3 — OUTBOX  (public.outbox)
        # ════════════════════════════════════════════════════════════

        outbox_summary = await _safe_fetchrow(conn, """
            SELECT
                COUNT(*) AS total_messages,
                COUNT(*) FILTER (WHERE published_at IS NOT NULL) AS published,
                COUNT(*) FILTER (WHERE published_at IS NULL)     AS pending,
                MAX(attempts)                                     AS max_attempts,
                ROUND(AVG(attempts), 1)                          AS avg_attempts
            FROM outbox
        """)
        outbox_dest_dist = await _safe_fetch(conn, """
            SELECT destination, COUNT(*) AS count,
                   COUNT(*) FILTER (WHERE published_at IS NULL) AS pending
            FROM outbox GROUP BY destination ORDER BY count DESC
        """)

        # ════════════════════════════════════════════════════════════
        # SECTION 4 — PROJECTION HEALTH  (public.projection_checkpoints)
        # ════════════════════════════════════════════════════════════

        checkpoint_rows = await _safe_fetch(conn, """
            SELECT projection_name, last_position,
                   updated_at,
                   (SELECT MAX(global_position) FROM events) - last_position AS lag_events
            FROM projection_checkpoints ORDER BY projection_name
        """)
        for r in checkpoint_rows:
            r["updated_at"] = _ts(r.pop("updated_at", None))

        # ════════════════════════════════════════════════════════════
        # SECTION 5 — APPLICATION SUMMARY  (public.projection_application_summary)
        # ════════════════════════════════════════════════════════════

        application_funnel = await _safe_fetch(conn, """
            SELECT state, COUNT(*) AS count
            FROM projection_application_summary
            GROUP BY state ORDER BY count DESC
        """)

        app_financials = await _safe_fetchrow(conn, """
            SELECT
                ROUND(AVG(requested_amount_usd), 0)   AS avg_requested_usd,
                ROUND(AVG(approved_amount_usd), 0)    AS avg_approved_usd,
                MAX(requested_amount_usd)             AS max_requested_usd,
                ROUND(AVG(fraud_score), 3)            AS avg_fraud_score,
                COUNT(*) FILTER (WHERE fraud_score > 0.6) AS high_fraud_count
            FROM projection_application_summary
        """)
        for k, v in list(app_financials.items()):
            app_financials[k] = float(v) if v is not None else None

        risk_tier_dist = await _safe_fetch(conn, """
            SELECT risk_tier, COUNT(*) AS count
            FROM projection_application_summary
            WHERE risk_tier IS NOT NULL
            GROUP BY risk_tier ORDER BY count DESC
        """)

        decision_dist = await _safe_fetch(conn, """
            SELECT decision, COUNT(*) AS count
            FROM projection_application_summary
            WHERE decision IS NOT NULL
            GROUP BY decision ORDER BY count DESC
        """)

        compliance_status_dist = await _safe_fetch(conn, """
            SELECT compliance_status, COUNT(*) AS count
            FROM projection_application_summary
            GROUP BY compliance_status ORDER BY count DESC
        """)

        # ════════════════════════════════════════════════════════════
        # SECTION 6 — AGENT PERFORMANCE  (public.projection_agent_performance)
        # ════════════════════════════════════════════════════════════

        agent_summary = await _safe_fetchrow(conn, """
            SELECT
                COUNT(DISTINCT agent_id)              AS total_agents,
                SUM(analyses_completed)               AS total_analyses,
                SUM(decisions_generated)              AS total_decisions,
                ROUND(AVG(avg_confidence_score), 3)   AS overall_avg_confidence,
                ROUND(AVG(avg_duration_ms), 0)        AS overall_avg_duration_ms,
                SUM(human_override_count)             AS total_overrides
            FROM projection_agent_performance
        """)
        for k, v in list(agent_summary.items()):
            agent_summary[k] = float(v) if v is not None else None

        top_agents = await _safe_fetch(conn, """
            SELECT agent_id, model_version,
                   analyses_completed, decisions_generated,
                   ROUND(avg_confidence_score, 3) AS avg_confidence,
                   approve_count, decline_count, refer_count,
                   human_override_count,
                   last_seen_at
            FROM projection_agent_performance
            ORDER BY analyses_completed DESC LIMIT 10
        """)
        for r in top_agents:
            r["last_seen_at"] = _ts(r.pop("last_seen_at", None))
            r["avg_confidence"] = float(r["avg_confidence"]) if r["avg_confidence"] else None

        # ════════════════════════════════════════════════════════════
        # SECTION 7 — COMPLIANCE AUDIT  (public.projection_compliance_audit)
        # ════════════════════════════════════════════════════════════

        compliance_audit_summary = await _safe_fetchrow(conn, """
            SELECT
                COUNT(*)                                           AS total_checks,
                COUNT(*) FILTER (WHERE status = 'PASSED')         AS passed,
                COUNT(*) FILTER (WHERE status = 'FAILED')         AS failed,
                COUNT(*) FILTER (WHERE status = 'PENDING')        AS pending,
                COUNT(DISTINCT application_id)                    AS apps_evaluated,
                COUNT(DISTINCT rule_id)                           AS unique_rules
            FROM projection_compliance_audit
        """)

        rule_pass_rates = await _safe_fetch(conn, """
            SELECT rule_id,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                   COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'PASSED')
                         / NULLIF(COUNT(*), 0), 1) AS pass_rate_pct
            FROM projection_compliance_audit
            GROUP BY rule_id ORDER BY total DESC LIMIT 10
        """)
        for r in rule_pass_rates:
            r["pass_rate_pct"] = float(r["pass_rate_pct"] or 0)

        compliance_snapshots_summary = await _safe_fetchrow(conn, """
            SELECT COUNT(*) AS total_snapshots,
                   COUNT(DISTINCT application_id) AS apps_with_snapshots,
                   MAX(snapshot_at) AS latest_snapshot_at
            FROM projection_compliance_audit_snapshots
        """)
        compliance_snapshots_summary["latest_snapshot_at"] = _ts(
            compliance_snapshots_summary.pop("latest_snapshot_at", None)
        )

        # ════════════════════════════════════════════════════════════
        # SECTION 8 — APPLICANT REGISTRY  (applicant_registry.*)
        # ════════════════════════════════════════════════════════════

        registry_summary = await _safe_fetchrow(conn, """
            SELECT COUNT(*) AS total_companies,
                   COUNT(DISTINCT sector) AS sectors,
                   COUNT(DISTINCT jurisdiction) AS jurisdictions
            FROM applicant_registry.companies
        """)

        sector_dist = await _safe_fetch(conn, """
            SELECT sector, COUNT(*) AS count
            FROM applicant_registry.companies
            WHERE sector IS NOT NULL
            GROUP BY sector ORDER BY count DESC
        """)

        registry_risk_tier_dist = await _safe_fetch(conn, """
            SELECT risk_tier, COUNT(*) AS count
            FROM applicant_registry.companies
            WHERE risk_tier IS NOT NULL
            GROUP BY risk_tier ORDER BY count DESC
        """)

        trajectory_dist = await _safe_fetch(conn, """
            SELECT trajectory, COUNT(*) AS count
            FROM applicant_registry.companies
            WHERE trajectory IS NOT NULL
            GROUP BY trajectory ORDER BY count DESC
        """)

        top_jurisdictions = await _safe_fetch(conn, """
            SELECT jurisdiction, COUNT(*) AS count
            FROM applicant_registry.companies
            WHERE jurisdiction IS NOT NULL
            GROUP BY jurisdiction ORDER BY count DESC LIMIT 10
        """)

        legal_type_dist = await _safe_fetch(conn, """
            SELECT legal_type, COUNT(*) AS count
            FROM applicant_registry.companies
            WHERE legal_type IS NOT NULL
            GROUP BY legal_type ORDER BY count DESC
        """)

        # ── Compliance flags ──────────────────────────────────────
        flags_summary = await _safe_fetchrow(conn, """
            SELECT COUNT(*) AS total_flags,
                   COUNT(*) FILTER (WHERE is_active) AS active_flags,
                   COUNT(DISTINCT company_id) AS companies_flagged
            FROM applicant_registry.compliance_flags
        """)

        flag_type_dist = await _safe_fetch(conn, """
            SELECT flag_type,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_active) AS active
            FROM applicant_registry.compliance_flags
            GROUP BY flag_type ORDER BY total DESC
        """)

        # ── Financial history ─────────────────────────────────────
        financials_summary = await _safe_fetchrow(conn, """
            SELECT COUNT(*) AS total_records,
                   COUNT(DISTINCT company_id) AS companies_with_financials,
                   COUNT(DISTINCT fiscal_year) AS fiscal_years,
                   ROUND(AVG(total_revenue), 0) AS avg_revenue,
                   ROUND(AVG(net_income), 0) AS avg_net_income,
                   ROUND(AVG(ebitda_margin_pct), 1) AS avg_ebitda_margin
            FROM applicant_registry.financial_history
        """)
        for k, v in list(financials_summary.items()):
            financials_summary[k] = float(v) if v is not None else None

        revenue_by_sector = await _safe_fetch(conn, """
            SELECT c.sector,
                   ROUND(AVG(f.total_revenue), 0) AS avg_revenue,
                   ROUND(AVG(f.ebitda_margin_pct), 1) AS avg_ebitda_margin,
                   COUNT(DISTINCT f.company_id) AS company_count
            FROM applicant_registry.financial_history f
            JOIN applicant_registry.companies c USING (company_id)
            WHERE c.sector IS NOT NULL
            GROUP BY c.sector ORDER BY avg_revenue DESC NULLS LAST
        """)
        for r in revenue_by_sector:
            r["avg_revenue"] = float(r["avg_revenue"]) if r["avg_revenue"] else None
            r["avg_ebitda_margin"] = float(r["avg_ebitda_margin"]) if r["avg_ebitda_margin"] else None

        # ── Loan relationships ────────────────────────────────────
        loan_summary = await _safe_fetchrow(conn, """
            SELECT COUNT(*) AS total_loans,
                   COUNT(DISTINCT company_id) AS borrowers,
                   ROUND(AVG(loan_amount_usd), 0) AS avg_loan_usd,
                   SUM(loan_amount_usd) AS total_loan_usd,
                   COUNT(*) FILTER (WHERE default_occurred) AS defaults,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE default_occurred)
                         / NULLIF(COUNT(*), 0), 1) AS default_rate_pct
            FROM applicant_registry.loan_relationships
        """)
        for k, v in list(loan_summary.items()):
            loan_summary[k] = float(v) if v is not None else None

        loan_status_dist = await _safe_fetch(conn, """
            SELECT status,
                   COUNT(*) AS count,
                   COUNT(*) FILTER (WHERE default_occurred) AS defaults
            FROM applicant_registry.loan_relationships
            WHERE status IS NOT NULL
            GROUP BY status ORDER BY count DESC
        """)

    return _resp({
        # Event store — public.events
        "summary":           summary,
        "event_type_dist":   event_type_dist,
        "daily_volume":      daily_volume,
        "hourly_volume":     hourly_volume,
        "version_dist":      version_dist,
        "top_streams":       top_streams,
        "stream_type_dist":  stream_type_dist,
        # Stream registry — public.event_streams
        "stream_registry":       stream_registry,
        "aggregate_type_dist":   aggregate_type_dist,
        # Outbox — public.outbox
        "outbox_summary":    outbox_summary,
        "outbox_dest_dist":  outbox_dest_dist,
        # Projection health — public.projection_checkpoints
        "checkpoints":       checkpoint_rows,
        # Application summary — public.projection_application_summary
        "application_funnel":     application_funnel,
        "app_financials":         app_financials,
        "risk_tier_dist":         risk_tier_dist,
        "decision_dist":          decision_dist,
        "compliance_status_dist": compliance_status_dist,
        # Agent performance — public.projection_agent_performance
        "agent_summary":     agent_summary,
        "top_agents":        top_agents,
        # Compliance audit — public.projection_compliance_audit + snapshots
        "compliance_audit_summary":       compliance_audit_summary,
        "rule_pass_rates":                rule_pass_rates,
        "compliance_snapshots_summary":   compliance_snapshots_summary,
        # applicant_registry.companies
        "registry_summary":        registry_summary,
        "sector_dist":             sector_dist,
        "registry_risk_tier_dist": registry_risk_tier_dist,
        "trajectory_dist":         trajectory_dist,
        "top_jurisdictions":       top_jurisdictions,
        "legal_type_dist":         legal_type_dist,
        # applicant_registry.compliance_flags
        "flags_summary":    flags_summary,
        "flag_type_dist":   flag_type_dist,
        # applicant_registry.financial_history
        "financials_summary":  financials_summary,
        "revenue_by_sector":   revenue_by_sector,
        # applicant_registry.loan_relationships
        "loan_summary":      loan_summary,
        "loan_status_dist":  loan_status_dist,
    })
