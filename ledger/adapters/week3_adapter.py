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

import json
import os
import re
import subprocess
import sys
import asyncio
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_WEEK3_PATH = os.getenv("WEEK3_PROJECT_PATH", "")
WEEK3_AVAILABLE = bool(_WEEK3_PATH and Path(_WEEK3_PATH).is_dir())

# ── Subprocess script — runs entirely in Week 3's namespace ───────────────
# Passed to `python -c`. Braces doubled because it's a .format() template.
_EXTRACT_SCRIPT = """\
import sys, json
sys.path.insert(0, {week3_path!r})
from src.agents.triage import TriageAgent
from src.agents.extractor import ExtractionRouter
from pathlib import Path

path = Path({file_path!r})
triage = TriageAgent()
router = ExtractionRouter()
profile = triage.triage(path)
doc = router.route(path, profile)

tables = []
for t in (doc.tables or []):
    tables.append([[str(c) for c in row] for row in t.rows])

print(json.dumps({{
    "tables": tables,
    "full_text": doc.full_text or "",
    "text_blocks": [b.text for b in (doc.text_blocks or [])],
}}))
"""


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

def _pdf_is_digital(file_path: str, min_chars: int = 100) -> bool:
    """
    Return True if the PDF has extractable digital text on its first 3 pages.
    Takes < 50 ms for a typical financial PDF.
    """
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages[:3]:
                if len((page.extract_text() or "").strip()) >= min_chars:
                    return True
    except Exception:
        pass
    return False


async def _extract_via_week3(file_path: str) -> FinancialFacts:
    """
    Smart dispatcher:
      - Digital PDF  → pdfplumber fast path (tables + text, < 1 s)
      - Scanned PDF  → Week 3 subprocess (OCR, slower but necessary)

    Week 3's TriageAgent misclassifies native-digital PDFs as scanned images,
    routing them to a slow OCR pipeline.  The pre-flight check fixes this by
    detecting extractable text before ever launching the subprocess.
    """
    path = Path(file_path)
    loop = asyncio.get_running_loop()

    # ── Pre-flight: is the PDF digital? ──────────────────────────────────────
    is_digital = await loop.run_in_executor(None, _pdf_is_digital, str(path))

    if is_digital:
        print(f"[week3_adapter] Digital PDF — fast pdfplumber path: {path.name}")
        facts = await _extract_pdfplumber(file_path)
        print(f"[week3_adapter] Extraction OK: {path.name} "
              f"(revenue={facts.total_revenue}, net_income={facts.net_income})")
        return facts

    # ── Scanned / image-based PDF: use Week 3 OCR subprocess ─────────────────
    print(f"[week3_adapter] Scanned PDF detected — Week 3 OCR path: {path.name}")

    def _run_subprocess() -> dict:
        script = _EXTRACT_SCRIPT.format(
            week3_path=_WEEK3_PATH,
            file_path=str(path),
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Week 3 subprocess exited non-zero")
        return json.loads(result.stdout)

    try:
        raw = await loop.run_in_executor(None, _run_subprocess)
        facts = _parse_financial_facts_from_raw(raw)
        print(f"[week3_adapter] Week 3 OCR OK: {path.name} "
              f"(revenue={facts.total_revenue}, net_income={facts.net_income})")

        if facts.total_revenue is None and facts.net_income is None:
            print("[week3_adapter] Week 3 returned no financials — merging pdfplumber fallback")
            fallback = await _extract_pdfplumber(file_path)
            for field_name in ["total_revenue", "gross_profit", "operating_income",
                               "ebitda", "net_income", "total_assets",
                               "total_liabilities", "total_equity"]:
                if getattr(facts, field_name) is None and getattr(fallback, field_name) is not None:
                    setattr(facts, field_name, getattr(fallback, field_name))
                    facts.field_confidence[field_name] = fallback.field_confidence.get(field_name, 0.75)
        return facts

    except Exception as e:
        print(f"[week3_adapter] Week 3 failed ({type(e).__name__}: {e}) — using pdfplumber")
        fallback = await _extract_pdfplumber(file_path)
        fallback.extraction_notes.insert(0, f"Week 3 OCR failed ({e}), used pdfplumber")
        return fallback


def _parse_financial_facts_from_raw(raw: dict) -> FinancialFacts:
    """
    Parse subprocess JSON output (plain dict with tables/full_text/text_blocks)
    into FinancialFacts — no ExtractedDocument object needed.

    tables is list[list[list[str]]]: outer = tables, middle = rows, inner = cells.
    """
    facts:      dict = {}
    confidence: dict = {}
    notes:      list = []

    # ── 1. Parse from tables ──────────────────────────────────────────────
    for table_rows in raw.get("tables", []):
        # Each table_rows is a list of rows (list of str); _parse_table_rows
        # works with plain lists because _parse_table only needs row[0] and row[1:]
        _parse_table_rows(table_rows, facts, confidence)

    # ── 2. Parse from full text ───────────────────────────────────────────
    text = raw.get("full_text") or " ".join(raw.get("text_blocks", []))
    if text:
        _parse_text(text, facts, confidence, notes)

    # ── 3. Note missing critical fields ───────────────────────────────────
    for f in ["total_revenue", "net_income", "total_assets"]:
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


_LABEL_MAP = {
    "total revenue":          "total_revenue",
    "net sales":              "total_revenue",
    "revenue":                "total_revenue",
    "gross profit":           "gross_profit",
    "operating income":       "operating_income",
    "income from operations": "operating_income",
    "ebitda":                 "ebitda",
    "net income":             "net_income",
    "net earnings":           "net_income",
    "net loss":               "net_income",
    "total assets":           "total_assets",
    "total liabilities":      "total_liabilities",
    "total equity":           "total_equity",
    "shareholders equity":    "total_equity",
    "stockholders equity":    "total_equity",
}


def _parse_table_rows(rows: list, facts: dict, confidence: dict) -> None:
    """Parse a plain list-of-rows (list[list[str]]) — used by the subprocess path."""
    for row in rows:
        if not row:
            continue
        label = str(row[0]).lower().strip()
        for key, field_name in _LABEL_MAP.items():
            if key in label and field_name not in facts:
                for cell in row[1:]:
                    val = _to_float(str(cell))
                    if val is not None:
                        facts[field_name] = val
                        confidence[field_name] = 0.85
                        break


def _parse_table(table, facts: dict, confidence: dict) -> None:
    """
    Extract financial figures from an ExtractedTable.
    GAAP tables typically have label in col 0, value in col 1 or 2.
    """
    _parse_table_rows(table.rows, facts, confidence)


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


# ── pdfplumber extraction (digital PDFs and fallback) ────────────────────

async def _extract_pdfplumber(file_path: str) -> FinancialFacts:
    """
    Full pdfplumber extraction: tables first (high confidence), then text.
    Fast for digital PDFs (< 500 ms).  Used as:
      - Primary path for native-digital PDFs
      - Fallback when Week 3 OCR fails or is unavailable
    """
    loop = asyncio.get_running_loop()

    def _read():
        import pdfplumber
        tables_raw: list[list[list[str]]] = []
        text_parts: list[str] = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                # Extract structured tables (preserves row/column layout)
                for tbl in (page.extract_tables() or []):
                    if tbl:
                        tables_raw.append(
                            [[str(cell or "") for cell in row] for row in tbl]
                        )
                # Extract plain text for regex fallback
                text_parts.append(page.extract_text() or "")
        return tables_raw, "\n".join(text_parts)

    facts:      dict = {}
    confidence: dict = {}
    notes:      list = []

    try:
        tables_raw, text = await loop.run_in_executor(None, _read)
        # Tables first — structured data is more reliable than regex on text
        for tbl in tables_raw:
            _parse_table_rows(tbl, facts, confidence)
        # Text for any fields not found in tables
        if text:
            _parse_text(text, facts, confidence, notes)
    except Exception as e:
        notes.append(f"pdfplumber failed: {e}")

    for f in ["total_revenue", "net_income", "total_assets"]:
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


# Keep old name as alias so existing callers (tests etc.) don't break
_extract_fallback = _extract_pdfplumber