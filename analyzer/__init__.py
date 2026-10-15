"""Analyzer module for mapping dependency usage and verifying replacements with tests."""

try:
    from analyzer.usage_mapper import map_dependency_usage
    from analyzer.verify_test import run_verification_tests
except ImportError:
    from depexit.analyzer.usage_mapper import map_dependency_usage
    from depexit.analyzer.verify_test import run_verification_tests

__all__ = ["map_dependency_usage", "run_verification_tests"]
