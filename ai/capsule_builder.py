"""Capsule builder module for assembling Dependency Exit Capsules.

Consolidates scanner findings, code usage analysis, AI migration advice,
and test verification results into a structured ExitCapsule dataclass.
"""

from __future__ import annotations

from typing import Optional

try:
    from schema import (
        AIRecommendation,
        Dependency,
        ExitCapsule,
        RiskFlag,
        UsageLocation,
        VerificationResult,
    )
except ImportError:
    from depexit.schema import (
        AIRecommendation,
        Dependency,
        ExitCapsule,
        RiskFlag,
        UsageLocation,
        VerificationResult,
    )


def build_exit_capsule(
    dependency: Dependency,
    risk_flags: list[RiskFlag],
    usage_locations: list[UsageLocation],
    ai_recommendation: Optional[AIRecommendation] = None,
    verification_results: Optional[list[VerificationResult]] = None,
    compatibility_gaps: Optional[list[str]] = None,
    open_concerns: Optional[list[str]] = None,
) -> ExitCapsule:
    """Assemble all analysis artifacts into a final ExitCapsule instance.

    Args:
        dependency: Discovered dependency object.
        risk_flags: List of risks flagged for this dependency.
        usage_locations: Source code locations using this dependency.
        ai_recommendation: AI-generated replacement plan.
        verification_results: Test verification outputs.
        compatibility_gaps: Identified API incompatibilities.
        open_concerns: Unresolved technical concerns or edge cases.

    Returns:
        Structured ExitCapsule object.
    """
    # TODO: Correlate findings, populate gaps/concerns if not provided, and instantiate ExitCapsule
    raise NotImplementedError("TODO: implement build_exit_capsule")


def identify_compatibility_gaps(
    dependency: Dependency,
    recommendation: Optional[AIRecommendation],
    usages: list[UsageLocation],
) -> list[str]:
    """Detect known API behavioral differences or missing equivalents in candidate replacement.

    Args:
        dependency: Original dependency.
        recommendation: Proposed replacement details.
        usages: Detected usage patterns in the codebase.

    Returns:
        List of identified compatibility gap descriptions.
    """
    # TODO: Compare API signatures and usage patterns against replacement capabilities
    raise NotImplementedError("TODO: implement identify_compatibility_gaps")


def identify_open_concerns(
    dependency: Dependency,
    recommendation: Optional[AIRecommendation],
    verification_results: list[VerificationResult],
) -> list[str]:
    """Highlight remaining risks, failed tests, or human review requirements.

    Args:
        dependency: Original dependency.
        recommendation: Proposed replacement.
        verification_results: Verification test outputs.

    Returns:
        List of concern descriptions.
    """
    # TODO: Analyze verification failures, license differences, and performance considerations
    raise NotImplementedError("TODO: implement identify_open_concerns")
