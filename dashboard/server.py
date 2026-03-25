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

from fastapi import FastAPI, HTTPException
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

@app.get("/api/analysis")
async def get_analysis():
    """
    Comprehensive event-store analysis report pulled directly from the
    ``events`` table and the projection views.

    Sections returned:
      summary          — totals: events, streams, today, last recorded_at
      event_type_dist  — count + pct per event type, sorted desc
      daily_volume     — events per calendar day for the last 14 days
      hourly_volume    — events per hour for the last 24 hours
      top_streams      — top 15 streams by event count
      stream_type_dist — stream-prefix breakdown (loan / agent / audit / other)
      application_funnel — application count per lifecycle state
      version_dist     — event schema version distribution
      write_latency    — p50 / p95 / p99 gap between consecutive global positions
                         (proxy for write cadence, not wall-clock latency)
    """
    try:
        async with _store._pool.acquire() as conn:

            # ── Summary ──────────────────────────────────────────────
            summary_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                              AS total_events,
                    COUNT(DISTINCT stream_id)                             AS total_streams,
                    COUNT(*) FILTER (WHERE recorded_at::date = CURRENT_DATE) AS today_events,
                    COUNT(*) FILTER (WHERE recorded_at >= NOW() - INTERVAL '1 hour') AS last_hour_events,
                    MAX(recorded_at)                                      AS last_event_at,
                    MIN(recorded_at)                                      AS first_event_at
                FROM events
                """
            )
            summary = dict(summary_row) if summary_row else {}
            if summary.get("last_event_at"):
                summary["last_event_at"] = summary["last_event_at"].isoformat()
            if summary.get("first_event_at"):
                summary["first_event_at"] = summary["first_event_at"].isoformat()

            # ── Event-type distribution ───────────────────────────────
            type_rows = await conn.fetch(
                """
                SELECT event_type,
                       COUNT(*)                                AS count,
                       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
                FROM events
                GROUP BY event_type
                ORDER BY count DESC
                """
            )
            event_type_dist = [
                {"event_type": r["event_type"], "count": r["count"], "pct": float(r["pct"])}
                for r in type_rows
            ]

            # ── Daily volume — last 14 days ───────────────────────────
            daily_rows = await conn.fetch(
                """
                SELECT TO_CHAR(DATE_TRUNC('day', recorded_at), 'YYYY-MM-DD') AS day,
                       COUNT(*) AS count
                FROM events
                WHERE recorded_at >= NOW() - INTERVAL '14 days'
                GROUP BY day
                ORDER BY day
                """
            )
            daily_volume = [{"day": r["day"], "count": r["count"]} for r in daily_rows]

            # ── Hourly volume — last 24 hours ─────────────────────────
            hourly_rows = await conn.fetch(
                """
                SELECT TO_CHAR(DATE_TRUNC('hour', recorded_at), 'HH24:MI') AS hour,
                       COUNT(*) AS count
                FROM events
                WHERE recorded_at >= NOW() - INTERVAL '24 hours'
                GROUP BY hour
                ORDER BY hour
                """
            )
            hourly_volume = [{"hour": r["hour"], "count": r["count"]} for r in hourly_rows]

            # ── Top streams ───────────────────────────────────────────
            stream_rows = await conn.fetch(
                """
                SELECT stream_id,
                       COUNT(*)        AS event_count,
                       MIN(recorded_at) AS first_event,
                       MAX(recorded_at) AS last_event
                FROM events
                GROUP BY stream_id
                ORDER BY event_count DESC
                LIMIT 15
                """
            )
            top_streams = [
                {
                    "stream_id":   r["stream_id"],
                    "event_count": r["event_count"],
                    "first_event": r["first_event"].isoformat() if r["first_event"] else None,
                    "last_event":  r["last_event"].isoformat()  if r["last_event"]  else None,
                }
                for r in stream_rows
            ]

            # ── Stream-type distribution (prefix bucketing) ───────────
            stream_type_rows = await conn.fetch(
                """
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
                GROUP BY stream_type
                ORDER BY event_count DESC
                """
            )
            stream_type_dist = [
                {
                    "stream_type":  r["stream_type"],
                    "stream_count": r["stream_count"],
                    "event_count":  r["event_count"],
                }
                for r in stream_type_rows
            ]

            # ── Application lifecycle funnel ──────────────────────────
            try:
                funnel_rows = await conn.fetch(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM projection_application_summary
                    GROUP BY state
                    ORDER BY count DESC
                    """
                )
                application_funnel = [
                    {"state": r["state"], "count": r["count"]} for r in funnel_rows
                ]
            except Exception:
                application_funnel = []

            # ── Schema version distribution ───────────────────────────
            version_rows = await conn.fetch(
                """
                SELECT event_version, COUNT(*) AS count
                FROM events
                GROUP BY event_version
                ORDER BY event_version
                """
            )
            version_dist = [
                {"version": r["event_version"], "count": r["count"]} for r in version_rows
            ]

        return _resp({
            "summary":            summary,
            "event_type_dist":    event_type_dist,
            "daily_volume":       daily_volume,
            "hourly_volume":      hourly_volume,
            "top_streams":        top_streams,
            "stream_type_dist":   stream_type_dist,
            "application_funnel": application_funnel,
            "version_dist":       version_dist,
        })

    except Exception as e:
        logger.error("get_analysis failed: %s", e)
        raise HTTPException(500, f"Analysis query failed: {e}")
