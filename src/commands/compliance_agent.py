"""
ComplianceAgent Command Handler

Evaluates 6 regulatory rules in sequence. Rules are deterministic Python —
no LLM in the decision path. LLM used only to generate human-readable summaries.
Hard blocks (is_hard_block=True) stop evaluation immediately.

Rules:
  REG-001 AML         No hard block  — no active AML_WATCH flag
  REG-002 OFAC        HARD BLOCK     — no active SANCTIONS_REVIEW flag
  REG-003 Jurisdiction HARD BLOCK    — jurisdiction != "MT"
  REG-004 Legal entity No hard block — not (Sole Proprietor AND >$250K)
  REG-005 Min history  HARD BLOCK    — (2026 - founded_year) >= 2
  REG-006 CRA          No hard block — always NOTED, never failed
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from src.aggregates.loan_application import LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    ApplicationDeclined,
    ComplianceCheckCompleted,
    ComplianceRuleFailed,
    ComplianceRuleNoted,
    ComplianceRulePassed,
    DomainError,
)
from src.registry.client import ApplicantRegistryClient

REGULATION_VERSION = os.getenv("REGULATION_VERSION", "2026-Q1")
CURRENT_YEAR = 2026


@dataclass
class RunComplianceCommand:
    application_id: str
    applicant_id: str
    agent_id: str
    requested_amount_usd: float
    session_id: str = field(default_factory=lambda: str(uuid4()))
    model_version: str = "compliance-agent-v1"
    correlation_id: str | None = None


async def handle_compliance_check(
    cmd: RunComplianceCommand,
    store: EventStore,
    registry: ApplicantRegistryClient,
) -> dict[str, Any]:
    """Full compliance evaluation pipeline."""

    # ── Step 1: Gas Town anchor ───────────────────────────────────────────────
    await store.append(
        stream_id=f"agent-{cmd.agent_id}-{cmd.session_id}",
        events=[AgentContextLoaded(
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            context_source="fresh",
            model_version=cmd.model_version,
        )],
        expected_version=-1,
        correlation_id=cmd.correlation_id,
    )

    # ── Step 2: Load company profile ──────────────────────────────────────────
    company = await registry.get_company(cmd.applicant_id)
    if not company:
        raise DomainError(
            f"Company {cmd.applicant_id} not found in Applicant Registry.",
            rule="compliance_company_required",
        )

    flags = await registry.get_compliance_flags(cmd.applicant_id)
    flag_types = {f["flag_type"] for f in flags if f["is_active"]}

    compliance_stream = f"compliance-{cmd.application_id}"
    compliance_events = []

    rules_evaluated = 0
    rules_passed    = 0
    rules_failed    = 0
    hard_block      = False
    block_reason    = ""

    # ── Step 3: Evaluate rules in sequence ────────────────────────────────────

    # REG-001 — AML (not hard block)
    rules_evaluated += 1
    if "AML_WATCH" not in flag_types:
        compliance_events.append(ComplianceRulePassed(
            application_id=cmd.application_id,
            rule_id="REG-001",
            rule_version=REGULATION_VERSION,
            evidence_hash=_hash({"flag_types": list(flag_types)}),
        ))
        rules_passed += 1
    else:
        compliance_events.append(ComplianceRuleFailed(
            application_id=cmd.application_id,
            rule_id="REG-001",
            rule_version=REGULATION_VERSION,
            failure_reason="Active AML_WATCH flag on file.",
            remediation_required=True,
        ))
        rules_failed += 1

    # REG-002 — OFAC Sanctions (HARD BLOCK)
    rules_evaluated += 1
    if "SANCTIONS_REVIEW" not in flag_types:
        compliance_events.append(ComplianceRulePassed(
            application_id=cmd.application_id,
            rule_id="REG-002",
            rule_version=REGULATION_VERSION,
            evidence_hash=_hash({"flag_types": list(flag_types)}),
        ))
        rules_passed += 1
    else:
        compliance_events.append(ComplianceRuleFailed(
            application_id=cmd.application_id,
            rule_id="REG-002",
            rule_version=REGULATION_VERSION,
            failure_reason="Active SANCTIONS_REVIEW flag — OFAC hard block.",
            remediation_required=False,
        ))
        rules_failed += 1
        hard_block   = True
        block_reason = "OFAC SANCTIONS_REVIEW flag active."

    if hard_block:
        return await _finalise(
            cmd, store, compliance_stream, compliance_events,
            rules_evaluated, rules_passed, rules_failed,
            hard_block, block_reason,
        )

    # REG-003 — Jurisdiction (HARD BLOCK)
    rules_evaluated += 1
    jurisdiction = (company.get("jurisdiction") or "").upper()
    if jurisdiction != "MT":
        compliance_events.append(ComplianceRulePassed(
            application_id=cmd.application_id,
            rule_id="REG-003",
            rule_version=REGULATION_VERSION,
            evidence_hash=_hash({"jurisdiction": jurisdiction}),
        ))
        rules_passed += 1
    else:
        compliance_events.append(ComplianceRuleFailed(
            application_id=cmd.application_id,
            rule_id="REG-003",
            rule_version=REGULATION_VERSION,
            failure_reason="Montana (MT) jurisdiction excluded from lending programme.",
            remediation_required=False,
        ))
        rules_failed += 1
        hard_block   = True
        block_reason = "REG-003: Montana jurisdiction excluded."

    if hard_block:
        return await _finalise(
            cmd, store, compliance_stream, compliance_events,
            rules_evaluated, rules_passed, rules_failed,
            hard_block, block_reason,
        )

    # REG-004 — Legal Entity (not hard block)
    rules_evaluated += 1
    legal_type = (company.get("legal_type") or "").lower()
    is_sole_prop_over_limit = (
        "sole proprietor" in legal_type and cmd.requested_amount_usd > 250_000
    )
    if not is_sole_prop_over_limit:
        compliance_events.append(ComplianceRulePassed(
            application_id=cmd.application_id,
            rule_id="REG-004",
            rule_version=REGULATION_VERSION,
            evidence_hash=_hash({
                "legal_type": legal_type,
                "requested_amount": cmd.requested_amount_usd,
            }),
        ))
        rules_passed += 1
    else:
        compliance_events.append(ComplianceRuleFailed(
            application_id=cmd.application_id,
            rule_id="REG-004",
            rule_version=REGULATION_VERSION,
            failure_reason=(
                f"Sole Proprietor entities may not exceed $250,000. "
                f"Requested: ${cmd.requested_amount_usd:,.0f}."
            ),
            remediation_required=True,
        ))
        rules_failed += 1

    # REG-005 — Minimum Operating History (HARD BLOCK)
    rules_evaluated += 1
    founded_year = company.get("founded_year")
    if founded_year is None:
        # Unknown founding year — benefit of doubt, note it
        compliance_events.append(ComplianceRuleNoted(
            application_id=cmd.application_id,
            rule_id="REG-005",
            rule_version=REGULATION_VERSION,
            note_type="UNKNOWN_FOUNDING_YEAR",
            note_text="Founded year not recorded in registry. Manual verification required.",
        ))
        rules_passed += 1  # counted as pass pending manual review
    elif (CURRENT_YEAR - int(founded_year)) >= 2:
        compliance_events.append(ComplianceRulePassed(
            application_id=cmd.application_id,
            rule_id="REG-005",
            rule_version=REGULATION_VERSION,
            evidence_hash=_hash({
                "founded_year": founded_year,
                "years_operating": CURRENT_YEAR - int(founded_year),
            }),
        ))
        rules_passed += 1
    else:
        years = CURRENT_YEAR - int(founded_year)
        compliance_events.append(ComplianceRuleFailed(
            application_id=cmd.application_id,
            rule_id="REG-005",
            rule_version=REGULATION_VERSION,
            failure_reason=f"Company has only {years} year(s) of operating history. Minimum 2 required.",
            remediation_required=False,
        ))
        rules_failed += 1
        hard_block   = True
        block_reason = f"REG-005: insufficient operating history ({years} year(s))."

    if hard_block:
        return await _finalise(
            cmd, store, compliance_stream, compliance_events,
            rules_evaluated, rules_passed, rules_failed,
            hard_block, block_reason,
        )

    # REG-006 — CRA (always NOTED, never failed)
    rules_evaluated += 1
    compliance_events.append(ComplianceRuleNoted(
        application_id=cmd.application_id,
        rule_id="REG-006",
        rule_version=REGULATION_VERSION,
        note_type="CRA_CONSIDERATION",
        note_text=(
            f"Community Reinvestment Act consideration noted for "
            f"{company.get('company_name', cmd.applicant_id)} "
            f"in {jurisdiction}."
        ),
    ))

    return await _finalise(
        cmd, store, compliance_stream, compliance_events,
        rules_evaluated, rules_passed, rules_failed,
        hard_block, block_reason,
    )


async def _finalise(
    cmd: RunComplianceCommand,
    store: EventStore,
    compliance_stream: str,
    rule_events: list,
    rules_evaluated: int,
    rules_passed: int,
    rules_failed: int,
    hard_block: bool,
    block_reason: str,
) -> dict[str, Any]:
    """Append all rule events + ComplianceCheckCompleted, handle loan stream."""

    verdict = "BLOCKED" if hard_block else (
        "CONDITIONAL" if rules_failed > 0 else "CLEAR"
    )

    # LLM summary (best-effort)
    summary = await _generate_summary(
        verdict, rules_evaluated, rules_passed, rules_failed,
        hard_block, block_reason,
    )

    rule_events.append(ComplianceCheckCompleted(
        application_id=cmd.application_id,
        overall_verdict=verdict,
        has_hard_block=hard_block,
        rules_evaluated=rules_evaluated,
        rules_passed=rules_passed,
        rules_failed=rules_failed,
        summary=summary,
    ))

    # Append all compliance events atomically
    compliance_version = await store.stream_version(compliance_stream)
    expected = -1 if compliance_version == 0 else compliance_version
    await store.append(
        stream_id=compliance_stream,
        events=rule_events,
        expected_version=expected,
        correlation_id=cmd.correlation_id,
    )

    # Update loan stream
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    if hard_block:
        await store.append(
            stream_id=f"loan-{cmd.application_id}",
            events=[ApplicationDeclined(
                application_id=cmd.application_id,
                decline_reasons=[block_reason],
                declined_by=cmd.agent_id,
                adverse_action_notice_required=True,
            )],
            expected_version=app.version,
            correlation_id=cmd.correlation_id,
        )

    return {
        "compliance_stream": compliance_stream,
        "overall_verdict": verdict,
        "has_hard_block": hard_block,
        "block_reason": block_reason,
        "rules_evaluated": rules_evaluated,
        "rules_passed": rules_passed,
        "rules_failed": rules_failed,
        "summary": summary,
    }


async def _generate_summary(
    verdict: str,
    evaluated: int,
    passed: int,
    failed: int,
    hard_block: bool,
    block_reason: str,
) -> str:
    prompt = (
        f"Compliance check result: {verdict}. "
        f"{evaluated} rules evaluated: {passed} passed, {failed} failed. "
        f"{'Hard block: ' + block_reason if hard_block else 'No hard block.'} "
        "Write a one-sentence plain-English summary for a loan officer."
    )
    openai_key    = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    try:
        if openai_key:
            import openai
            client = openai.AsyncOpenAI(api_key=openai_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        if anthropic_key:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=anthropic_key)
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
    except Exception:
        pass
    return f"Compliance {verdict}: {passed}/{evaluated} rules passed."


def _hash(data: dict) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
