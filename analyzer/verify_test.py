"""Test verification module for candidate dependency replacements.

Runs the target repository's test suite (e.g. pytest, unittest) against proposed
replacements or migration shims to verify whether compatibility regressions occur.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

try:
    from schema import VerificationResult, VerificationStatus
except ImportError:
    from depexit.schema import VerificationResult, VerificationStatus


def run_verification_tests(
    repo_path: Union[str, Path],
    dependency_name: str,
    replacement_name: str,
) -> list[VerificationResult]:
    """Execute verification tests in the repository context to evaluate a replacement.

    Args:
        repo_path: Path to the target repository.
        dependency_name: Original risky dependency name.
        replacement_name: Proposed replacement library name.

    Returns:
        List of VerificationResult objects detailing individual test results and output.
    """
    # TODO: Set up test environment, run test suite, and collect results
    raise NotImplementedError("TODO: implement run_verification_tests")


def execute_test_suite(
    repo_path: Union[str, Path],
    test_command: Optional[list[str]] = None,
) -> VerificationResult:
    """Execute a specified test command inside the target repository.

    Args:
        repo_path: Path to the target repository.
        test_command: Command list to run (defaults to pytest / unittest discovery).

    Returns:
        VerificationResult containing test status and raw output.
    """
    # TODO: Run subprocess test runner and parse exit code + stdout/stderr
    raise NotImplementedError("TODO: implement execute_test_suite")
