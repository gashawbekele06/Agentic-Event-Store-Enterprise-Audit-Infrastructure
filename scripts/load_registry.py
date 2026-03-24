"""
Loads data/applicant_profiles.json into applicant_registry schema.
Run from project root: uv run python scripts/load_registry.py
"""

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATA_FILE = Path("data/applicant_profiles.json")
DB_URL    = os.getenv("DATABASE_URL")


def parse_date(val) -> date | None:
    """Convert a date string like '2025-08-27' to datetime.date, or None."""
    if not val:
        return None
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


async def main():
    import asyncpg

    print(f"Connecting to {DB_URL}")
    conn = await asyncpg.connect(DB_URL)

    print("Creating applicant_registry schema...")
    await conn.execute("""
        CREATE SCHEMA IF NOT EXISTS applicant_registry;

        CREATE TABLE IF NOT EXISTS applicant_registry.companies (
            company_id    TEXT PRIMARY KEY,
            company_name  TEXT NOT NULL,
            sector        TEXT,
            legal_type    TEXT,
            jurisdiction  TEXT,
            founded_year  INT,
            risk_tier     TEXT,
            trajectory    TEXT,
            annual_revenue  NUMERIC,
            employee_count  INT,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS applicant_registry.financial_history (
            id                 SERIAL PRIMARY KEY,
            company_id         TEXT NOT NULL,
            fiscal_year        INT NOT NULL,
            total_revenue      NUMERIC,
            net_income         NUMERIC,
            gross_profit       NUMERIC,
            ebitda             NUMERIC,
            total_assets       NUMERIC,
            total_liabilities  NUMERIC,
            total_equity       NUMERIC,
            revenue_growth_pct NUMERIC,
            ebitda_margin_pct  NUMERIC
        );

        CREATE TABLE IF NOT EXISTS applicant_registry.compliance_flags (
            id           SERIAL PRIMARY KEY,
            company_id   TEXT NOT NULL,
            flag_type    TEXT NOT NULL,
            flag_reason  TEXT,
            flagged_at   DATE,
            is_active    BOOLEAN DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS applicant_registry.loan_relationships (
            id               SERIAL PRIMARY KEY,
            company_id       TEXT NOT NULL,
            loan_id          TEXT,
            loan_amount_usd  NUMERIC,
            status           TEXT,
            default_occurred BOOLEAN DEFAULT FALSE,
            originated_at    TIMESTAMPTZ,
            closed_at        TIMESTAMPTZ
        );
    """)

    print(f"Loading {DATA_FILE}...")
    with open(DATA_FILE, encoding="utf-8") as f:
        profiles = json.load(f)

    companies_loaded = 0
    flags_loaded     = 0

    for p in profiles:
        await conn.execute("""
            INSERT INTO applicant_registry.companies
              (company_id, company_name, sector, legal_type, jurisdiction,
               risk_tier, trajectory)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (company_id) DO NOTHING
        """,
            p["company_id"],
            p.get("name"),
            p.get("industry"),       # JSON: 'industry'  → DB: 'sector'
            p.get("legal_type"),
            p.get("jurisdiction"),
            p.get("risk_segment"),   # JSON: 'risk_segment' → DB: 'risk_tier'
            p.get("trajectory"),
        )
        companies_loaded += 1

        for flag in p.get("compliance_flags", []):
            await conn.execute("""
                INSERT INTO applicant_registry.compliance_flags
                  (company_id, flag_type, flag_reason, flagged_at, is_active)
                VALUES ($1, $2, $3, $4, $5)
            """,
                p["company_id"],
                flag.get("flag_type"),
                flag.get("note"),
                parse_date(flag.get("added_date")),   # ← fixed: str → date
                flag.get("is_active", True),
            )
            flags_loaded += 1

    await conn.close()

    print(f"\n  Companies loaded:   {companies_loaded}")
    print(f"  Flags loaded:       {flags_loaded}")
    print(f"  Financial history:  0  (run export_financials_from_starter.py next)")
    print(f"  Loan relationships: 0  (run export_financials_from_starter.py next)")
    print("\n  Base registry loaded.")


if __name__ == "__main__":
    asyncio.run(main())