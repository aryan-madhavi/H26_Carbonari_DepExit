"""Dependency discovery for Python repositories.

Scans the target repository for dependency declarations across manifest files
such as requirements.txt, pyproject.toml, Pipfile, and setup.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

try:
    from schema import Dependency
except ImportError:
    from depexit.schema import Dependency


def discover_dependencies(repo_path: Union[str, Path]) -> list[Dependency]:
    """Scan a target repository and discover all declared dependencies.

    Args:
        repo_path: Path to the root of the target Python repository.

    Returns:
        A list of discovered Dependency objects.
    """
    # TODO: Traverse repo_path, locate manifest files, and parse dependencies
    raise NotImplementedError("TODO: implement discover_dependencies")


def parse_requirements_file(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a requirements.txt file.

    Args:
        file_path: Path to the requirements.txt file.

    Returns:
        List of Dependency objects extracted from the requirements file.
    """
    # TODO: Parse standard requirements.txt lines into Dependency instances
    raise NotImplementedError("TODO: implement parse_requirements_file")


def parse_pyproject_toml(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a pyproject.toml file.

    Args:
        file_path: Path to the pyproject.toml file.

    Returns:
        List of Dependency objects extracted from pyproject.toml.
    """
    # TODO: Parse tool.poetry.dependencies, project.dependencies, or flit configurations
    raise NotImplementedError("TODO: implement parse_pyproject_toml")


def parse_setup_py(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a setup.py file.

    Args:
        file_path: Path to the setup.py file.

    Returns:
        List of Dependency objects extracted from install_requires in setup.py.
    """
    # TODO: Parse install_requires from setup.py via AST or packaging utilities
    raise NotImplementedError("TODO: implement parse_setup_py")
