"""Usage mapper module for tracking dependency usage across Python source code.

Analyzes the target repository using AST parsing to detect import statements,
function/method calls, class instantiations, and attribute lookups corresponding
to a given dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

try:
    from schema import UsageLocation, UsageType
except ImportError:
    from depexit.schema import UsageLocation, UsageType


def map_dependency_usage(
    repo_path: Union[str, Path],
    dependency_name: str,
) -> list[UsageLocation]:
    """Scan all Python files in the repository to map usage of a specified dependency.

    Args:
        repo_path: Root path of the target Python repository.
        dependency_name: Package name of the dependency to trace.

    Returns:
        List of UsageLocation instances detailing file, line number, usage type, and snippet.
    """
    # TODO: Traverse Python source files (.py) and parse AST to collect usage locations
    raise NotImplementedError("TODO: implement map_dependency_usage")


def find_usages_in_file(
    file_path: Union[str, Path],
    dependency_name: str,
) -> list[UsageLocation]:
    """Find all usages of a dependency within a single Python file using AST analysis.

    Args:
        file_path: Path to the Python source file.
        dependency_name: Name of the dependency to search for.

    Returns:
        List of UsageLocation instances found in the file.
    """
    # TODO: Parse file into AST, walk nodes, and identify imports, calls, attributes, instances
    raise NotImplementedError("TODO: implement find_usages_in_file")


def extract_code_snippet(
    file_path: Union[str, Path],
    line_number: int,
    context_lines: int = 2,
) -> str:
    """Extract a multi-line code snippet around a specific line number.

    Args:
        file_path: Path to the source file.
        line_number: 1-based target line number.
        context_lines: Number of surrounding lines to include.

    Returns:
        Formatted code snippet string.
    """
    # TODO: Read source file and slice lines around line_number
    raise NotImplementedError("TODO: implement extract_code_snippet")
