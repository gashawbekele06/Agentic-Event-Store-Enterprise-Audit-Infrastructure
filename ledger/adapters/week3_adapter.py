# ledger/adapters/week3_adapter.py
"""
Adapter that bridges Week 3 Document Intelligence Refinery
into Week 5 DocumentProcessingAgent.

Flow:
  PDF path
    → TriageAgent.triage()         (Week 3 - classify document)
    → ExtractionRouter.route()     (Week 3 - extract text/tables)
    → ExtractedDocument            (Week 3 output)
    → _parse_financial_facts()     (Adapter - map to FinancialFacts)
    → FinancialFacts               (Week 5 format)
"""
from __future__ import annotations

import os
import re
import sys
import asyncio
import threading
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ── Add Week 3 to Python path ─────────────────────────────────────────────
_WEEK3_PATH = os.getenv("WEEK3_PROJECT_PATH", "")
if _WEEK3_PATH and _WEEK3_PATH not in sys.path:
    sys.path.insert(0, _WEEK3_PATH)

# Week 3 is available if the path is configured and points to a real directory.
# The actual import happens lazily inside _run() to avoid the `src` namespace
# conflict that occurs when this project's src.* package is already cached.
WEEK3_AVAILABLE = bool(_WEEK3_PATH and Path(_WEEK3_PATH).is_dir())

# Lock ensures only one thread swaps sys.modules at a time.
_week3_import_lock = threading.Lock()


# ── FinancialFacts — matches what DocumentProcessingAgent expects ──────────
from dataclasses import dataclass, field

@dataclass
class FinancialFacts:
    total_revenue:      Optional[float] = None
    gross_profit:       Optional[float] = None
    operating_income:   Optional[float] = None
    ebitda:             Optional[float] = None
    net_income:         Optional[float] = None
    total_assets:       Optional[float] = None
    total_liabilities:  Optional[float] = None
    total_equity:       Optional[float] = None
    field_confidence:   dict = field(default_factory=dict)
    extraction_notes:   list = field(default_factory=list)


# ── Public interface ──────────────────────────────────────────────────────

async def extract_income_statement(file_path: str) -> FinancialFacts:
    """Extract income statement facts from a PDF."""
    if WEEK3_AVAILABLE:
        return await _extract_via_week3(file_path)
    return await _extract_fallback(file_path)


async def extract_balance_sheet(file_path: str) -> FinancialFacts:
    """Extract balance sheet facts from a PDF."""
    if WEEK3_AVAILABLE:
        return await _extract_via_week3(file_path)
    return await _extract_fallback(file_path)


# ── Week 3 extraction path ────────────────────────────────────────────────

async def _extract_via_week3(file_path: str) -> FinancialFacts:
    """
    Call Week 3 pipeline. Falls back to pdfplumber if Week 3
    returns empty results or raises an exception.
    """
    path = Path(file_path)
    loop = asyncio.get_event_loop()

    def _run():
        # Swap sys.modules so Week 3's `src` package loads instead of this
        # project's `src`. The lock serialises concurrent extractions.
        with _week3_import_lock:
            saved = {k: sys.modules.pop(k)
                     for k in list(sys.modules)
                     if k == "src" or k.startswith("src.")}
            try:
                from src.agents.triage import TriageAgent
                from src.agents.extractor import ExtractionRouter
                triage  = TriageAgent()
                router  = ExtractionRouter()
                profile = triage.triage(path)
                doc     = router.route(path, profile)
                return doc
            finally:
                # Remove Week 3's src entries, restore this project's.
                for k in [k for k in sys.modules
                          if k == "src" or k.startswith("src.")]:
                    del sys.modules[k]
                sys.modules.update(saved)

    try:
        doc = await loop.run_in_executor(None, _run)
        facts = _parse_financial_facts(doc)

        # If Week 3 returned nothing useful, fall back to pdfplumber
        if facts.total_revenue is None and facts.net_income is None:
            fallback = await _extract_fallback(file_path)
            # Merge: use fallback values for any fields Week 3 missed
            for field in ["total_revenue", "gross_profit", "operating_income",
                          "ebitda", "net_income", "total_assets",
                          "total_liabilities", "total_equity"]:
                if getattr(facts, field) is None and getattr(fallback, field) is not None:
                    setattr(facts, field, getattr(fallback, field))
                    facts.field_confidence[field] = fallback.field_confidence.get(field, 0.75)
        return facts

    except Exception as e:
        # Week 3 failed entirely — use pdfplumber
        fallback = await _extract_fallback(file_path)
        fallback.extraction_notes.insert(0, f"Week 3 failed ({e}), used pdfplumber")
        return fallback


def _parse_financial_facts(doc) -> FinancialFacts:
    """
    Map ExtractedDocument → FinancialFacts.

    Strategy:
    1. Try tables first — Week 3 extracts structured tables from GAAP PDFs
    2. Fall back to full_text regex if tables are empty or missing fields
    """
    facts:      dict = {}
    confidence: dict = {}
    notes:      list = []

    # ── 1. Parse from tables ──────────────────────────────────────────────
    for table in doc.tables:
        _parse_table(table, facts, confidence)

    # ── 2. Parse from full text (fallback for missing fields) ─────────────
    text = doc.full_text or " ".join(b.text for b in doc.text_blocks)
    if text:
        _parse_text(text, facts, confidence, notes)

    # ── 3. Note missing critical fields ───────────────────────────────────
    critical = ["total_revenue", "net_income", "total_assets"]
    for f in critical:
        if facts.get(f) is None:
            notes.append(f"{f}: not found in document")
            confidence[f] = 0.0

    return FinancialFacts(
        total_revenue     = facts.get("total_revenue"),
        gross_profit      = facts.get("gross_profit"),
        operating_income  = facts.get("operating_income"),
        ebitda            = facts.get("ebitda"),        # None if missing — never default to 0
        net_income        = facts.get("net_income"),
        total_assets      = facts.get("total_assets"),
        total_liabilities = facts.get("total_liabilities"),
        total_equity      = facts.get("total_equity"),
        field_confidence  = confidence,
        extraction_notes  = notes,
    )


def _parse_table(table, facts: dict, confidence: dict) -> None:
    """
    Extract financial figures from an ExtractedTable.
    GAAP tables typically have label in col 0, value in col 1 or 2.
    """
    LABEL_MAP = {
        "total revenue":       "total_revenue",
        "net sales":           "total_revenue",
        "revenue":             "total_revenue",
        "gross profit":        "gross_profit",
        "operating income":    "operating_income",
        "income from operations": "operating_income",
        "ebitda":              "ebitda",
        "net income":          "net_income",
        "net earnings":        "net_income",
        "net loss":            "net_income",
        "total assets":        "total_assets",
        "total liabilities":   "total_liabilities",
        "total equity":        "total_equity",
        "shareholders equity": "total_equity",
        "stockholders equity": "total_equity",
    }

    for row in table.rows:
        if not row:
            continue
        label = str(row[0]).lower().strip()

        for key, field_name in LABEL_MAP.items():
            if key in label and field_name not in facts:
                # Find first numeric value in row
                for cell in row[1:]:
                    val = _to_float(str(cell))
                    if val is not None:
                        facts[field_name] = val
                        confidence[field_name] = 0.85
                        break


def _parse_text(text: str, facts: dict, confidence: dict, notes: list) -> None:
    """
    Regex parser for GAAP income statement / balance sheet PDFs.
    Handles formats like:
      Revenue                $6,376,032
      Cost of Revenue        ($4,880,954)
      Net Income             $120,142
    """
    # Pattern: label ... optional $ ... number (with optional parentheses for negatives)
    NUM = r'\(?\$?([\d,]+(?:\.\d+)?)\)?'

    PATTERNS = {
        "total_revenue": [
            r"^Revenue\s+" + NUM,
            r"Total\s+(?:Net\s+)?(?:Revenue|Sales)\s+" + NUM,
            r"Net\s+Sales\s+" + NUM,
        ],
        "gross_profit": [
            r"Gross\s+Profit\s+" + NUM,
        ],
        "operating_income": [
            r"Operating\s+Income(?:\s+\(EBIT\))?\s+" + NUM,
            r"Income\s+from\s+Operations\s+" + NUM,
        ],
        "ebitda": [
            r"EBITDA\s+" + NUM,
        ],
        "net_income": [
            r"Net\s+Income\s+" + NUM,
            r"Net\s+(?:Earnings|Loss)\s+" + NUM,
        ],
        "total_assets": [
            r"Total\s+Assets\s+" + NUM,
        ],
        "total_liabilities": [
            r"Total\s+Liabilities\s+" + NUM,
        ],
        "total_equity": [
            r"Total\s+(?:Equity|Stockholders|Shareholders)[^\n]*\s+" + NUM,
        ],
    }

    for field_name, patterns in PATTERNS.items():
        if field_name in facts:
            continue
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                val = _to_float(m.group(1))
                if val is not None:
                    # Treat parenthesised values as negative
                    raw = m.group(0)
                    if raw.strip().startswith("(") or "(" in raw.split("$")[-1]:
                        val = -abs(val)
                    facts[field_name] = val
                    confidence[field_name] = 0.80
                    break



def _to_float(val: str) -> Optional[float]:
    """Convert string like '1,234,567' or '(567)' to float. Returns None if unparseable."""
    if not val:
        return None
    cleaned = val.replace(",", "").replace("$", "").strip()
    # Handle negative in parentheses: (1234) → -1234
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── pdfplumber fallback (when Week 3 is unavailable) ─────────────────────

async def _extract_fallback(file_path: str) -> FinancialFacts:
    """Pure pdfplumber extraction — used when Week 3 is not available."""
    loop = asyncio.get_event_loop()

    def _read():
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)

    facts:      dict = {}
    confidence: dict = {}
    notes:      list = []

    try:
        text = await loop.run_in_executor(None, _read)
        _parse_text(text, facts, confidence, notes)
    except Exception as e:
        notes.append(f"pdfplumber failed: {e}")

    critical = ["total_revenue", "net_income", "total_assets"]
    for f in critical:
        if facts.get(f) is None:
            notes.append(f"{f}: not found in document")
            confidence[f] = 0.0

    return FinancialFacts(
        total_revenue     = facts.get("total_revenue"),
        gross_profit      = facts.get("gross_profit"),
        operating_income  = facts.get("operating_income"),
        ebitda            = facts.get("ebitda"),
        net_income        = facts.get("net_income"),
        total_assets      = facts.get("total_assets"),
        total_liabilities = facts.get("total_liabilities"),
        total_equity      = facts.get("total_equity"),
        field_confidence  = confidence,
        extraction_notes  = notes,
    )