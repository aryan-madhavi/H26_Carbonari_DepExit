"""Scanner module for discovering dependencies and assessing risks."""

try:
    from scanner.discover import discover_dependencies
    from scanner.risk_assess import assess_risks
except ImportError:
    from depexit.scanner.discover import discover_dependencies
    from depexit.scanner.risk_assess import assess_risks

__all__ = ["discover_dependencies", "assess_risks"]
