"""Scanner module for discovering dependencies and assessing risks."""

try:
    from scanner.discover import (
        discover_dependencies,
        parse_pyproject_toml,
        parse_requirements_file,
        parse_setup_py,
    )
    from scanner.risk_assess import assess_risk, assess_risks
except ImportError:
    from depexit.scanner.discover import (
        discover_dependencies,
        parse_pyproject_toml,
        parse_requirements_file,
        parse_setup_py,
    )
    from depexit.scanner.risk_assess import assess_risk, assess_risks

__all__ = [
    "discover_dependencies",
    "parse_requirements_file",
    "parse_pyproject_toml",
    "parse_setup_py",
    "assess_risk",
    "assess_risks",
]
