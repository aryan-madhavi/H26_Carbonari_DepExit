"""Dependency discovery for Python repositories.

Scans the target repository for dependency declarations across manifest files
such as requirements.txt, pyproject.toml, and setup.py.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from typing import Union

# Ensure parent directory is in sys.path for standalone script execution
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

try:
    from schema import Dependency
except ImportError:
    from depexit.schema import Dependency

from packaging.requirements import InvalidRequirement, Requirement


def parse_requirements_file(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a requirements.txt file.

    Args:
        file_path: Path to the requirements.txt file.

    Returns:
        List of Dependency objects extracted from the requirements file.
    """
    path = Path(file_path)
    if not path.is_file():
        return []

    dependencies: list[Dependency] = []
    seen: set[str] = set()

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue
        # Skip pip options (-r, -i, --extra-index-url, -f, etc.)
        if line.startswith(("-", "--")):
            continue
        # Remove inline comments
        if " #" in line:
            line = line.split(" #", 1)[0].strip()

        try:
            req = Requirement(line)
            name = req.name
            spec_str = str(req.specifier)
            if spec_str.startswith("=="):
                declared_version = spec_str[2:].strip()
            elif spec_str:
                declared_version = spec_str.strip()
            else:
                declared_version = ""
        except InvalidRequirement:
            match = re.match(r"^([a-zA-Z0-9_.\-]+)(?:([=><!~^]+)([^;\s]+))?", line)
            if not match:
                continue
            name = match.group(1)
            op = match.group(2) or ""
            ver = match.group(3) or ""
            if op == "==":
                declared_version = ver.strip()
            elif op:
                declared_version = f"{op}{ver}".strip()
            else:
                declared_version = ""

        norm_key = name.lower().replace("_", "-")
        if norm_key in seen:
            continue
        seen.add(norm_key)

        dependencies.append(
            Dependency(
                name=name,
                declared_version=declared_version,
                source_file=path.name,
            )
        )

    return dependencies


def parse_pyproject_toml(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a pyproject.toml file.

    Args:
        file_path: Path to the pyproject.toml file.

    Returns:
        List of Dependency objects extracted from pyproject.toml.
    """
    path = Path(file_path)
    if not path.is_file():
        return []

    dependencies: list[Dependency] = []
    seen: set[str] = set()

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []

    # 1. PEP 621: project.dependencies
    project_deps = data.get("project", {}).get("dependencies", [])
    if isinstance(project_deps, list):
        for item in project_deps:
            if not isinstance(item, str):
                continue
            try:
                req = Requirement(item)
                name = req.name
                spec_str = str(req.specifier)
                if spec_str.startswith("=="):
                    declared_version = spec_str[2:].strip()
                elif spec_str:
                    declared_version = spec_str.strip()
                else:
                    declared_version = ""
            except InvalidRequirement:
                match = re.match(r"^([a-zA-Z0-9_.\-]+)(?:([=><!~^]+)([^;\s]+))?", item)
                if not match:
                    continue
                name = match.group(1)
                op = match.group(2) or ""
                ver = match.group(3) or ""
                declared_version = ver.strip() if op == "==" else f"{op}{ver}".strip()

            norm_key = name.lower().replace("_", "-")
            if norm_key in seen:
                continue
            seen.add(norm_key)

            dependencies.append(
                Dependency(
                    name=name,
                    declared_version=declared_version,
                    source_file=path.name,
                )
            )

    # 2. Poetry: tool.poetry.dependencies
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry_deps, dict):
        for name, ver_spec in poetry_deps.items():
            if name.lower() == "python":
                continue
            if isinstance(ver_spec, str):
                declared_version = ver_spec.strip()
            elif isinstance(ver_spec, dict):
                declared_version = str(ver_spec.get("version", "")).strip()
            else:
                declared_version = ""

            norm_key = name.lower().replace("_", "-")
            if norm_key in seen:
                continue
            seen.add(norm_key)

            dependencies.append(
                Dependency(
                    name=name,
                    declared_version=declared_version,
                    source_file=path.name,
                )
            )

    return dependencies


def parse_setup_py(file_path: Union[str, Path]) -> list[Dependency]:
    """Parse dependencies from a setup.py file.

    Args:
        file_path: Path to the setup.py file.

    Returns:
        List of Dependency objects extracted from install_requires in setup.py.
    """
    path = Path(file_path)
    if not path.is_file():
        return []

    dependencies: list[Dependency] = []
    seen: set[str] = set()

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    raw_reqs: list[str] = []

    # Attempt AST parse
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "install_requires":
                        if isinstance(kw.value, (ast.List, ast.Tuple)):
                            for elt in kw.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    raw_reqs.append(elt.value)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "install_requires":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    raw_reqs.append(elt.value)
    except Exception:
        pass

    # Fallback regex if AST did not extract anything
    if not raw_reqs:
        match = re.search(r"install_requires\s*=\s*\[(.*?)\]", content, re.DOTALL)
        if match:
            for item in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)):
                raw_reqs.append(item)

    for item in raw_reqs:
        try:
            req = Requirement(item)
            name = req.name
            spec_str = str(req.specifier)
            if spec_str.startswith("=="):
                declared_version = spec_str[2:].strip()
            elif spec_str:
                declared_version = spec_str.strip()
            else:
                declared_version = ""
        except InvalidRequirement:
            match = re.match(r"^([a-zA-Z0-9_.\-]+)(?:([=><!~^]+)([^;\s]+))?", item)
            if not match:
                continue
            name = match.group(1)
            op = match.group(2) or ""
            ver = match.group(3) or ""
            declared_version = ver.strip() if op == "==" else f"{op}{ver}".strip()

        norm_key = name.lower().replace("_", "-")
        if norm_key in seen:
            continue
        seen.add(norm_key)

        dependencies.append(
            Dependency(
                name=name,
                declared_version=declared_version,
                source_file=path.name,
            )
        )

    return dependencies


def discover_dependencies(repo_path: Union[str, Path]) -> list[Dependency]:
    """Scan a target repository and discover all declared dependencies.

    Parses requirements.txt, pyproject.toml, and setup.py (whichever exist)
    in the given repository path. Missing files are handled gracefully without crashing.

    Args:
        repo_path: Path to the target Python repository directory or manifest file.

    Returns:
        A list of discovered Dependency objects.
    """
    path = Path(repo_path).resolve()
    if not path.exists():
        return []

    # If repo_path is directly a file
    if path.is_file():
        if path.name == "pyproject.toml":
            return parse_pyproject_toml(path)
        elif path.name == "setup.py":
            return parse_setup_py(path)
        else:
            return parse_requirements_file(path)

    # If repo_path is a directory, check all supported manifest files
    discovered: list[Dependency] = []

    # 1. Check requirements.txt
    req_file = path / "requirements.txt"
    if req_file.is_file():
        discovered.extend(parse_requirements_file(req_file))

    # 2. Check pyproject.toml
    pyproject_file = path / "pyproject.toml"
    if pyproject_file.is_file():
        discovered.extend(parse_pyproject_toml(pyproject_file))

    # 3. Check setup.py
    setup_file = path / "setup.py"
    if setup_file.is_file():
        discovered.extend(parse_setup_py(setup_file))

    return discovered


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    results = discover_dependencies(target)
    print(f"Discovered {len(results)} dependencies in '{target}':")
    for dep in results:
        ver = (dep.declared_version if any(dep.declared_version.startswith(op) for op in ("==", ">=", "<=", "~=", "!=", "<", ">")) else f"=={dep.declared_version}") if dep.declared_version else "(unpinned)"
        print(f"  - {dep.name} {ver} [{dep.source_file}]")
