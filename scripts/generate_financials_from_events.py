# scripts/generate_financials_from_events.py
"""
Extracts financial history from seed_events.jsonl ExtractionCompleted events
and loads it into applicant_registry.financial_history.

The seed events contain full FinancialFacts in ExtractionCompleted payloads.
This is the only source of financial data available.

Run: uv run python scripts/generate_financials_from_events.py
"""

import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

EVENTS_FILE = Path("data/seed_events.jsonl")
DB_URL      = os.getenv("DATABASE_URL")


async def main():
    import asyncpg

    print(f"Connecting to {DB_URL}")
    conn = await asyncpg.connect(DB_URL)

    # ── Read seed events ──────────────────────────────────────────────────
    print(f"Reading {EVENTS_FILE}...")
    events = []
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    print(f"  Total events: {len(events)}")

    # ── Extract application → company mapping from ApplicationSubmitted ───
    app_to_company: dict[str, str] = {}
    for ev in events:
        if ev.get("event_type") == "ApplicationSubmitted":
            app_id     = ev["payload"]["application_id"]
            company_id = ev["payload"]["applicant_id"]
            app_to_company[app_id] = company_id

    print(f"  Applications found: {len(app_to_company)}")

    # ── Extract financial facts from ExtractionCompleted events ──────────
    # Use income_statement extractions (they have ebitda; balance_sheet does not)
    # Key: company_id → facts dict
    company_facts: dict[str, dict] = {}

    for ev in events:
        if ev.get("event_type") != "ExtractionCompleted":
            continue

        payload = ev.get("payload", {})
        if payload.get("document_type") != "income_statement":
            continue

        # Get application_id from stream_id (format: "docpkg-APEX-XXXX")
        stream_id = ev.get("stream_id", "")
        if not stream_id.startswith("docpkg-"):
            continue

        app_id = stream_id.replace("docpkg-", "")
        company_id = app_to_company.get(app_id)
        if not company_id:
            continue

        facts = payload.get("facts", {})
        if not facts:
            continue

        # Only store first extraction per company (avoid duplicates)
        if company_id not in company_facts:
            company_facts[company_id] = facts

    print(f"  Companies with financial facts: {len(company_facts)}")

    # ── Insert into financial_history ─────────────────────────────────────
    loaded = 0
    skipped = 0

    for company_id, facts in company_facts.items():
        # Parse numeric values safely
        def to_num(val):
            if val is None:
                return None
            try:
                return float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                return None

        fiscal_year = 2024  # all seed data is 2024

        try:
            await conn.execute("""
                INSERT INTO applicant_registry.financial_history
                  (company_id, fiscal_year, total_revenue, net_income,
                   gross_profit, ebitda, total_assets, total_liabilities,
                   total_equity, revenue_growth_pct, ebitda_margin_pct)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT DO NOTHING
            """,
                company_id,
                fiscal_year,
                to_num(facts.get("total_revenue")),
                to_num(facts.get("net_income")),
                to_num(facts.get("gross_profit")),
                to_num(facts.get("ebitda")),
                to_num(facts.get("total_assets")),
                to_num(facts.get("total_liabilities")),
                to_num(facts.get("total_equity")),
                to_num(facts.get("gross_margin")),   # closest to revenue_growth_pct
                to_num(facts.get("ebitda_margin_pct") or
                       (facts.get("ebitda") and facts.get("total_revenue") and
                        float(str(facts["ebitda"])) / float(str(facts["total_revenue"])) * 100)),
            )
            loaded += 1
        except Exception as e:
            print(f"  Warning {company_id}: {e}")
            skipped += 1

    # Also insert loan relationship placeholders from HistoricalProfileConsumed
    loan_loaded = 0
    seen_companies = set()

    for ev in events:
        if ev.get("event_type") != "HistoricalProfileConsumed":
            continue
        payload = ev.get("payload", {})
        app_id  = payload.get("application_id")
        company_id = app_to_company.get(app_id)
        if not company_id or company_id in seen_companies:
            continue

        seen_companies.add(company_id)
        has_prior   = payload.get("has_prior_loans", False)
        has_default = payload.get("has_defaults", False)

        if has_prior:
            try:
                await conn.execute("""
                    INSERT INTO applicant_registry.loan_relationships
                      (company_id, loan_id, status, default_occurred)
                    VALUES ($1, $2, $3, $4)
                """,
                    company_id,
                    f"LOAN-{company_id}-HIST",
                    "CLOSED",
                    has_default,
                )
                loan_loaded += 1
            except Exception:
                pass

    await conn.close()

    # ── Final check ───────────────────────────────────────────────────────
    verify = await asyncpg.connect(DB_URL)
    companies  = await verify.fetchval("SELECT count(*) FROM applicant_registry.companies")
    financials = await verify.fetchval("SELECT count(*) FROM applicant_registry.financial_history")
    flags      = await verify.fetchval("SELECT count(*) FROM applicant_registry.compliance_flags")
    loans      = await verify.fetchval("SELECT count(*) FROM applicant_registry.loan_relationships")
    ev_count   = await verify.fetchval("SELECT count(*) FROM events")
    await verify.close()

    print(f"\n  {'='*45}")
    print(f"  Financial rows loaded:  {loaded}  (skipped: {skipped})")
    print(f"  Loan rows loaded:       {loan_loaded}")
    print(f"  {'='*45}")
    print(f"  companies:    {companies}")
    print(f"  financials:   {financials}")
    print(f"  flags:        {flags}")
    print(f"  loans:        {loans}")
    print(f"  events:       {ev_count}")
    print(f"  {'='*45}")


if __name__ == "__main__":
    asyncio.run(main())