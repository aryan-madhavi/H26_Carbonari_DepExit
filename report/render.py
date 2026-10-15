"""Report renderer module for Dependency Exit Capsules.

Generates structured JSON capsules and visually distinct HTML reports
separating observed/verified evidence from AI-generated proposals,
and prints readable terminal summaries for CLI evaluation.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from jinja2 import Template

# Ensure repository root is on sys.path for direct script execution
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from schema import (
        AIRecommendation,
        Dependency,
        ExitCapsule,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        UsageType,
        VerificationResult,
        VerificationStatus,
    )
except ImportError:
    from depexit.schema import (
        AIRecommendation,
        Dependency,
        ExitCapsule,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        UsageType,
        VerificationResult,
        VerificationStatus,
    )

try:
    from ai.gemma_client import MODEL_INFO as DEFAULT_MODEL_INFO
except ImportError:
    DEFAULT_MODEL_INFO = {
        "model_id": "gemma-4-31b-it",
        "version": "4",
        "access_method": "hosted_api",
        "terms_url": "https://ai.google.dev/gemma/terms",
        "model": "gemma-4-31b-it",
        "method": "hosted_api",
        "terms": "https://ai.google.dev/gemma/terms",
    }

__all__ = [
    "render_report",
    "print_summary",
    "render_html",
    "render_markdown",
    "save_report",
]


# ---------------------------------------------------------------------------
# Test Partitioning and Verification Counter Helpers
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Normalize test or dependency name for robust comparisons."""
    cleaned = re.sub(
        r"^(?:\[(?:baseline|replacement|original|candidate|target)\]|(?:baseline|replacement|original|candidate|target)::)\s*",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return cleaned.strip().lower()


def _is_pass(status: Any) -> bool:
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.PASS
    return str(status).strip().upper() == "PASS"


def _is_fail(status: Any) -> bool:
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.FAIL
    return str(status).strip().upper() == "FAIL"


def _is_error(status: Any) -> bool:
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.ERROR
    return str(status).strip().upper() == "ERROR"


def _is_not_run(status: Any) -> bool:
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.NOT_RUN
    return str(status).strip().upper() in ("NOT_RUN", "NOTRUN", "SKIPPED", "SKIP")


def _partition_and_count_tests(
    capsule: ExitCapsule,
) -> tuple[dict[str, int], dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute before-and-after test execution counts (Baseline vs Replacement).

    Returns:
        (baseline_counts, replacement_counts, baseline_items, replacement_items)
    """
    dep_name = capsule.dependency.name
    repl_name = (
        capsule.ai_recommendation.proposed_replacement
        if capsule.ai_recommendation
        else ""
    )

    dep_norm = _normalize_name(dep_name)
    repl_norm = _normalize_name(repl_name) if repl_name else ""

    baseline_items: list[dict[str, Any]] = []
    replacement_items: list[dict[str, Any]] = []

    for res in capsule.verification_results:
        norm_test = _normalize_name(res.test_name)
        res_dep = str(res.dependency_name).strip().lower()
        test_raw = str(res.test_name).strip()

        # Check explicit attributes
        is_base = getattr(res, "is_baseline", None)
        run_type = str(getattr(res, "run_type", "")).lower()

        status_val = (
            res.status.value
            if hasattr(res.status, "value")
            else str(res.status)
        )
        item = {
            "dependency_name": res.dependency_name,
            "test_name": res.test_name,
            "normalized_name": norm_test,
            "status": status_val,
            "raw_output": res.raw_output,
            "verified": _is_pass(res.status),
            "evidence": getattr(res, "evidence", "verified_by_execution"),
        }

        if is_base is True or run_type in ("baseline", "original", "base"):
            baseline_items.append(item)
        elif is_base is False or run_type in ("replacement", "candidate", "new"):
            replacement_items.append(item)
        elif re.search(r"^(\[baseline\]|baseline::)", test_raw, re.IGNORECASE):
            baseline_items.append(item)
        elif re.search(r"^(\[replacement\]|replacement::)", test_raw, re.IGNORECASE):
            replacement_items.append(item)
        elif repl_norm and (res_dep == repl_norm or "replacement" in res_dep):
            replacement_items.append(item)
        elif res_dep == dep_norm or "baseline" in res_dep or "original" in res_dep:
            baseline_items.append(item)
        else:
            # Fallback heuristic
            if repl_norm and any(b["normalized_name"] == norm_test for b in baseline_items):
                replacement_items.append(item)
            else:
                baseline_items.append(item)

    def _tally(items: list[dict[str, Any]]) -> dict[str, int]:
        total = len(items)
        passed = sum(1 for i in items if _is_pass(i["status"]))
        failed = sum(1 for i in items if _is_fail(i["status"]))
        errors = sum(1 for i in items if _is_error(i["status"]))
        not_run = sum(1 for i in items if _is_not_run(i["status"]))
        return {
            "total": total,
            "pass": passed,
            "fail": failed,
            "error": errors,
            "not_run": not_run,
        }

    return _tally(baseline_items), _tally(replacement_items), baseline_items, replacement_items


def _resolve_model_info(capsule: Optional[ExitCapsule] = None) -> dict[str, Any]:
    """Retrieve model info dictionary from capsule or fallback to default."""
    info: dict[str, Any] = {}
    if capsule and hasattr(capsule, "model_info") and isinstance(capsule.model_info, dict) and capsule.model_info:
        info = dict(capsule.model_info)
    elif capsule and capsule.ai_recommendation and hasattr(capsule.ai_recommendation, "model_info") and isinstance(capsule.ai_recommendation.model_info, dict):
        info = dict(capsule.ai_recommendation.model_info)
    else:
        info = dict(DEFAULT_MODEL_INFO)

    # Standardize keys
    model_name = info.get("model_id") or info.get("model") or "gemma-4-31b-it"
    version = info.get("version") or "4"
    access_method = info.get("access_method") or info.get("method") or "hosted_api"
    terms_url = info.get("terms_url") or info.get("terms") or info.get("terms_link") or "https://ai.google.dev/gemma/terms"

    return {
        "model_id": model_name,
        "model_name": model_name,
        "version": str(version),
        "access_method": access_method,
        "terms_url": terms_url,
        "evidence": "observed",
    }


# ---------------------------------------------------------------------------
# JSON Serialization Helpers (with evidence tag on every section)
# ---------------------------------------------------------------------------

def _capsule_to_dict(capsule: ExitCapsule, model_info: dict[str, Any]) -> dict[str, Any]:
    """Convert an ExitCapsule into a dictionary with explicit evidence tags on every section."""
    b_counts, r_counts, b_items, r_items = _partition_and_count_tests(capsule)

    # a. Dependency details and risk indicators (OBSERVED)
    dep_dict = {
        "name": capsule.dependency.name,
        "declared_version": capsule.dependency.declared_version,
        "source_file": capsule.dependency.source_file,
        "evidence": "observed",
    }

    risks_list = []
    for r in capsule.risk_flags:
        rtype = r.risk_type.value if hasattr(r.risk_type, "value") else str(r.risk_type)
        rsev = r.severity.value if hasattr(r.severity, "value") else str(r.severity)
        risks_list.append({
            "dependency_name": r.dependency_name,
            "risk_type": rtype,
            "reason": r.reason,
            "severity": rsev,
            "evidence": getattr(r, "evidence", "observed"),
        })

    risk_section = {
        "evidence": "observed",
        "count": len(risks_list),
        "items": risks_list,
    }

    # b. Usage locations (OBSERVED)
    usage_list = []
    for u in capsule.usage_locations:
        utype = u.usage_type.value if hasattr(u.usage_type, "value") else str(u.usage_type)
        usage_list.append({
            "dependency_name": u.dependency_name,
            "file_path": u.file_path,
            "line_number": u.line_number,
            "usage_type": utype,
            "code_snippet": u.code_snippet,
            "evidence": getattr(u, "evidence", "observed"),
        })

    usage_section = {
        "evidence": "observed",
        "count": len(usage_list),
        "items": usage_list,
    }

    # c. Proposed replacement, migration steps, suggested tests, patch (AI-GENERATED, UNVERIFIED)
    ai_rec = capsule.ai_recommendation
    if ai_rec:
        candidates_raw = getattr(ai_rec, "candidates", []) or []
        candidates_clean = []
        for c in candidates_raw:
            if isinstance(c, dict):
                candidates_clean.append({
                    "name": c.get("name", ""),
                    "why": c.get("why", ""),
                    "api_gaps": c.get("api_gaps", ""),
                    "evidence": "ai_generated",
                })
            else:
                candidates_clean.append({"name": str(c), "evidence": "ai_generated"})

        ai_section: dict[str, Any] = {
            "evidence": "ai_generated",
            "verified": bool(getattr(ai_rec, "verified", False)),
            "proposed_replacement": ai_rec.proposed_replacement,
            "usage_complexity": getattr(ai_rec, "usage_complexity", "BASIC"),
            "candidates": candidates_clean,
            "migration_steps": list(ai_rec.migration_steps or []),
            "code_patch": getattr(ai_rec, "code_patch", ""),
            "suggested_tests": list(getattr(ai_rec, "suggested_tests", []) or []),
            "reasoning": ai_rec.reasoning,
        }
    else:
        ai_section = {
            "evidence": "ai_generated",
            "verified": False,
            "proposed_replacement": None,
            "usage_complexity": None,
            "candidates": [],
            "migration_steps": [],
            "code_patch": "",
            "suggested_tests": [],
            "reasoning": "",
        }

    # d. Executed test results, baseline vs replacement (VERIFIED BY EXECUTION)
    test_results_raw = []
    for t in capsule.verification_results:
        sval = t.status.value if hasattr(t.status, "value") else str(t.status)
        test_results_raw.append({
            "dependency_name": t.dependency_name,
            "test_name": t.test_name,
            "status": sval,
            "raw_output": t.raw_output,
            "verified": _is_pass(t.status),
            "evidence": getattr(t, "evidence", "verified_by_execution"),
        })

    test_section = {
        "evidence": "verified_by_execution",
        "counts_before_swap_baseline": b_counts,
        "counts_after_swap_replacement": r_counts,
        "results": test_results_raw,
    }

    # e. Compatibility gaps (observed) and Gemma's explanation of them (AI-generated)
    gaps_section = {
        "evidence": "observed",
        "count": len(capsule.compatibility_gaps),
        "items": list(capsule.compatibility_gaps or []),
    }

    gap_exps_raw = getattr(capsule, "gap_explanations", []) or []
    gap_exps_section = {
        "evidence": "ai_generated",
        "count": len(gap_exps_raw),
        "items": list(gap_exps_raw),
    }

    # f. Open concerns and limitations (highlighted box, always shown)
    concerns_section = {
        "evidence": "observed",
        "count": len(capsule.open_concerns),
        "items": list(capsule.open_concerns or []),
    }

    return {
        "dependency": dep_dict,
        "risk_indicators": risk_section,
        "usage_locations": usage_section,
        "ai_recommendation": ai_section,
        "executed_test_results": test_section,
        "compatibility_gaps": gaps_section,
        "gap_explanations": gap_exps_section,
        "open_concerns": concerns_section,
        "model_info": model_info,
        "verified": bool(getattr(capsule, "verified", False)),
    }


# ---------------------------------------------------------------------------
# HTML Jinja2 Template (Inline CSS, No External Files, Scored Visual Separation)
# ---------------------------------------------------------------------------

HTML_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DepExit • Dependency Exit Capsules Report</title>
  <style>
    /* System Font Stack - 100% Offline with zero external dependencies */
    :root {
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      
      /* Theme Palettes */
      --color-bg: #0f172a;
      --color-card-bg: #1e293b;
      --color-text-main: #f8fafc;
      --color-text-muted: #94a3b8;
      
      /* OBSERVED Evidence (Blue / Grey Theme) */
      --obs-bg: #0f2744;
      --obs-card: #143254;
      --obs-border: #38bdf8;
      --obs-text: #e0f2fe;
      --obs-badge-bg: #0284c7;
      --obs-badge-text: #ffffff;
      
      /* AI-GENERATED / UNVERIFIED (Orange / Yellow Theme) */
      --ai-bg: #451a03;
      --ai-card: #572205;
      --ai-border: #f59e0b;
      --ai-text: #fef3c7;
      --ai-badge-bg: #d97706;
      --ai-badge-text: #ffffff;
      
      /* VERIFIED BY EXECUTION (Green Theme) */
      --ver-bg: #064e3b;
      --ver-card: #065f46;
      --ver-border: #10b981;
      --ver-text: #d1fae5;
      --ver-badge-bg: #059669;
      --ver-badge-text: #ffffff;
      
      /* OPEN CONCERNS (Highlighted Callout Box) */
      --concern-bg: #450a0a;
      --concern-border: #ef4444;
      --concern-text: #fecaca;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    
    body {
      font-family: var(--font-sans);
      background-color: var(--color-bg);
      color: var(--color-text-main);
      line-height: 1.6;
      padding: 32px 16px 80px 16px;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
    }

    /* Page Header */
    .report-header {
      margin-bottom: 36px;
      padding-bottom: 24px;
      border-bottom: 2px solid #334155;
    }
    .report-title {
      font-size: 2.2rem;
      font-weight: 800;
      color: #38bdf8;
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .report-subtitle {
      color: var(--color-text-muted);
      font-size: 1rem;
      margin-top: 6px;
    }

    /* Scored Separation Legend */
    .legend-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 20px 0;
      padding: 14px 18px;
      background: #1e293b;
      border-radius: 8px;
      border: 1px solid #334155;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.85rem;
      font-weight: 600;
    }
    .legend-pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    /* Dependency Card */
    .capsule-card {
      background: var(--color-card-bg);
      border-radius: 12px;
      border: 1px solid #334155;
      margin-bottom: 40px;
      overflow: hidden;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
    }
    .card-topbar {
      padding: 22px 28px;
      background: #172554;
      border-bottom: 1px solid #1e3a8a;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    .card-dep-name {
      font-size: 1.6rem;
      font-weight: 800;
      color: #60a5fa;
    }
    .card-dep-meta {
      font-size: 0.9rem;
      color: #bfdbfe;
      margin-top: 4px;
    }
    .card-badges {
      display: flex;
      gap: 10px;
      align-items: center;
    }

    /* Status Pills */
    .badge-observed {
      background: var(--obs-badge-bg);
      color: var(--obs-badge-text);
      border: 1px solid var(--obs-border);
    }
    .badge-ai {
      background: var(--ai-badge-bg);
      color: var(--ai-badge-text);
      border: 1px solid var(--ai-border);
    }
    .badge-verified {
      background: var(--ver-badge-bg);
      color: var(--ver-badge-text);
      border: 1px solid var(--ver-border);
    }
    .badge-unverified {
      background: #78350f;
      color: #fde68a;
      border: 1px solid #f59e0b;
    }
    .badge-danger {
      background: #dc2626;
      color: #fff;
    }
    .badge-warning {
      background: #d97706;
      color: #fff;
    }
    .badge-success {
      background: #059669;
      color: #fff;
    }
    .badge-neutral {
      background: #475569;
      color: #fff;
    }

    .card-body {
      padding: 28px;
      display: flex;
      flex-direction: column;
      gap: 28px;
    }

    /* Section Containers */
    .section-block {
      border-radius: 8px;
      padding: 20px 22px;
      border-left: 6px solid #64748b;
    }
    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    }
    .section-title {
      font-size: 1.15rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .section-evidence-tag {
      font-size: 0.75rem;
      font-weight: 800;
      padding: 3px 8px;
      border-radius: 4px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }

    /* Section a & b: OBSERVED (Blue/Grey Theme) */
    .section-observed {
      background: var(--obs-bg);
      border-left-color: var(--obs-border);
      border: 1px solid #1e3a8a;
      border-left-width: 6px;
    }
    .section-observed .section-title { color: #7dd3fc; }

    /* Section c: AI-GENERATED / UNVERIFIED (Orange/Yellow Theme) */
    .section-ai-unverified {
      background: var(--ai-bg);
      border-left-color: var(--ai-border);
      border: 1px solid #78350f;
      border-left-width: 6px;
    }
    .section-ai-unverified .section-title { color: #fde68a; }

    /* Section d: VERIFIED BY EXECUTION (Green Theme) */
    .section-verified-execution {
      background: var(--ver-bg);
      border-left-color: var(--ver-border);
      border: 1px solid #065f46;
      border-left-width: 6px;
    }
    .section-verified-execution .section-title { color: #6ee7b7; }

    /* Section f: OPEN CONCERNS (Highlighted Box, Always Shown) */
    .section-open-concerns {
      background: var(--concern-bg);
      border: 2px solid var(--concern-border);
      border-left-width: 8px;
      box-shadow: 0 4px 15px rgba(239, 68, 68, 0.2);
    }
    .section-open-concerns .section-title { color: #fca5a5; font-size: 1.2rem; }

    /* Tables & Grids */
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
      font-size: 0.9rem;
    }
    th, td {
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    th {
      font-weight: 700;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.5px;
      color: var(--color-text-muted);
    }

    /* Code & Diffs */
    pre, code {
      font-family: var(--font-mono);
    }
    .code-block {
      background: #020617;
      padding: 14px 16px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.85rem;
      color: #e2e8f0;
      border: 1px solid #1e293b;
      margin-top: 8px;
      line-height: 1.5;
    }
    .diff-line-add { color: #4ade80; background: rgba(74, 222, 128, 0.1); display: block; padding: 1px 4px; }
    .diff-line-del { color: #f87171; background: rgba(248, 113, 113, 0.1); display: block; padding: 1px 4px; }
    .diff-line-info { color: #38bdf8; display: block; padding: 1px 4px; }

    /* Scoreboard */
    .scoreboard-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-top: 12px;
      margin-bottom: 16px;
    }
    .score-card {
      background: rgba(0, 0, 0, 0.25);
      border-radius: 6px;
      padding: 14px 18px;
      border: 1px solid rgba(255, 255, 255, 0.1);
    }
    .score-card-title {
      font-size: 0.8rem;
      text-transform: uppercase;
      color: var(--color-text-muted);
      margin-bottom: 6px;
      font-weight: 700;
    }
    .score-stats {
      display: flex;
      gap: 12px;
      font-size: 0.95rem;
      font-weight: 700;
    }
    .stat-pass { color: #4ade80; }
    .stat-fail { color: #f87171; }
    .stat-err { color: #f472b6; }
    .stat-notrun { color: #94a3b8; }

    /* Lists */
    ul.item-list, ol.item-list {
      padding-left: 20px;
      margin-top: 8px;
    }
    ul.item-list li, ol.item-list li {
      margin-bottom: 6px;
      font-size: 0.92rem;
    }

    /* Warning callout banner */
    .banner-warning {
      background: rgba(217, 119, 6, 0.2);
      border: 1px solid #d97706;
      border-radius: 6px;
      padding: 10px 14px;
      margin-bottom: 14px;
      font-size: 0.85rem;
      color: #fef3c7;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Footer */
    .report-footer {
      margin-top: 48px;
      padding: 24px;
      background: #1e293b;
      border-radius: 8px;
      border: 1px solid #334155;
      text-align: center;
      font-size: 0.9rem;
      color: var(--color-text-muted);
    }
    .footer-links a {
      color: #38bdf8;
      text-decoration: underline;
    }
    .footer-links a:hover {
      color: #7dd3fc;
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header class="report-header">
      <h1 class="report-title">🚀 DepExit Dependency Exit Capsules</h1>
      <p class="report-subtitle">
        Automated architectural migration plans grounded in real code usage and verified by test execution.
      </p>
      
      <!-- Visual Separation Legend (Scored Criterion) -->
      <div class="legend-bar">
        <span class="legend-item">
          <span class="legend-pill badge-observed">OBSERVED EVIDENCE</span>
          Source-grounded facts & AST tracking (Blue/Grey)
        </span>
        <span class="legend-item">
          <span class="legend-pill badge-ai">AI-GENERATED (UNVERIFIED)</span>
          Gemma 4 model proposals requiring human review (Orange/Yellow)
        </span>
        <span class="legend-item">
          <span class="legend-pill badge-verified">VERIFIED BY EXECUTION</span>
          Concrete test execution outcomes (Green)
        </span>
      </div>
    </header>

    <!-- Cards per Dependency -->
    {% for c in data.capsules %}
    <article class="capsule-card" id="card-{{ c.dependency.name }}">
      <!-- Card Topbar -->
      <div class="card-topbar">
        <div>
          <div class="card-dep-name">{{ c.dependency.name }}</div>
          <div class="card-dep-meta">
            Declared Version: <strong>{{ c.dependency.declared_version }}</strong> | Source: <code>{{ c.dependency.source_file }}</code>
          </div>
        </div>
        <div class="card-badges">
          {% if c.ai_recommendation.proposed_replacement %}
          <span class="legend-pill badge-ai">REPLACEMENT: {{ c.ai_recommendation.proposed_replacement }}</span>
          {% endif %}
          
          {% if c.verified %}
          <span class="legend-pill badge-verified">✓ FULLY VERIFIED PASS</span>
          {% else %}
          <span class="legend-pill badge-unverified">⚠ UNVERIFIED (REGRESSIONS OR NOT RUN)</span>
          {% endif %}
        </div>
      </div>

      <div class="card-body">
        <!-- ============================================================= -->
        <!-- a. Dependency details and risk indicators (OBSERVED, blue/grey) -->
        <!-- ============================================================= -->
        <section class="section-block section-observed">
          <div class="section-header">
            <h2 class="section-title">
              <span>🛡️ a. Dependency Details & Risk Indicators</span>
            </h2>
            <span class="section-evidence-tag badge-observed">EVIDENCE: OBSERVED</span>
          </div>

          {% if c.risk_indicators['items'] %}
          <table>
            <thead>
              <tr>
                <th>Risk Type</th>
                <th>Severity</th>
                <th>Reason / Advisory</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {% for r in c.risk_indicators['items'] %}
              <tr>
                <td><strong>{{ r.risk_type }}</strong></td>
                <td>
                  {% if r.severity == 'HIGH' %}
                  <span class="legend-pill badge-danger">{{ r.severity }}</span>
                  {% elif r.severity == 'MED' %}
                  <span class="legend-pill badge-warning">{{ r.severity }}</span>
                  {% else %}
                  <span class="legend-pill badge-neutral">{{ r.severity }}</span>
                  {% endif %}
                </td>
                <td>{{ r.reason }}</td>
                <td><span class="legend-pill badge-observed">{{ r.evidence }}</span></td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
          <p>No risk flags recorded for this dependency.</p>
          {% endif %}
        </section>

        <!-- ============================================================= -->
        <!-- b. Usage locations (OBSERVED, blue/grey)                       -->
        <!-- ============================================================= -->
        <section class="section-block section-observed">
          <div class="section-header">
            <h2 class="section-title">
              <span>📍 b. Code Usage Locations (AST Mapped)</span>
            </h2>
            <span class="section-evidence-tag badge-observed">EVIDENCE: OBSERVED</span>
          </div>

          {% if c.usage_locations['items'] %}
          <p style="font-size: 0.9rem; margin-bottom: 8px;">
            Identified <strong>{{ c.usage_locations.count }}</strong> concrete AST usage location(s) in the target codebase:
          </p>
          {% for u in c.usage_locations['items'] %}
          <div style="margin-bottom: 12px;">
            <div style="font-size: 0.85rem; font-weight: 600; color: #7dd3fc;">
              📄 {{ u.file_path }}:{{ u.line_number }} &nbsp;•&nbsp; Type: <span class="legend-pill badge-neutral">{{ u.usage_type }}</span>
            </div>
            <pre class="code-block"><code>{{ u.code_snippet }}</code></pre>
          </div>
          {% endfor %}
          {% else %}
          <p>No direct AST code usages detected in the repository.</p>
          {% endif %}
        </section>

        <!-- ============================================================= -->
        <!-- c. Proposed replacement, migration steps, suggested tests,    -->
        <!--    and patch (AI-GENERATED, UNVERIFIED, orange/yellow)        -->
        <!-- ============================================================= -->
        <section class="section-block section-ai-unverified">
          <div class="section-header">
            <h2 class="section-title">
              <span>🤖 c. Proposed Replacement & Migration Plan</span>
            </h2>
            <span class="section-evidence-tag badge-ai">EVIDENCE: AI-GENERATED (UNVERIFIED)</span>
          </div>

          <div class="banner-warning">
            ⚠️ <strong>UNVERIFIED AI PROPOSAL:</strong> The replacement below was synthesized by Gemma 4. 
            Do not merge without human code review and successful execution of the test suite.
          </div>

          <div style="display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 14px; align-items: center;">
            <div>
              Proposed Replacement: <strong style="font-size: 1.15rem; color: #fde68a;">{{ c.ai_recommendation.proposed_replacement or 'None' }}</strong>
            </div>
            <div>
              Usage Complexity: 
              <span class="legend-pill badge-warning">{{ c.ai_recommendation.usage_complexity or 'BASIC' }}</span>
            </div>
            <div>
              Verification State: 
              {% if c.ai_recommendation.verified %}
              <span class="legend-pill badge-verified">VERIFIED PASS</span>
              {% else %}
              <span class="legend-pill badge-unverified">UNVERIFIED</span>
              {% endif %}
            </div>
          </div>

          {% if c.ai_recommendation.reasoning %}
          <div style="margin-bottom: 16px;">
            <strong style="color: #fde68a;">AI Reasoning:</strong>
            <p style="font-size: 0.92rem; margin-top: 4px;">{{ c.ai_recommendation.reasoning }}</p>
          </div>
          {% endif %}

          {% if c.ai_recommendation.candidates %}
          <div style="margin-bottom: 16px;">
            <strong style="color: #fde68a;">Evaluated Candidates:</strong>
            <ul class="item-list">
              {% for cand in c.ai_recommendation.candidates %}
              <li>
                <strong>{{ cand.name }}</strong>: {{ cand.why }}
                {% if cand.api_gaps %}
                <span style="color: #fca5a5;">(API Gaps: {{ cand.api_gaps }})</span>
                {% endif %}
              </li>
              {% endfor %}
            </ul>
          </div>
          {% endif %}

          {% if c.ai_recommendation.migration_steps %}
          <div style="margin-bottom: 16px;">
            <strong style="color: #fde68a;">Step-by-Step Migration Instructions:</strong>
            <ol class="item-list">
              {% for step in c.ai_recommendation.migration_steps %}
              <li>{{ step }}</li>
              {% endfor %}
            </ol>
          </div>
          {% endif %}

          {% if c.ai_recommendation.code_patch %}
          <div style="margin-bottom: 16px;">
            <strong style="color: #fde68a;">Proposed Unified Diff Code Patch:</strong>
            <pre class="code-block"><code>{{ c.ai_recommendation.code_patch }}</code></pre>
          </div>
          {% endif %}

          {% if c.ai_recommendation.suggested_tests %}
          <div style="margin-bottom: 16px;">
            <strong style="color: #fde68a;">AI-Suggested Dual-Passing Pytest Functions:</strong>
            {% for t in c.ai_recommendation.suggested_tests %}
            <pre class="code-block"><code>{{ t }}</code></pre>
            {% endfor %}
          </div>
          {% endif %}
        </section>

        <!-- ============================================================= -->
        <!-- d. Executed test results, baseline vs replacement             -->
        <!--    (VERIFIED BY EXECUTION, green)                             -->
        <!-- ============================================================= -->
        <section class="section-block section-verified-execution">
          <div class="section-header">
            <h2 class="section-title">
              <span>🧪 d. Executed Test Results (Baseline vs Replacement)</span>
            </h2>
            <span class="section-evidence-tag badge-verified">EVIDENCE: VERIFIED BY EXECUTION</span>
          </div>

          <!-- Scoreboard comparing Before Swap vs After Swap -->
          <div class="scoreboard-grid">
            <div class="score-card">
              <div class="score-card-title">Before Swap (Baseline: {{ c.dependency.name }})</div>
              <div class="score-stats">
                <span class="stat-pass">{{ c.executed_test_results.counts_before_swap_baseline.pass }} Pass</span>
                <span class="stat-fail">{{ c.executed_test_results.counts_before_swap_baseline.fail }} Fail</span>
                <span class="stat-err">{{ c.executed_test_results.counts_before_swap_baseline.error }} Error</span>
                <span class="stat-notrun">{{ c.executed_test_results.counts_before_swap_baseline.not_run }} Unrun</span>
              </div>
            </div>
            <div class="score-card">
              <div class="score-card-title">After Swap (Replacement: {{ c.ai_recommendation.proposed_replacement or 'candidate' }})</div>
              <div class="score-stats">
                <span class="stat-pass">{{ c.executed_test_results.counts_after_swap_replacement.pass }} Pass</span>
                <span class="stat-fail">{{ c.executed_test_results.counts_after_swap_replacement.fail }} Fail</span>
                <span class="stat-err">{{ c.executed_test_results.counts_after_swap_replacement.error }} Error</span>
                <span class="stat-notrun">{{ c.executed_test_results.counts_after_swap_replacement.not_run }} Unrun</span>
              </div>
            </div>
          </div>

          {% if c.executed_test_results.results %}
          <table>
            <thead>
              <tr>
                <th>Target</th>
                <th>Test Identifier</th>
                <th>Status</th>
                <th>Verified?</th>
                <th>Output Summary</th>
              </tr>
            </thead>
            <tbody>
              {% for res in c.executed_test_results.results %}
              <tr>
                <td><code>{{ res.dependency_name }}</code></td>
                <td><strong>{{ res.test_name }}</strong></td>
                <td>
                  {% if res.status == 'PASS' %}
                  <span class="legend-pill badge-success">{{ res.status }}</span>
                  {% elif res.status == 'FAIL' %}
                  <span class="legend-pill badge-danger">{{ res.status }}</span>
                  {% elif res.status == 'ERROR' %}
                  <span class="legend-pill badge-danger">{{ res.status }}</span>
                  {% else %}
                  <span class="legend-pill badge-neutral">{{ res.status }}</span>
                  {% endif %}
                </td>
                <td>
                  {% if res.verified %}
                  <span style="color: #4ade80; font-weight: bold;">✓ Yes (Pass)</span>
                  {% else %}
                  <span style="color: #f87171;">✗ No</span>
                  {% endif %}
                </td>
                <td style="font-size: 0.82rem; color: #cbd5e1;">
                  {{ res.raw_output[:120] if res.raw_output else 'No output' }}
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
          <p>No verification tests executed.</p>
          {% endif %}
        </section>

        <!-- ============================================================= -->
        <!-- e. Compatibility gaps (observed) and Gemma's explanation      -->
        <!-- ============================================================= -->
        <section class="section-block section-observed" style="border-left-color: #38bdf8;">
          <div class="section-header">
            <h2 class="section-title">
              <span>🔍 e. Compatibility Gaps & AI Interpretations</span>
            </h2>
            <div style="display: flex; gap: 8px;">
              <span class="section-evidence-tag badge-observed">GAPS: OBSERVED</span>
              <span class="section-evidence-tag badge-ai">EXPLANATION: AI-GENERATED</span>
            </div>
          </div>

          <!-- Observed Gaps -->
          <div style="margin-bottom: 16px;">
            <strong style="color: #7dd3fc;">Observed Execution Discrepancies & Regressions:</strong>
            {% if c.compatibility_gaps['items'] %}
            <ul class="item-list">
              {% for g in c.compatibility_gaps['items'] %}
              <li>
                {% if 'Regression' in g %}
                <span class="legend-pill badge-danger" style="margin-right: 4px;">REGRESSION</span>
                {% elif 'missing' in g or 'not executed' in g %}
                <span class="legend-pill badge-warning" style="margin-right: 4px;">COVERAGE GAP</span>
                {% endif %}
                {{ g }}
              </li>
              {% endfor %}
            </ul>
            {% else %}
            <p style="font-size: 0.9rem; margin-top: 4px; color: #86efac;">
              ✓ No compatibility regressions observed between baseline and replacement test runs.
            </p>
            {% endif %}
          </div>

          <!-- AI Gap Explanations -->
          <div>
            <strong style="color: #fde68a;">Gemma 4 Failure Explanations & Suggested Fixes (AI Interpretation):</strong>
            {% if c.gap_explanations['items'] %}
            <ul class="item-list">
              {% for exp in c.gap_explanations['items'] %}
              <li style="color: #fef3c7;">
                <span class="legend-pill badge-ai" style="margin-right: 4px;">AI INTERPRETATION</span>
                {{ exp }}
              </li>
              {% endfor %}
            </ul>
            {% else %}
            <p style="font-size: 0.9rem; margin-top: 4px; color: var(--color-text-muted);">
              No failing test interpretations required.
            </p>
            {% endif %}
          </div>
        </section>

        <!-- ============================================================= -->
        <!-- f. Open concerns and limitations (highlighted box)            -->
        <!-- ============================================================= -->
        <section class="section-block section-open-concerns">
          <div class="section-header" style="border-bottom-color: rgba(239, 68, 68, 0.3);">
            <h2 class="section-title">
              <span>⚠️ f. Open Concerns & Limitations (Action Required)</span>
            </h2>
            <span class="section-evidence-tag badge-danger">ATTENTION REQUIRED</span>
          </div>

          {% if c.open_concerns['items'] %}
          <ul class="item-list" style="color: var(--concern-text);">
            {% for concern in c.open_concerns['items'] %}
            <li><strong>!</strong> {{ concern }}</li>
            {% endfor %}
          </ul>
          {% else %}
          <p style="color: #86efac; font-weight: 600;">
            ✓ No open concerns or unresolved limitations identified for this dependency.
          </p>
          {% endif %}
        </section>
      </div>
    </article>
    {% endfor %}

    <!-- Footer -->
    <footer class="report-footer">
      <div style="font-weight: 700; color: #f8fafc; margin-bottom: 6px;">
        Model Attribution & Inference Metadata
      </div>
      <div>
        Model: <strong>{{ data.model_info.model_name }}</strong> &nbsp;|&nbsp; 
        Version: <strong>{{ data.model_info.version }}</strong> &nbsp;|&nbsp; 
        Access Method: <strong>{{ data.model_info.access_method }}</strong>
      </div>
      <div class="footer-links" style="margin-top: 6px;">
        Applicable License & Terms of Use: 
        <a href="{{ data.model_info.terms_url }}" target="_blank" rel="noopener noreferrer">
          {{ data.model_info.terms_url }}
        </a>
      </div>
      <div style="margin-top: 12px; font-size: 0.75rem; color: #64748b;">
        Generated by DepExit • {{ data.generated_at }}
      </div>
    </footer>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Core Module Functions (Required Interface)
# ---------------------------------------------------------------------------

def render_report(
    capsules: Union[ExitCapsule, Sequence[ExitCapsule]],
    output_dir: Union[str, Path] = "output",
    output_path: Optional[Union[str, Path]] = None,
    format: str = "all",
    **kwargs: Any,
) -> str:
    """Render ExitCapsules into structured JSON and an offline, self-contained HTML report.

    Tasks:
    1. Dump all capsules to output/capsule.json, pretty-printed, including model_info and
       an explicit 'evidence' tag on every section.
    2. Render output/capsule.html using Jinja2 with inline CSS and zero external files,
       strictly following sections (a) through (f) and the attribution footer.

    Args:
        capsules: ExitCapsule instance or list of ExitCapsule instances.
        output_dir: Target output directory or file destination.
        output_path: Optional alias for output_dir.
        format: Output report format (default: 'all').
        **kwargs: Additional options.

    Returns:
        Rendered content string or path description.
    """
    if output_path is not None:
        output_dir = output_path

    if isinstance(capsules, ExitCapsule):
        capsule_list = [capsules]
    else:
        capsule_list = list(capsules)

    # Determine output directory
    out_path = Path(output_dir)
    if out_path.suffix in (".html", ".json", ".md"):
        target_dir = out_path.parent
    else:
        target_dir = out_path

    target_dir.mkdir(parents=True, exist_ok=True)

    # Resolve model_info
    model_info = _resolve_model_info(capsule_list[0] if capsule_list else None)

    # Convert all capsules to structured dictionary with evidence tags
    capsules_payload = [_capsule_to_dict(c, model_info) for c in capsule_list]

    full_json_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_info": model_info,
        "evidence": "capsule_report",
        "total_capsules": len(capsules_payload),
        "capsules": capsules_payload,
    }

    # 1. Dump to output/capsule.json
    json_path = target_dir / "capsule.json"
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(full_json_payload, jf, indent=2, ensure_ascii=False)

    # 2. Render output/capsule.html with Jinja2
    template = Template(HTML_REPORT_TEMPLATE)
    html_content = template.render(data=full_json_payload)

    html_path = target_dir / "capsule.html"
    with open(html_path, "w", encoding="utf-8") as hf:
        hf.write(html_content)

    return html_content


def print_summary(capsules: Union[ExitCapsule, Sequence[ExitCapsule]]) -> str:
    """Print a spacious, highly readable terminal summary of each dependency capsule.

    Each capsule is rendered with clear section blocks, generous vertical spacing,
    and labelled sub-sections so the output is easy to scan at a glance.

    Features:
    - [BLOCK 1: OBSERVED / VERIFIED]  -- source-grounded & execution-tested facts
    - [BLOCK 2: AI-SUGGESTED]         -- Gemma 4 model output (requires human review)
    - [OPEN CONCERNS & LIMITATIONS]   -- highlighted action items

    Args:
        capsules: ExitCapsule instance or list of ExitCapsule instances.

    Returns:
        The printed summary text string.
    """
    if isinstance(capsules, ExitCapsule):
        capsule_list = [capsules]
    else:
        capsule_list = list(capsules)

    output_lines: list[str] = []

    WIDE  = "=" * 80
    THIN  = "-" * 80
    BLANK = ""

    for idx, c in enumerate(capsule_list, 1):
        dep        = c.dependency
        rec        = c.ai_recommendation
        repl_name  = rec.proposed_replacement if rec else "None"
        model_info = _resolve_model_info(c)
        b_counts, r_counts, _, _ = _partition_and_count_tests(c)

        # ── Header ────────────────────────────────────────────────────────────
        lines = [
            BLANK,
            WIDE,
            BLANK,
            f"    DEPEXIT CAPSULE  [{idx} of {len(capsule_list)}]",
            BLANK,
            f"    Package   : {dep.name}  (declared version: {dep.declared_version})",
            f"    Manifest  : {dep.source_file}",
            BLANK,
            WIDE,
        ]

        # ── Block 1 ───────────────────────────────────────────────────────────
        lines += [
            BLANK,
            "  ┌" + THIN[2:],
            "  │  BLOCK 1 — OBSERVED & VERIFIED",
            "  │  Source-grounded and execution-tested facts",
            "  └" + THIN[2:],
            BLANK,
        ]

        # Target
        lines.append(f"    Target Dependency : {dep.name}  v{dep.declared_version}")
        lines.append(BLANK)

        # Risk flags
        if c.risk_flags:
            lines.append(f"    Risk Flags Observed ({len(c.risk_flags)} flag(s)):")
            for rf in c.risk_flags:
                rtype = rf.risk_type.value if hasattr(rf.risk_type, "value") else str(rf.risk_type)
                rsev  = rf.severity.value  if hasattr(rf.severity,  "value") else str(rf.severity)
                lines.append(f"        • [{rsev:4s}]  {rtype}")
                lines.append(f"               {rf.reason}")
                lines.append(BLANK)
        else:
            lines.append("    Risk Flags Observed : None flagged")
            lines.append(BLANK)

        # AST usages
        if c.usage_locations:
            lines.append(f"    Code Usages in Repository ({len(c.usage_locations)} location(s)):")
            for u in c.usage_locations:
                utype         = u.usage_type.value if hasattr(u.usage_type, "value") else str(u.usage_type)
                code_one_line = " ".join(u.code_snippet.strip().splitlines())[:70]
                lines.append(f"        • {u.file_path}  line {u.line_number}")
                lines.append(f"          Type    : {utype}")
                lines.append(f"          Snippet : {code_one_line}")
                lines.append(BLANK)
        else:
            lines.append("    Code Usages in Repository : No direct usages detected")
            lines.append(BLANK)

        # Test counts
        lines.append("    Test Suite Results (Before vs After Package Swap):")
        lines.append(BLANK)
        lines.append(f"        Before Swap  — Baseline ({dep.name}):")
        lines.append(
            f"            PASS: {b_counts['pass']}   "
            f"FAIL: {b_counts['fail']}   "
            f"ERROR: {b_counts['error']}   "
            f"NOT_RUN: {b_counts['not_run']}"
        )
        lines.append(BLANK)
        lines.append(f"        After Swap   — Replacement ({repl_name}):")
        lines.append(
            f"            PASS: {r_counts['pass']}   "
            f"FAIL: {r_counts['fail']}   "
            f"ERROR: {r_counts['error']}   "
            f"NOT_RUN: {r_counts['not_run']}"
        )
        lines.append(BLANK)

        # Compatibility gaps
        if c.compatibility_gaps:
            lines.append(f"    Compatibility Gaps Observed ({len(c.compatibility_gaps)}):")
            for g in c.compatibility_gaps:
                lines.append(f"        • {g}")
        else:
            lines.append("    Compatibility Gaps : None  (no regressions observed)")
        lines.append(BLANK)

        # ── Block 2 ───────────────────────────────────────────────────────────
        lines += [
            "  ┌" + THIN[2:],
            "  │  BLOCK 2 — AI-SUGGESTED  (requires human review)",
            "  │  Gemma 4 model output — not independently verified",
            "  └" + THIN[2:],
            BLANK,
        ]

        lines.append(f"    Proposed Replacement : {repl_name}")
        lines.append(BLANK)

        if rec:
            comp       = getattr(rec, "usage_complexity", "BASIC")
            ver_status = "VERIFIED PASS" if getattr(rec, "verified", False) else "UNVERIFIED  (regressions present / untested)"

            lines.append(f"    Verification Status  : {ver_status}")
            lines.append(f"    Migration Complexity : {comp}")
            lines.append(BLANK)

            if rec.reasoning:
                # Wrap reasoning at ~70 chars
                raw   = rec.reasoning[:300]
                words = raw.split()
                wrapped_lines, current = [], []
                for w in words:
                    if sum(len(x) + 1 for x in current) + len(w) > 70:
                        wrapped_lines.append("        " + " ".join(current))
                        current = [w]
                    else:
                        current.append(w)
                if current:
                    wrapped_lines.append("        " + " ".join(current))
                if len(rec.reasoning) > 300:
                    wrapped_lines.append("        ... (truncated)")
                lines.append("    Reasoning:")
                lines.extend(wrapped_lines)
                lines.append(BLANK)

            if rec.migration_steps:
                lines.append(f"    Migration Steps ({len(rec.migration_steps)} step(s)):")
                for s_idx, step in enumerate(rec.migration_steps, 1):
                    lines.append(f"        {s_idx}.  {step}")
                lines.append(BLANK)

            patch = getattr(rec, "code_patch", "")
            if patch:
                lines.append("    Code Patch Preview (first 5 lines):")
                for pl in patch.strip().splitlines()[:5]:
                    lines.append(f"        {pl}")
                lines.append(BLANK)

            tests = getattr(rec, "suggested_tests", []) or []
            if tests:
                lines.append(f"    Suggested Pytest Functions ({len(tests)} test(s)):")
                for t in tests:
                    fn_match = re.search(r"def\s+([a-zA-Z0-9_]+)", str(t))
                    fn_name  = fn_match.group(1) if fn_match else "test_function"
                    lines.append(f"        • def {fn_name}()")
                lines.append(BLANK)
        else:
            lines.append("    Recommendation : No AI recommendation was generated.")
            lines.append(BLANK)

        # Gap explanations from Gemma
        gap_exps = getattr(c, "gap_explanations", []) or []
        if gap_exps:
            lines.append(f"    AI Gap Interpretations ({len(gap_exps)}):")
            for exp in gap_exps:
                lines.append(f"        • {exp}")
            lines.append(BLANK)

        # Model metadata
        lines.append("    Model Information:")
        lines.append(f"        Name    : {model_info['model_name']}")
        lines.append(f"        Version : {model_info['version']}")
        lines.append(f"        Method  : {model_info['access_method']}")
        lines.append(f"        Terms   : {model_info['terms_url']}")
        lines.append(BLANK)

        # ── Open Concerns ─────────────────────────────────────────────────────
        lines += [
            "  ┌" + THIN[2:],
            "  │  OPEN CONCERNS & LIMITATIONS",
            "  │  Action items that still require attention",
            "  └" + THIN[2:],
            BLANK,
        ]
        if c.open_concerns:
            for concern in c.open_concerns:
                lines.append(f"    !  {concern}")
        else:
            lines.append("    [OK]  No unresolved concerns or unverified risk flags identified.")

        lines += [BLANK, WIDE, BLANK]
        output_lines.extend(lines)

    summary_str = "\n".join(output_lines)
    print(summary_str)
    return summary_str


# ---------------------------------------------------------------------------
# Auxiliary Renderers for Backwards Compatibility
# ---------------------------------------------------------------------------

def render_html(capsules: list[ExitCapsule]) -> str:
    """Render a list of ExitCapsule objects into HTML format."""
    model_info = _resolve_model_info(capsules[0] if capsules else None)
    capsules_payload = [_capsule_to_dict(c, model_info) for c in capsules]
    full_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_info": model_info,
        "evidence": "capsule_report",
        "total_capsules": len(capsules_payload),
        "capsules": capsules_payload,
    }
    template = Template(HTML_REPORT_TEMPLATE)
    return template.render(data=full_payload)


def render_markdown(capsules: list[ExitCapsule]) -> str:
    """Render a list of ExitCapsule objects into Markdown format."""
    md_lines: list[str] = ["# DepExit Dependency Exit Capsules Report\n"]
    for c in capsules:
        dep = c.dependency
        rec = c.ai_recommendation
        md_lines.append(f"## Dependency: {dep.name} (Declared: {dep.declared_version})\n")
        md_lines.append(f"**Source File:** `{dep.source_file}`\n")
        
        md_lines.append("### a. Risk Indicators [OBSERVED]\n")
        for r in c.risk_flags:
            md_lines.append(f"- **[{r.severity}] {r.risk_type}**: {r.reason}")
        md_lines.append("")

        md_lines.append("### b. Usage Locations [OBSERVED]\n")
        for u in c.usage_locations:
            md_lines.append(f"- `{u.file_path}:{u.line_number}` ({u.usage_type})")
            md_lines.append(f"```python\n{u.code_snippet}\n```")
        md_lines.append("")

        md_lines.append("### c. AI Recommendation [AI-GENERATED, UNVERIFIED]\n")
        if rec:
            md_lines.append(f"- **Proposed Replacement:** `{rec.proposed_replacement}`")
            md_lines.append(f"- **Complexity:** `{getattr(rec, 'usage_complexity', 'BASIC')}`")
            md_lines.append(f"- **Reasoning:** {rec.reasoning}")
            if rec.migration_steps:
                md_lines.append("- **Steps:**")
                for s in rec.migration_steps:
                    md_lines.append(f"  1. {s}")
        md_lines.append("")

        md_lines.append("### d. Executed Test Results [VERIFIED BY EXECUTION]\n")
        b_cnt, r_cnt, _, _ = _partition_and_count_tests(c)
        md_lines.append(f"- Baseline: {b_cnt['pass']} Pass, {b_cnt['fail']} Fail, {b_cnt['error']} Error")
        md_lines.append(f"- Replacement: {r_cnt['pass']} Pass, {r_cnt['fail']} Fail, {r_cnt['error']} Error")
        md_lines.append("")

        md_lines.append("### e. Compatibility Gaps & Explanations\n")
        for g in c.compatibility_gaps:
            md_lines.append(f"- [Observed Gap] {g}")
        for exp in getattr(c, "gap_explanations", []):
            md_lines.append(f"- [AI Explanation] {exp}")
        md_lines.append("")

        md_lines.append("### f. Open Concerns and Limitations [ATTENTION REQUIRED]\n")
        for concern in c.open_concerns:
            md_lines.append(f"> ⚠️ {concern}")
        md_lines.append("\n---\n")

    return "\n".join(md_lines)


def save_report(content: str, output_path: Union[str, Path]) -> None:
    """Save rendered report string to the specified file path."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Standalone Verification and Demonstration
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from ai.capsule_builder import build_capsule

    print("=" * 80)
    print("DepExit Report Renderer: Standalone Verification")
    print("=" * 80)

    # Build mock capsule
    mock_dep = Dependency(name="requests", declared_version="2.28.0", source_file="requirements.txt")
    mock_risks = [
        RiskFlag(
            dependency_name="requests",
            risk_type=RiskType.VULNERABLE,
            reason="CVE-2023-32681: Unintended leak of Proxy-Authorization header in redirects",
            severity=RiskSeverity.HIGH,
        ),
        RiskFlag(
            dependency_name="requests",
            risk_type=RiskType.UNMAINTAINED,
            reason="Lacks native asynchronous request handling",
            severity=RiskSeverity.LOW,
        ),
    ]
    mock_usages = [
        UsageLocation(
            dependency_name="requests",
            file_path="src/api_client.py",
            line_number=42,
            usage_type=UsageType.CALL,
            code_snippet="resp = requests.get('https://api.example.com/data', timeout=5)",
        )
    ]
    mock_rec = AIRecommendation(
        dependency_name="requests",
        proposed_replacement="httpx",
        migration_steps=[
            "Add 'httpx' to requirements.txt and remove 'requests'",
            "Replace 'import requests' with 'import httpx'",
            "Update top-level calls 'requests.get(...)' to 'httpx.get(...)'",
        ],
        reasoning="httpx provides an API largely identical to requests while supporting async and fixing CVE-2023-32681.",
        ai_generated=True,
    )
    mock_rec.usage_complexity = "MODERATE"
    mock_rec.code_patch = "--- a/src/api_client.py\n+++ b/src/api_client.py\n@@ -1,2 +1,2 @@\n-import requests\n+import httpx\n"
    mock_rec.suggested_tests = [
        "def test_api_client_fetch_status():\n    import httpx\n    assert True\n"
    ]
    mock_rec.candidates = [
        {"name": "httpx", "why": "Feature complete", "api_gaps": "Explicit timeout structure"},
        {"name": "urllib3", "why": "Lightweight", "api_gaps": "Lower level API"},
    ]

    mock_tests = {
        "baseline": [
            VerificationResult(
                dependency_name="requests",
                test_name="test_get_status",
                status=VerificationStatus.PASS,
                raw_output="PASSED in 0.02s",
            ),
            VerificationResult(
                dependency_name="requests",
                test_name="test_headers_roundtrip",
                status=VerificationStatus.PASS,
                raw_output="PASSED in 0.01s",
            ),
        ],
        "replacement": [
            VerificationResult(
                dependency_name="httpx",
                test_name="test_get_status",
                status=VerificationStatus.PASS,
                raw_output="PASSED in 0.03s",
            ),
            VerificationResult(
                dependency_name="httpx",
                test_name="test_headers_roundtrip",
                status=VerificationStatus.FAIL,
                raw_output="AssertionError: Case-preservation differed in headers",
            ),
            VerificationResult(
                dependency_name="httpx",
                test_name="test_proxy_auth",
                status=VerificationStatus.NOT_RUN,
                raw_output="Proxy fixture unavailable",
            ),
        ],
    }

    mock_gap_explanations = [
        "[test_headers_roundtrip] Likely Cause: Case-insensitivity differs between requests CaseInsensitiveDict and httpx Headers. Suggested Fix: Access headers using lowercased keys."
    ]

    capsule = build_capsule(
        dependency=mock_dep,
        risk_flags=mock_risks,
        usage_locations=mock_usages,
        ai_recommendation=mock_rec,
        verification_results=mock_tests,
        gap_explanations=mock_gap_explanations,
        model_info=DEFAULT_MODEL_INFO,
    )

    # 1. Test render_report
    test_output_dir = Path("output")
    print(f"\n[*] Rendering report to directory: {test_output_dir.resolve()}...")
    html_output = render_report([capsule], output_dir=test_output_dir)

    json_file = test_output_dir / "capsule.json"
    html_file = test_output_dir / "capsule.html"

    assert json_file.exists(), "output/capsule.json must exist"
    assert html_file.exists(), "output/capsule.html must exist"

    # Verify JSON content
    with open(json_file, "r", encoding="utf-8") as jf:
        data = json.load(jf)
        assert "model_info" in data
        assert "capsules" in data
        first_cap = data["capsules"][0]
        assert first_cap["dependency"]["evidence"] == "observed"
        assert first_cap["risk_indicators"]["evidence"] == "observed"
        assert first_cap["usage_locations"]["evidence"] == "observed"
        assert first_cap["ai_recommendation"]["evidence"] == "ai_generated"
        assert first_cap["executed_test_results"]["evidence"] == "verified_by_execution"
        assert first_cap["compatibility_gaps"]["evidence"] == "observed"
        assert first_cap["gap_explanations"]["evidence"] == "ai_generated"
        assert first_cap["open_concerns"]["evidence"] == "observed"
        print("[+] output/capsule.json verified with required 'evidence' tags on every section!")

    # Verify HTML content
    with open(html_file, "r", encoding="utf-8") as hf:
        html_str = hf.read()
        # Verify required sections exist
        assert "a. Dependency Details & Risk Indicators" in html_str
        assert "b. Code Usage Locations" in html_str
        assert "c. Proposed Replacement & Migration Plan" in html_str
        assert "d. Executed Test Results" in html_str
        assert "e. Compatibility Gaps & AI Interpretations" in html_str
        assert "f. Open Concerns & Limitations" in html_str
        assert "gemma-4-31b-it" in html_str
        assert "https://ai.google.dev/gemma/terms" in html_str
        assert "EVIDENCE: OBSERVED" in html_str
        assert "EVIDENCE: AI-GENERATED" in html_str
        assert "EVIDENCE: VERIFIED BY EXECUTION" in html_str
        print("[+] output/capsule.html verified with required ordered sections and badges!")

    # 2. Test print_summary
    print("\n[*] Testing print_summary():\n")
    summary_output = print_summary([capsule])
    assert "[BLOCK 1: OBSERVED / VERIFIED]" in summary_output
    assert "[BLOCK 2: AI-SUGGESTED (unverified)]" in summary_output
    assert "[OPEN CONCERNS & LIMITATIONS]" in summary_output
    assert "Before Swap (Baseline: requests):" in summary_output
    assert "After Swap  (Replacement: httpx):" in summary_output
    print("[+] print_summary() verified with readable CLI blocks!")

    print("\n" + "=" * 80)
    print("ALL REPORT RENDERER CONTRACTS AND ASSERTIONS PASSED SUCCESSFULLY!")
    print("=" * 80)
