"""Core schema and data contracts for DepExit.

This module defines the shared dataclasses and enums used across all components
of the DepExit pipeline (discovery, risk assessment, usage mapping, AI recommendation,
verification testing, and capsule reporting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskType(str, Enum):
    """Enumeration of possible risk types for a dependency."""

    UNMAINTAINED = "UNMAINTAINED"
    DEPRECATED = "DEPRECATED"
    VULNERABLE = "VULNERABLE"
    EOL = "EOL"
    INCOMPATIBLE = "INCOMPATIBLE"


class RiskSeverity(str, Enum):
    """Severity levels for risk flags."""

    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


# Alias for convenience
Severity = RiskSeverity


class UsageType(str, Enum):
    """Types of dependency usage detected in codebase."""

    IMPORT = "IMPORT"
    CALL = "CALL"
    CLASS_INSTANCE = "CLASS_INSTANCE"
    ATTRIBUTE = "ATTRIBUTE"


class VerificationStatus(str, Enum):
    """Execution status for verification test runs."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"


@dataclass
class Dependency:
    """Represents a discovered dependency in the target repository."""

    name: str
    declared_version: str
    source_file: str


@dataclass
class RiskFlag:
    """Represents an identified risk flag associated with a dependency."""

    dependency_name: str
    risk_type: RiskType
    reason: str
    severity: RiskSeverity
    evidence: str = "observed"


@dataclass
class UsageLocation:
    """Represents a specific code location where a dependency is utilized."""

    dependency_name: str
    file_path: str
    line_number: int
    usage_type: UsageType
    code_snippet: str
    evidence: str = "observed"


@dataclass
class AIRecommendation:
    """Represents an AI-generated replacement proposal for a risky dependency."""

    dependency_name: str
    proposed_replacement: str
    migration_steps: list[str] = field(default_factory=list)
    reasoning: str = ""
    ai_generated: bool = True
    evidence: str = "ai_generated"


@dataclass
class VerificationResult:
    """Represents the result of running verification tests against a replacement."""

    dependency_name: str
    test_name: str
    status: VerificationStatus
    raw_output: str = ""
    verified: bool = True
    evidence: str = "observed"


@dataclass
class ExitCapsule:
    """Structured Dependency Exit Capsule containing all findings and recommendations."""

    dependency: Dependency
    risk_flags: list[RiskFlag] = field(default_factory=list)
    usage_locations: list[UsageLocation] = field(default_factory=list)
    ai_recommendation: Optional[AIRecommendation] = None
    verification_results: list[VerificationResult] = field(default_factory=list)
    compatibility_gaps: list[str] = field(default_factory=list)
    open_concerns: list[str] = field(default_factory=list)
