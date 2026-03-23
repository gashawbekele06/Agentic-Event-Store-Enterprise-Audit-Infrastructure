"""
src.what_if — Counterfactual What-If Projector package.

Public API re-exported from projector.py for convenience:

    from src.what_if import run_what_if, InMemoryApplicationSummary, WhatIfResult
"""
from src.what_if.projector import InMemoryApplicationSummary, WhatIfResult, run_what_if

__all__ = ["run_what_if", "WhatIfResult", "InMemoryApplicationSummary"]
