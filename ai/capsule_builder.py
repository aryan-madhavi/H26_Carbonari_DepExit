"""Capsule builder module for assembling Dependency Exit Capsules.

Consolidates scanner findings, code usage analysis, AI migration advice,
and test verification results into a structured ExitCapsule dataclass.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence, Union

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

logger = logging.getLogger(__name__)

__all__ = [
    "build_capsule",
    "build_exit_capsule",
    "identify_compatibility_gaps",
    "identify_open_concerns",
]


# ---------------------------------------------------------------------------
# Test Partitioning & Normalization Helpers
# ---------------------------------------------------------------------------

def _normalize_test_name(test_name: str) -> str:
    """Strip run prefixes (e.g. [baseline], [replacement], baseline::) from test name."""
    cleaned = re.sub(
        r"^(?:\[(?:baseline|replacement|original|candidate|target)\]|(?:baseline|replacement|original|candidate|target)::)\s*",
        "",
        test_name,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _is_pass(status: Union[VerificationStatus, str]) -> bool:
    """Return True if status represents an executed passing test."""
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.PASS
    return str(status).strip().upper() == "PASS"


def _is_fail_or_error(status: Union[VerificationStatus, str]) -> bool:
    """Return True if status represents a failing or error test."""
    if isinstance(status, VerificationStatus):
        return status in (VerificationStatus.FAIL, VerificationStatus.ERROR)
    return str(status).strip().upper() in ("FAIL", "ERROR")


def _is_not_run(status: Union[VerificationStatus, str]) -> bool:
    """Return True if status represents an unexecuted test."""
    if isinstance(status, VerificationStatus):
        return status == VerificationStatus.NOT_RUN
    return str(status).strip().upper() in ("NOT_RUN", "NOTRUN", "UNRUN", "SKIPPED")


def _status_str(status: Union[VerificationStatus, str]) -> str:
    """Format status enum or string for display."""
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


def _partition_verification_results(
    verification_results: Optional[Union[list[VerificationResult], dict[str, list[VerificationResult]]]],
    dep_name: str,
    repl_name: Optional[str] = None,
) -> tuple[dict[str, VerificationResult], dict[str, VerificationResult], list[VerificationResult]]:
    """Partition verification results into baseline_map, replacement_map, and flat all_results list.

    Returns:
        (baseline_map, replacement_map, all_results)
    """
    if not verification_results:
        return {}, {}, []

    baseline_map: dict[str, VerificationResult] = {}
    replacement_map: dict[str, VerificationResult] = {}
    all_results: list[VerificationResult] = []

    # Handle dictionary representation: {"baseline": [...], "replacement": [...]}
    if isinstance(verification_results, dict):
        baseline_list = (
            verification_results.get("baseline")
            or verification_results.get("original")
            or verification_results.get("base")
            or []
        )
        replacement_list = (
            verification_results.get("replacement")
            or verification_results.get("candidate")
            or verification_results.get("proposed")
            or verification_results.get("target")
            or []
        )

        for res in baseline_list:
            all_results.append(res)
            baseline_map[_normalize_test_name(res.test_name)] = res

        for res in replacement_list:
            all_results.append(res)
            replacement_map[_normalize_test_name(res.test_name)] = res

        # Catch-all for any other keys in dict
        for k, v in verification_results.items():
            if k not in ("baseline", "original", "base", "replacement", "candidate", "proposed", "target"):
                if isinstance(v, (list, tuple)):
                    all_results.extend(v)

        return baseline_map, replacement_map, all_results

    # Handle flat list / sequence representation
    dep_lower = dep_name.lower().strip()
    repl_lower = repl_name.lower().strip() if repl_name else ""

    for res in verification_results:
        all_results.append(res)
        norm_name = _normalize_test_name(res.test_name)
        res_dep = str(res.dependency_name).lower().strip()

        # Check explicit attributes if present
        is_base_attr = getattr(res, "is_baseline", None)
        run_type_attr = str(getattr(res, "run_type", "")).lower()

        if is_base_attr is True or run_type_attr in ("baseline", "original", "base"):
            baseline_map[norm_name] = res
            continue
        elif is_base_attr is False or run_type_attr in ("replacement", "candidate", "target", "new"):
            replacement_map[norm_name] = res
            continue

        # Check test_name prefixes
        if re.search(r"^(\[baseline\]|baseline::)", res.test_name, re.IGNORECASE):
            baseline_map[norm_name] = res
            continue
        elif re.search(r"^(\[replacement\]|replacement::)", res.test_name, re.IGNORECASE):
            replacement_map[norm_name] = res
            continue

        # Check dependency_name matching
        if repl_lower and (res_dep == repl_lower or "replacement" in res_dep):
            replacement_map[norm_name] = res
        elif res_dep == dep_lower or "baseline" in res_dep or "original" in res_dep:
            baseline_map[norm_name] = res
        else:
            # Fallback heuristic: if replacement name is defined and differs, treat as replacement; else baseline
            if repl_lower and norm_name not in replacement_map:
                replacement_map[norm_name] = res
            else:
                baseline_map[norm_name] = res

    return baseline_map, replacement_map, all_results


# ---------------------------------------------------------------------------
# Compatibility Gaps & Open Concerns Computation
# ---------------------------------------------------------------------------

def identify_compatibility_gaps(
    dependency: Dependency,
    ai_recommendation: Optional[AIRecommendation] = None,
    verification_results: Optional[Union[list[VerificationResult], dict[str, list[VerificationResult]]]] = None,
    usage_locations: Optional[list[UsageLocation]] = None,
) -> list[str]:
    """Compute observed compatibility gaps by comparing BASELINE vs REPLACEMENT test results.

    Identifies:
    1. Regressions: Tests that passed in baseline but failed or errored in replacement.
    2. Tests executed in baseline only (missing from replacement run).
    3. Tests executed in replacement only (missing from baseline run).

    These are observed facts directly from test execution.

    Args:
        dependency: Original risky dependency.
        ai_recommendation: Proposed replacement details.
        verification_results: Test execution outcomes.
        usage_locations: Optional detected usage locations.

    Returns:
        List of observed compatibility gap description strings.
    """
    dep_name = dependency.name
    repl_name = ai_recommendation.proposed_replacement if ai_recommendation else "replacement"

    baseline_map, replacement_map, _ = _partition_verification_results(
        verification_results, dep_name, repl_name
    )

    gaps: list[str] = []

    # 1. Compare tests present in BOTH runs (regression check)
    common_tests = sorted(set(baseline_map.keys()) & set(replacement_map.keys()))
    for test_name in common_tests:
        b_res = baseline_map[test_name]
        r_res = replacement_map[test_name]

        if _is_pass(b_res.status) and _is_fail_or_error(r_res.status):
            # Observed Regression
            fail_snippet = ""
            if r_res.raw_output and r_res.raw_output.strip():
                # Extract first meaningful line of failure output
                first_line = r_res.raw_output.strip().splitlines()[0][:100]
                fail_snippet = f" - Output: {first_line}"
            gaps.append(
                f"Regression in '{test_name}': PASS in baseline ({dep_name}), "
                f"but {_status_str(r_res.status)} in replacement ({repl_name}){fail_snippet}."
            )

    # 2. Tests present in baseline only (not executed in replacement)
    baseline_only = sorted(set(baseline_map.keys()) - set(replacement_map.keys()))
    for test_name in baseline_only:
        b_res = baseline_map[test_name]
        gaps.append(
            f"Test '{test_name}' was executed in baseline ({dep_name}) [{_status_str(b_res.status)}], "
            f"but was not executed in replacement ({repl_name}) run."
        )

    # 3. Tests present in replacement only (not present in baseline)
    replacement_only = sorted(set(replacement_map.keys()) - set(baseline_map.keys()))
    for test_name in replacement_only:
        r_res = replacement_map[test_name]
        gaps.append(
            f"Test '{test_name}' was executed in replacement ({repl_name}) [{_status_str(r_res.status)}], "
            f"but had no corresponding baseline run."
        )

    return gaps


def identify_open_concerns(
    dependency: Dependency,
    ai_recommendation: Optional[AIRecommendation] = None,
    verification_results: Optional[Union[list[VerificationResult], dict[str, list[VerificationResult]]]] = None,
    risk_flags: Optional[list[RiskFlag]] = None,
    usage_locations: Optional[list[UsageLocation]] = None,
    existing_concerns: Optional[list[str]] = None,
) -> list[str]:
    """Compute open concerns highlighting unverified risks, unexecuted tests, and pipeline failures.

    Points evaluated:
    1. Any dependency or test with status NOT_RUN (including reason).
    2. Risk flags with no matching verification test.
    3. AI-generated tests that were never executed.
    4. Usage complexity DEEP (behaviour differences tests may not catch).
    5. Anything that failed in the pipeline and was skipped.

    Args:
        dependency: Original dependency.
        ai_recommendation: Proposed replacement.
        verification_results: Test verification outputs.
        risk_flags: Identified risks.
        usage_locations: Detected code usage.
        existing_concerns: Any pre-existing concern strings to preserve.

    Returns:
        List of concern description strings.
    """
    dep_name = dependency.name
    repl_name = ai_recommendation.proposed_replacement if ai_recommendation else "replacement"

    concerns: list[str] = list(existing_concerns or [])
    _, _, all_results = _partition_verification_results(verification_results, dep_name, repl_name)

    # -----------------------------------------------------------------------
    # 1. Any dependency or test with status NOT_RUN (include the reason)
    # -----------------------------------------------------------------------
    if not all_results:
        concerns.append(
            f"Dependency '{dep_name}' has no test verification records (status NOT_RUN). "
            f"Reason: No verification test suite was executed."
        )
    else:
        for res in all_results:
            if _is_not_run(res.status):
                reason = getattr(res, "reason", None)
                if not reason and res.raw_output:
                    reason = res.raw_output.strip().splitlines()[0]
                if not reason:
                    reason = "Execution was skipped or environment setup failed"
                concerns.append(
                    f"Test '{res.test_name}' for '{res.dependency_name}' had status NOT_RUN. Reason: {reason}."
                )

    # -----------------------------------------------------------------------
    # 2. Risk flags with no matching verification
    # -----------------------------------------------------------------------
    if risk_flags:
        for flag in risk_flags:
            rtype_val = flag.risk_type.value if hasattr(flag.risk_type, "value") else str(flag.risk_type)
            rsev_val = flag.severity.value if hasattr(flag.severity, "value") else str(flag.severity)

            # Check if any test in all_results explicitly targets or mentions this risk
            matched = False
            flag_tokens = set(re.findall(r"\b[A-Za-z0-9_-]{4,}\b", flag.reason.lower()))
            flag_type_lower = rtype_val.lower()

            for res in all_results:
                test_str = (res.test_name + " " + res.raw_output).lower()
                # Check for risk type or CVE identifier in test
                if flag_type_lower in test_str:
                    matched = True
                    break
                # Check for CVE-XXXX-XXXX
                cves = re.findall(r"cve-\d{4}-\d+", flag.reason.lower())
                if any(cve in test_str for cve in cves):
                    matched = True
                    break
                # Check token overlap
                if len(flag_tokens & set(re.findall(r"\b[A-Za-z0-9_-]{4,}\b", test_str))) >= 2:
                    matched = True
                    break

            if not matched:
                concerns.append(
                    f"Unverified risk flag: [{rtype_val}] '{flag.reason}' (severity: {rsev_val}) "
                    f"has no matching verification test in the test suite."
                )

    # -----------------------------------------------------------------------
    # 3. AI-generated tests that were never executed
    # -----------------------------------------------------------------------
    if ai_recommendation:
        suggested_tests = getattr(ai_recommendation, "suggested_tests", []) or []
        executed_test_names = {_normalize_test_name(r.test_name).lower() for r in all_results}

        for idx, test_code in enumerate(suggested_tests, 1):
            # Extract test function name via regex
            fn_match = re.search(r"def\s+([a-zA-Z0-9_]+)\s*\(", str(test_code))
            test_identifier = fn_match.group(1) if fn_match else f"suggested_test_{idx}"

            # Check if this suggested test was executed in any verification result
            test_ran = any(
                test_identifier.lower() in executed_name or executed_name in test_identifier.lower()
                for executed_name in executed_test_names
            )

            if not test_ran:
                concerns.append(
                    f"AI-generated test '{test_identifier}' was never executed in the verification test suite."
                )

    # -----------------------------------------------------------------------
    # 3b. AI patch could not be parsed from model response (needs_manual_patch)
    # -----------------------------------------------------------------------
    if ai_recommendation and getattr(ai_recommendation, "needs_manual_patch", False):
        concerns.append(
            "AI patch could not be parsed from the model response; manual migration required"
        )

    # -----------------------------------------------------------------------
    # 4. Usage complexity DEEP (behaviour differences tests may not catch)
    # -----------------------------------------------------------------------
    if ai_recommendation:
        complexity = str(getattr(ai_recommendation, "usage_complexity", "")).strip().upper()
        if complexity == "DEEP":
            concerns.append(
                "Usage complexity is classified as DEEP: Codebase relies on internal, private, or complex "
                "interfaces of the dependency; behavioural differences and subtle edge-case regressions "
                "may not be caught by standard test suites."
            )

    # -----------------------------------------------------------------------
    # 5. Anything that failed in the pipeline and was skipped
    # -----------------------------------------------------------------------
    for res in all_results:
        # Check for pipeline ERROR
        if isinstance(res.status, VerificationStatus) and res.status == VerificationStatus.ERROR:
            details = res.raw_output.strip().splitlines()[0] if res.raw_output else "Execution failed"
            concerns.append(
                f"Pipeline execution failure: Test '{res.test_name}' failed with status ERROR. Details: {details}."
            )
        elif str(res.status).strip().upper() == "ERROR":
            details = res.raw_output.strip().splitlines()[0] if res.raw_output else "Execution failed"
            concerns.append(
                f"Pipeline execution failure: Test '{res.test_name}' failed with status ERROR. Details: {details}."
            )

    return concerns


# ---------------------------------------------------------------------------
# Main Capsule Assembly Function
# ---------------------------------------------------------------------------

def build_capsule(
    dependency: Dependency,
    risk_flags: list[RiskFlag],
    usage_locations: list[UsageLocation],
    ai_recommendation: Optional[AIRecommendation] = None,
    verification_results: Optional[Union[list[VerificationResult], dict[str, list[VerificationResult]]]] = None,
    gap_explanations: Optional[list[str]] = None,
    model_info: Optional[dict[str, Any]] = None,
    compatibility_gaps: Optional[list[str]] = None,
    open_concerns: Optional[list[str]] = None,
) -> ExitCapsule:
    """Assemble all analysis artifacts into a final ExitCapsule instance.

    Performs a pure data transformation:
    1. Assembles all inputs into one ExitCapsule:
       - Observed side: dependency, risk_flags, usage_locations, verification_results, compatibility_gaps.
       - AI-generated side: ai_recommendation (patch, steps, reasoning, suggested tests, complexity).
    2. Computes compatibility_gaps from observed BASELINE vs REPLACEMENT test results.
    3. Computes open_concerns for NOT_RUN items, unverified risk flags, unexecuted AI tests,
       DEEP complexity, and pipeline execution failures.
    4. Attaches gap_explanations as AI-generated interpretation, separate from observed gaps.
    5. Sets verified flag: Only a PASS from an executed test counts as verified. The AI recommendation
       stays 'unverified' unless its replacement run passed.

    Args:
        dependency: Discovered dependency object.
        risk_flags: List of risks flagged for this dependency.
        usage_locations: Source code locations using this dependency.
        ai_recommendation: AI-generated replacement plan.
        verification_results: Test verification outputs (list or {"baseline": [...], "replacement": [...]}).
        gap_explanations: AI-generated explanations of test failure gaps.
        model_info: Dictionary containing AI model metadata and terms.
        compatibility_gaps: Optional pre-computed compatibility gaps.
        open_concerns: Optional pre-computed open concerns.

    Returns:
        Structured ExitCapsule object.
    """
    dep_name = dependency.name
    repl_name = ai_recommendation.proposed_replacement if ai_recommendation else None

    # Partition verification results into baseline, replacement, and flat list
    baseline_map, replacement_map, flat_verification_results = _partition_verification_results(
        verification_results, dep_name, repl_name
    )

    # -----------------------------------------------------------------------
    # Verification Status Accounting:
    # "Only a PASS from an executed test counts as verified.
    #  The AI recommendation stays 'unverified' unless its replacement run passed."
    # -----------------------------------------------------------------------
    for res in flat_verification_results:
        res.verified = _is_pass(res.status)

    replacement_tests = list(replacement_map.values())
    has_replacement_tests = len(replacement_tests) > 0
    all_replacement_passed = (
        has_replacement_tests
        and all(_is_pass(r.status) for r in replacement_tests)
        and not any(_is_fail_or_error(r.status) for r in replacement_tests)
    )

    if ai_recommendation is not None:
        # AI recommendation verified is True ONLY if replacement run passed
        ai_recommendation.verified = bool(all_replacement_passed)

    # -----------------------------------------------------------------------
    # Compute Compatibility Gaps (Observed Facts from Execution)
    # -----------------------------------------------------------------------
    computed_gaps = identify_compatibility_gaps(
        dependency=dependency,
        ai_recommendation=ai_recommendation,
        verification_results=verification_results,
        usage_locations=usage_locations,
    )
    if compatibility_gaps:
        # Merge pre-provided gaps with computed gaps (preserving order & uniqueness)
        merged_gaps = list(compatibility_gaps)
        for g in computed_gaps:
            if g not in merged_gaps:
                merged_gaps.append(g)
        final_gaps = merged_gaps
    else:
        final_gaps = computed_gaps

    # -----------------------------------------------------------------------
    # Compute Open Concerns
    # -----------------------------------------------------------------------
    computed_concerns = identify_open_concerns(
        dependency=dependency,
        ai_recommendation=ai_recommendation,
        verification_results=verification_results,
        risk_flags=risk_flags,
        usage_locations=usage_locations,
        existing_concerns=open_concerns,
    )

    # -----------------------------------------------------------------------
    # Assemble Structured ExitCapsule Dataclass
    # -----------------------------------------------------------------------
    capsule = ExitCapsule(
        dependency=dependency,
        risk_flags=list(risk_flags or []),
        usage_locations=list(usage_locations or []),
        ai_recommendation=ai_recommendation,
        verification_results=flat_verification_results,
        compatibility_gaps=final_gaps,
        open_concerns=computed_concerns,
    )

    # Attach AI-generated interpretation metadata (strictly separate from observed gaps)
    capsule.gap_explanations = list(gap_explanations or [])
    capsule.gap_explanations_evidence = "ai_generated"
    capsule.model_info = dict(model_info or {})
    capsule.verified = bool(all_replacement_passed)

    return capsule


# Backwards compatibility alias for skeleton callers
def build_exit_capsule(
    dependency: Dependency,
    risk_flags: list[RiskFlag],
    usage_locations: list[UsageLocation],
    ai_recommendation: Optional[AIRecommendation] = None,
    verification_results: Optional[list[VerificationResult]] = None,
    compatibility_gaps: Optional[list[str]] = None,
    open_concerns: Optional[list[str]] = None,
    gap_explanations: Optional[list[str]] = None,
    model_info: Optional[dict[str, Any]] = None,
) -> ExitCapsule:
    """Wrapper delegating to build_capsule."""
    return build_capsule(
        dependency=dependency,
        risk_flags=risk_flags,
        usage_locations=usage_locations,
        ai_recommendation=ai_recommendation,
        verification_results=verification_results,
        gap_explanations=gap_explanations,
        model_info=model_info,
        compatibility_gaps=compatibility_gaps,
        open_concerns=open_concerns,
    )


# ---------------------------------------------------------------------------
# Standalone Verification and Demonstration
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("DepExit Capsule Builder: Standalone Verification")
    print("=" * 70)

    # 1. Mock Dependency & Observed Evidence
    dep = Dependency(name="requests", declared_version="2.28.0", source_file="requirements.txt")
    risks = [
        RiskFlag(
            dependency_name="requests",
            risk_type=RiskType.VULNERABLE,
            reason="CVE-2023-32681: Unintended leak of Proxy-Authorization header in redirects",
            severity=RiskSeverity.HIGH,
        ),
        RiskFlag(
            dependency_name="requests",
            risk_type=RiskType.UNMAINTAINED,
            reason="Repository shows lagging response to modern HTTP/2 / HTTP/3 standards",
            severity=RiskSeverity.LOW,
        ),
    ]
    usages = [
        UsageLocation(
            dependency_name="requests",
            file_path="src/client.py",
            line_number=10,
            usage_type=UsageType.CALL,
            code_snippet="resp = requests.get('https://example.com')",
        )
    ]

    # 2. Mock AI Recommendation (AI-generated side)
    rec = AIRecommendation(
        dependency_name="requests",
        proposed_replacement="httpx",
        migration_steps=["pip install httpx", "replace requests with httpx"],
        reasoning="httpx is a modern drop-in replacement with async support.",
        ai_generated=True,
    )
    rec.usage_complexity = "DEEP"
    rec.code_patch = "--- a/src/client.py\n+++ b/src/client.py\n@@ -1 +1 @@\n-import requests\n+import httpx\n"
    rec.suggested_tests = [
        "def test_client_fetch_status():\n    assert True\n",
        "def test_unexecuted_check():\n    assert True\n",
    ]
    rec.candidates = [{"name": "httpx", "why": "modern", "api_gaps": "none"}]
    rec.evidence = "ai_generated"

    # 3. Mock Verification Results (Baseline vs Replacement)
    baseline_results = [
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
        VerificationResult(
            dependency_name="requests",
            test_name="test_baseline_only_feature",
            status=VerificationStatus.PASS,
            raw_output="PASSED in 0.01s",
        ),
    ]

    replacement_results = [
        # Regression: PASS in baseline, FAIL in replacement!
        VerificationResult(
            dependency_name="httpx",
            test_name="test_headers_roundtrip",
            status=VerificationStatus.FAIL,
            raw_output="AssertionError: Header keys were case-preserved differently in httpx",
        ),
        # Executed AI-suggested test: PASS
        VerificationResult(
            dependency_name="httpx",
            test_name="test_client_fetch_status",
            status=VerificationStatus.PASS,
            raw_output="PASSED in 0.03s",
        ),
        # Unexecuted test
        VerificationResult(
            dependency_name="httpx",
            test_name="test_proxy_auth_leak",
            status=VerificationStatus.NOT_RUN,
            raw_output="Environment missing proxy mock fixture",
        ),
        # Pipeline error test
        VerificationResult(
            dependency_name="httpx",
            test_name="test_stream_download",
            status=VerificationStatus.ERROR,
            raw_output="BrokenPipeError: Test harness interrupted",
        ),
    ]

    verification_dict = {
        "baseline": baseline_results,
        "replacement": replacement_results,
    }

    mock_gap_explanations = [
        "[test_headers_roundtrip] Likely Cause: Case-insensitivity in headers handling differs between requests.Structures.CaseInsensitiveDict and httpx.Headers. Suggested Fix: Access headers using lowercased keys or .get() with lowercase."
    ]

    mock_model_info = {
        "model_id": "gemma-4-31b-it",
        "version": "4",
        "access_method": "hosted_api",
        "terms_url": "https://ai.google.dev/gemma/terms",
    }

    # 4. Assemble Capsule
    capsule = build_capsule(
        dependency=dep,
        risk_flags=risks,
        usage_locations=usages,
        ai_recommendation=rec,
        verification_results=verification_dict,
        gap_explanations=mock_gap_explanations,
        model_info=mock_model_info,
    )

    print("\n--- Summary of Assembled Capsule ---")
    print(f"Dependency: {capsule.dependency.name} (Declared: {capsule.dependency.declared_version})")
    print(f"Risk Flags Count: {len(capsule.risk_flags)}")
    print(f"Usage Locations Count: {len(capsule.usage_locations)}")
    print(f"AI Recommendation Proposed: {capsule.ai_recommendation.proposed_replacement}")
    print(f"AI Recommendation Verified: {capsule.ai_recommendation.verified}")
    print(f"Capsule Verified: {capsule.verified}")

    print(f"\n--- Observed Compatibility Gaps ({len(capsule.compatibility_gaps)}) ---")
    for idx, g in enumerate(capsule.compatibility_gaps, 1):
        print(f"  [{idx}] {g}")

    print(f"\n--- Open Concerns ({len(capsule.open_concerns)}) ---")
    for idx, c in enumerate(capsule.open_concerns, 1):
        print(f"  [{idx}] {c}")

    print(f"\n--- AI Gap Explanations ({len(capsule.gap_explanations)}) [evidence={capsule.gap_explanations_evidence}] ---")
    for idx, exp in enumerate(capsule.gap_explanations, 1):
        print(f"  [{idx}] {exp}")

    # 5. Assertions on Requirements
    # Req 1: Assembly
    assert capsule.dependency == dep
    assert len(capsule.risk_flags) == 2
    assert len(capsule.usage_locations) == 1
    assert capsule.ai_recommendation == rec

    # Req 2: Regression and single-run detection
    assert any("Regression in 'test_headers_roundtrip'" in g for g in capsule.compatibility_gaps), (
        "Must flag regression when PASS in baseline and FAIL in replacement"
    )
    assert any("test_baseline_only_feature" in g for g in capsule.compatibility_gaps), (
        "Must flag baseline-only test"
    )
    assert any("test_client_fetch_status" in g for g in capsule.compatibility_gaps), (
        "Must flag replacement-only test"
    )

    # Req 3: Open Concerns
    # 3.1: NOT_RUN
    assert any("test_proxy_auth_leak" in c and "NOT_RUN" in c for c in capsule.open_concerns), (
        "Must flag NOT_RUN test with reason"
    )
    # 3.2: Unverified risk flag
    assert any("Unverified risk flag" in c and "UNMAINTAINED" in c for c in capsule.open_concerns), (
        "Must flag risk flags with no verification test"
    )
    # 3.3: AI-generated test never executed
    assert any("test_unexecuted_check" in c for c in capsule.open_concerns), (
        "Must flag unexecuted AI suggested tests"
    )
    # 3.4: Usage complexity DEEP
    assert any("classified as DEEP" in c for c in capsule.open_concerns), (
        "Must flag DEEP usage complexity concern"
    )
    # 3.5: Pipeline execution error
    assert any("test_stream_download" in c and "ERROR" in c for c in capsule.open_concerns), (
        "Must flag pipeline error"
    )

    # Req 4: Gap explanations & Verification Accounting
    assert len(capsule.gap_explanations) == 1
    assert capsule.gap_explanations_evidence == "ai_generated"
    # Because replacement run had FAIL/ERROR tests, ai_recommendation.verified must be False!
    assert capsule.ai_recommendation.verified is False, "Recommendation must stay unverified if tests failed"
    assert capsule.verified is False

    # Test that when replacement passes cleanly, verified becomes True
    clean_replacement_results = [
        VerificationResult(
            dependency_name="httpx",
            test_name="test_get_status",
            status=VerificationStatus.PASS,
        )
    ]
    clean_capsule = build_capsule(
        dependency=dep,
        risk_flags=risks,
        usage_locations=usages,
        ai_recommendation=rec,
        verification_results={"replacement": clean_replacement_results},
        gap_explanations=[],
        model_info=mock_model_info,
    )
    assert clean_capsule.ai_recommendation.verified is True
    assert clean_capsule.verified is True

    print("\n" + "=" * 70)
    print("ALL CAPSULE BUILDER CONTRACTS AND ASSERTIONS PASSED SUCCESSFULLY!")
    print("=" * 70)
