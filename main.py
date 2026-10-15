"""DepExit CLI entrypoint and pipeline orchestrator.

Analyzes a target Python repository for risky dependencies, maps their AST usages,
queries Gemma 4 for replacement recommendations and migration plans, verifies candidate
packages in isolated environments, and outputs structured Dependency Exit Capsules (JSON + HTML).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

# Configure stdout/stderr for UTF-8 on platforms where default encoding is restricted (e.g., Windows cp1252)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure repository root is on sys.path
_repo_root = str(Path(__file__).resolve().parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Pipeline component imports
from scanner.discover import discover_dependencies
from scanner.risk_assess import assess_risk
from analyzer.usage_mapper import map_usage, resolve_import_roots
from analyzer.verify_test import verify_replacement
from ai.gemma_client import get_recommendation, explain_gaps, MODEL_INFO
from ai.capsule_builder import build_capsule
from report.render import render_report, print_summary

try:
    from schema import (
        Dependency,
        ExitCapsule,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        VerificationResult,
        VerificationStatus,
    )
except ImportError:
    from depexit.schema import (
        Dependency,
        ExitCapsule,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        VerificationResult,
        VerificationStatus,
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("depexit")


def _is_high_or_med(severity: Union[RiskSeverity, str]) -> bool:
    """Return True if severity is HIGH or MED."""
    val = severity.value if hasattr(severity, "value") else str(severity)
    return str(val).strip().upper() in ("HIGH", "MED")


def _max_severity_score(risks: list[RiskFlag]) -> int:
    """Compute highest severity score for ranking: HIGH=2, MED=1, other=0."""
    score = 0
    for r in risks:
        s_val = (r.severity.value if hasattr(r.severity, "value") else str(r.severity)).upper()
        if s_val == "HIGH":
            return 2
        elif s_val == "MED":
            score = max(score, 1)
    return score


def main(repo_path: str) -> list[ExitCapsule]:
    """Execute the end-to-end DepExit analysis, recommendation, verification, and reporting pipeline.

    Args:
        repo_path: Path to target Python repository.

    Returns:
        List of generated ExitCapsule instances.
    """
    load_dotenv()
    target_path = Path(repo_path).resolve()

    print("\n" + "=" * 80)
    print(f"🚀 DepExit Engine Initialized: Target Repository -> '{target_path}'")
    print("=" * 80)

    if not target_path.exists():
        print(f"❌ Error: Repository path '{target_path}' does not exist.", file=sys.stderr)
        return []

    # -------------------------------------------------------------------------
    # Stage 1: Discover dependencies and assess risks
    # -------------------------------------------------------------------------
    print("\n[Stage 1/4] Discovering declared dependencies and scanning PyPI/vulnerability risks...")
    try:
        discovered_deps = discover_dependencies(target_path)
        print(f"       -> Discovered {len(discovered_deps)} declared dependencies in repository manifest(s).")
    except Exception as e:
        logger.error("Dependency discovery failed: %s", e)
        print(f"❌ Error during dependency discovery: {e}", file=sys.stderr)
        return []

    if not discovered_deps:
        print("⚠️ No dependencies discovered in repository manifest files.")
        return []

    try:
        risk_flags = assess_risk(discovered_deps)
        print(f"       -> Assessed repository risks: {len(risk_flags)} total risk flag(s) identified.")
    except Exception as e:
        logger.error("Risk assessment failed: %s", e)
        risk_flags = []

    # Index risk flags by dependency name
    flags_by_dep: dict[str, list[RiskFlag]] = {}
    for flag in risk_flags:
        flags_by_dep.setdefault(flag.dependency_name.lower(), []).append(flag)

    # -------------------------------------------------------------------------
    # Stage 2: Filter to HIGH/MED severity risky dependencies only
    # -------------------------------------------------------------------------
    print("\n[Stage 2/4] Filtering candidate dependencies to HIGH and MED risk severity...")
    risky_deps: list[tuple[Dependency, list[RiskFlag]]] = []
    seen_dep_names: set[str] = set()
    for dep in discovered_deps:
        norm_name = dep.name.lower()
        if norm_name in seen_dep_names:
            continue
        dep_risks = flags_by_dep.get(norm_name, [])
        high_med_risks = [r for r in dep_risks if _is_high_or_med(r.severity)]
        if high_med_risks:
            seen_dep_names.add(norm_name)
            risky_deps.append((dep, high_med_risks))

    if not risky_deps:
        print("✅ No HIGH or MED severity risky dependencies detected in repository.")
        # Output empty reports
        os.makedirs("output", exist_ok=True)
        render_report([], "output")
        print_summary([])
        return []

    # Sort so top highest-severity dependencies come first
    risky_deps.sort(key=lambda item: (-_max_severity_score(item[1]), item[0].name.lower()))
    print(f"       -> Identified {len(risky_deps)} HIGH/MED risky dependencies to analyze:")
    for idx, (rdep, rflags) in enumerate(risky_deps, 1):
        highest_sev = "HIGH" if any((rf.severity.value if hasattr(rf.severity, "value") else str(rf.severity)).upper() == "HIGH" for rf in rflags) else "MED"
        print(f"          {idx}. {rdep.name:<18} [{highest_sev:4}] ({len(rflags)} risk flag(s))")

    # -------------------------------------------------------------------------
    # Stage 3: Per-dependency analysis, recommendation, verification & capsule build
    # -------------------------------------------------------------------------
    print("\n[Stage 3/4] Processing risky dependencies through analysis, AI recommendation, and verification...")
    capsules: list[ExitCapsule] = []
    total_risky = len(risky_deps)

    for dep_idx, (dep, dep_risks) in enumerate(risky_deps, 1):
        dep_name = dep.name
        is_priority_verification = (dep_idx <= 2)
        open_concerns: list[str] = []

        print(f"\n---> [{dep_idx}/{total_risky}] Analyzing dependency: '{dep_name}'")

        # ---------------------------------------------------------------------
        # 3a. map_usage (use import name, e.g. PyYAML -> yaml, falling back to package name)
        # ---------------------------------------------------------------------
        usages: list[UsageLocation] = []
        try:
            import_names = resolve_import_roots(dep_name)
            # Query with import names and original package name
            query_names = list(dict.fromkeys([*import_names, dep_name]))
            print(f"  [1/5] Mapping AST usages in repository (import names: {', '.join(query_names)})...")
            usages = map_usage(target_path, query_names)
            print(f"        -> Located {len(usages)} usage location(s) in Python source files.")
        except Exception as e:
            logger.error("Usage mapping failed for '%s': %s", dep_name, e)
            open_concerns.append(f"Usage mapping failed: {e}")
            usages = []

        # ---------------------------------------------------------------------
        # 3b. get_recommendation via Gemma 4
        # ---------------------------------------------------------------------
        rec = None
        try:
            print(f"  [2/5] Asking Gemma 4 for replacement recommendation and migration plan for '{dep_name}'...")
            rec = get_recommendation(dep_name, dep_risks, usages)
            print(f"        -> Proposed Replacement: '{rec.proposed_replacement}'")
            print(f"        -> Migration Steps: {len(rec.migration_steps)} step(s) generated.")
        except Exception as e:
            logger.error("Gemma recommendation query failed for '%s': %s", dep_name, e)
            open_concerns.append(f"AI recommendation failed: {e}")
            rec = None

        # ---------------------------------------------------------------------
        # 3c. verify_replacement (top 2 highest-severity only; others marked NOT_RUN)
        # ---------------------------------------------------------------------
        v_results: list[VerificationResult] = []
        repl_name = rec.proposed_replacement if rec else ""

        if is_priority_verification:
            if not repl_name or repl_name.strip().lower() in ("none", "n/a", ""):
                print(f"  [3/5] Skipping verification: no valid replacement proposed for '{dep_name}'.")
                v_results = [
                    VerificationResult(
                        dependency_name=dep_name,
                        test_name="verification",
                        status=VerificationStatus.NOT_RUN,
                        raw_output="No replacement candidate proposed to verify.",
                        verified=False,
                    )
                ]
            else:
                try:
                    print(f"  [3/5] Running verification tests for '{dep_name}' -> '{repl_name}' in isolated sandbox (priority #{dep_idx})...")
                    code_patch = getattr(rec, "code_patch", "") if rec else ""
                    suggested_tests = getattr(rec, "suggested_tests", []) if rec else []
                    migration_steps = getattr(rec, "migration_steps", []) if rec else []

                    v_results = verify_replacement(
                        repo_path=str(target_path),
                        dependency_name=dep_name,
                        replacement_name=repl_name,
                        migration_steps=migration_steps,
                        is_priority=True,
                        code_patch=code_patch,
                        suggested_tests=suggested_tests,
                    )
                    pass_count = sum(1 for r in v_results if r.status == VerificationStatus.PASS)
                    print(f"        -> Verification completed: {pass_count}/{len(v_results)} test(s) passed.")
                except Exception as e:
                    logger.error("Verification testing failed for '%s': %s", dep_name, e)
                    open_concerns.append(f"Verification testing failed: {e}")
                    v_results = [
                        VerificationResult(
                            dependency_name=dep_name,
                            test_name="verification",
                            status=VerificationStatus.ERROR,
                            raw_output=f"Verification execution failed: {e}",
                            verified=False,
                        )
                    ]
        else:
            print(f"  [3/5] Verification skipped for '{dep_name}' (reason: skipped: time limit - beyond top 2 priority).")
            v_results = [
                VerificationResult(
                    dependency_name=dep_name,
                    test_name="verification",
                    status=VerificationStatus.NOT_RUN,
                    raw_output="skipped: time limit",
                    verified=False,
                )
            ]

        # ---------------------------------------------------------------------
        # 3d. explain_gaps (only if there were FAIL/ERROR results)
        # ---------------------------------------------------------------------
        gap_explanations: list[str] = []
        has_failures = any(
            r.status in (VerificationStatus.FAIL, VerificationStatus.ERROR, "FAIL", "ERROR")
            for r in v_results
        )

        if has_failures and repl_name:
            try:
                print(f"  [4/5] Interpreting test failures and API compatibility gaps with Gemma 4 for '{dep_name}'...")
                gap_explanations = explain_gaps(dep_name, repl_name, v_results)
                print(f"        -> Gemma 4 generated {len(gap_explanations)} failure gap interpretation(s).")
            except Exception as e:
                logger.error("Failed to explain verification gaps for '%s': %s", dep_name, e)
                open_concerns.append(f"AI failure gap explanation failed: {e}")
                gap_explanations = []
        else:
            print(f"  [4/5] Skipping gap explanation (no verification test failures detected for '{dep_name}').")

        # ---------------------------------------------------------------------
        # 3e. build_capsule
        # ---------------------------------------------------------------------
        try:
            print(f"  [5/5] Assembling Dependency Exit Capsule for '{dep_name}'...")
            capsule = build_capsule(
                dependency=dep,
                risk_flags=dep_risks,
                usage_locations=usages,
                ai_recommendation=rec,
                verification_results=v_results,
                gap_explanations=gap_explanations,
                model_info=MODEL_INFO,
                open_concerns=open_concerns,
            )
            capsules.append(capsule)
            print(f"        -> Exit Capsule successfully constructed.")
        except Exception as e:
            logger.error("Failed to build capsule for '%s': %s", dep_name, e)
            open_concerns.append(f"Capsule builder failed: {e}")
            capsule = ExitCapsule(
                dependency=dep,
                risk_flags=dep_risks,
                usage_locations=usages,
                ai_recommendation=rec,
                verification_results=v_results,
                open_concerns=open_concerns,
            )
            capsules.append(capsule)

    # -------------------------------------------------------------------------
    # Stage 4: Render reports (JSON + HTML) and print terminal summary
    # -------------------------------------------------------------------------
    print("\n[Stage 4/4] Rendering Exit Capsule reports and presenting results summary...")
    try:
        os.makedirs("output", exist_ok=True)
        render_report(capsules, "output")
        print("       -> Successfully generated output/capsule.json and output/capsule.html.")
    except Exception as e:
        logger.error("Failed to render output reports: %s", e)
        print(f"❌ Error rendering report files: {e}", file=sys.stderr)

    try:
        print_summary(capsules)
    except Exception as e:
        logger.error("Failed to print terminal summary: %s", e)

    return capsules


# Backward compatibility alias
run_depexit = main


def cli() -> None:
    """Parse CLI arguments and run the DepExit pipeline."""
    parser = argparse.ArgumentParser(
        prog="depexit",
        description="DepExit: Analyze risky dependencies and generate Dependency Exit Capsules.",
    )
    parser.add_argument(
        "repo_path",
        type=str,
        help="Path to the target Python repository to analyze.",
    )
    args = parser.parse_args()
    main(args.repo_path)


if __name__ == "__main__":
    cli()
