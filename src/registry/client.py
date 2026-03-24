"""
ApplicantRegistryClient — read-only queries against applicant_registry schema.

The registry is an external system. Agents query it; they never write to it.
All methods return plain dicts to avoid coupling agents to a specific model.
"""
from __future__ import annotations

from typing import Any


class ApplicantRegistryClient:
    def __init__(self, pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str) -> "ApplicantRegistryClient":
        import asyncpg
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def get_company(self, company_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT company_id, company_name, sector, legal_type, jurisdiction,
                       founded_year, risk_tier, trajectory, annual_revenue, employee_count
                FROM applicant_registry.companies
                WHERE company_id = $1
                """,
                company_id,
            )
            return dict(row) if row else None

    async def get_financial_history(
        self, company_id: str, years: int = 3
    ) -> list[dict[str, Any]]:
        """Return up to `years` most recent fiscal years, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT fiscal_year, total_revenue, net_income, gross_profit,
                       ebitda, total_assets, total_liabilities, total_equity,
                       revenue_growth_pct, ebitda_margin_pct
                FROM applicant_registry.financial_history
                WHERE company_id = $1
                ORDER BY fiscal_year DESC
                LIMIT $2
                """,
                company_id,
                years,
            )
            return [dict(r) for r in rows]

    async def get_compliance_flags(
        self, company_id: str, active_only: bool = True
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT flag_type, flag_reason, flagged_at, is_active
                FROM applicant_registry.compliance_flags
                WHERE company_id = $1
                  AND ($2 = FALSE OR is_active = TRUE)
                ORDER BY flagged_at DESC
                """,
                company_id,
                active_only,
            )
            return [dict(r) for r in rows]

    async def get_loan_relationships(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT loan_id, loan_amount_usd, status, default_occurred,
                       originated_at, closed_at
                FROM applicant_registry.loan_relationships
                WHERE company_id = $1
                ORDER BY originated_at DESC
                """,
                company_id,
            )
            return [dict(r) for r in rows]

    async def has_active_flag(self, company_id: str, flag_type: str) -> bool:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM applicant_registry.compliance_flags
                    WHERE company_id = $1 AND flag_type = $2 AND is_active = TRUE
                )
                """,
                company_id,
                flag_type,
            )
            return bool(val)

    async def had_prior_default(self, company_id: str) -> bool:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM applicant_registry.loan_relationships
                    WHERE company_id = $1 AND default_occurred = TRUE
                )
                """,
                company_id,
            )
            return bool(val)
