"""Analyzer module for mapping dependency usage and verifying replacements with tests."""

try:
    from analyzer.usage_mapper import find_usages_in_file, map_dependency_usage, map_usage
    from analyzer.verify_test import execute_test_suite, run_verification_tests, verify_replacement
except ImportError:
    from depexit.analyzer.usage_mapper import find_usages_in_file, map_dependency_usage, map_usage
    from depexit.analyzer.verify_test import execute_test_suite, run_verification_tests, verify_replacement

__all__ = [
    "map_usage",
    "map_dependency_usage",
    "find_usages_in_file",
    "verify_replacement",
    "run_verification_tests",
    "execute_test_suite",
]
