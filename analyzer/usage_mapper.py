"""Usage mapper module for tracking dependency usage across Python source code.

Analyzes the target repository using Python's AST module to detect imports,
function calls, class instantiations, and attribute lookups tied to target dependencies.
Translates PyPI package names to actual import names before resolving AST nodes.
"""

from __future__ import annotations

import ast
import os
import sys
import warnings
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
    from schema import UsageLocation, UsageType
except ImportError:
    from depexit.schema import UsageLocation, UsageType

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "ENV",
    ".env",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".eggs",
    ".mypy_cache",
}

# Known mappings from PyPI package names to their actual top-level import root names
KNOWN_PYPI_TO_IMPORT_MAP: dict[str, list[str]] = {
    "pyyaml": ["yaml"],
    "beautifulsoup4": ["bs4"],
    "python-dateutil": ["dateutil"],
    "pillow": ["PIL", "pil"],
    "scikit-learn": ["sklearn"],
    "opencv-python": ["cv2"],
    "opencv-python-headless": ["cv2"],
    "typing-extensions": ["typing_extensions"],
    "protobuf": ["google.protobuf", "google"],
    "paho-mqtt": ["paho"],
    "msgpack-python": ["msgpack"],
    "pymongo": ["pymongo", "bson"],
    "setuptools": ["setuptools", "pkg_resources"],
    "attrs": ["attr", "attrs"],
    "psycopg2-binary": ["psycopg2"],
    "markupsafe": ["markupsafe"],
    "jinja2": ["jinja2"],
    "importlib-metadata": ["importlib_metadata"],
    "pydantic-core": ["pydantic_core"],
}


def resolve_import_roots(pypi_name: str) -> list[str]:
    """Translate a PyPI package name to its actual import root name(s).

    Uses a known mapping for packages where import name differs from PyPI name
    (e.g. PyYAML -> yaml, beautifulsoup4 -> bs4, python-dateutil -> dateutil),
    falling back to the package name itself (with hyphens replaced by underscores).

    Args:
        pypi_name: Name of the package on PyPI (e.g. from discover_dependencies).

    Returns:
        List of importable top-level module names.
    """
    clean_pypi = pypi_name.strip().lower().replace("_", "-")
    if clean_pypi in KNOWN_PYPI_TO_IMPORT_MAP:
        return KNOWN_PYPI_TO_IMPORT_MAP[clean_pypi]

    clean_under = pypi_name.strip().lower().replace("-", "_")
    if clean_under in KNOWN_PYPI_TO_IMPORT_MAP:
        return KNOWN_PYPI_TO_IMPORT_MAP[clean_under]

    # Fallback to normalized package name
    return [clean_under]


class _DependencyUsageVisitor(ast.NodeVisitor):
    """AST NodeVisitor that tracks imports and resolves usages of target dependencies."""

    def __init__(
        self,
        file_path: str,
        source_lines: list[str],
        dep_map: dict[str, str],
    ) -> None:
        self.file_path = file_path
        self.source_lines = source_lines
        self.dep_map = dep_map  # import_root -> original_pypi_dep_name
        self.symbols: dict[str, dict] = {}  # local_name -> {'dep': str, 'is_class': bool}
        self.instances: dict[str, str] = {}  # local_name -> dep_name
        self.usages: list[UsageLocation] = []
        self._call_func_ids: set[int] = set()
        self._seen_records: set[tuple[str, int, UsageType]] = set()

    def _get_snippet(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()
        return ""

    def _record(self, dep_name: str, lineno: int, usage_type: UsageType) -> None:
        key = (dep_name, lineno, usage_type)
        if key not in self._seen_records:
            self._seen_records.add(key)
            self.usages.append(
                UsageLocation(
                    dependency_name=dep_name,
                    file_path=self.file_path,
                    line_number=lineno,
                    usage_type=usage_type,
                    code_snippet=self._get_snippet(lineno),
                    evidence="observed",
                )
            )

    def _resolve_chain(self, node: ast.AST) -> tuple[str | None, list[str]]:
        """Resolve a chain of attribute lookups (e.g. requests.Session.request)."""
        attrs: list[str] = []
        curr = node
        while isinstance(curr, ast.Attribute):
            attrs.append(curr.attr)
            curr = curr.value
        if isinstance(curr, ast.Name):
            attrs.append(curr.id)
            attrs.reverse()
            return attrs[0], attrs[1:]
        return None, []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0].lower()
            if root in self.dep_map:
                dep_name = self.dep_map[root]
                self._record(dep_name, node.lineno, UsageType.IMPORT)
                bound_name = alias.asname or alias.name.split(".")[0]
                self.symbols[bound_name] = {"dep": dep_name, "is_class": False}
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root = node.module.split(".")[0].lower()
            if root in self.dep_map:
                dep_name = self.dep_map[root]
                self._record(dep_name, node.lineno, UsageType.IMPORT)
                for alias in node.names:
                    bound_name = alias.asname or alias.name
                    # Heuristic for class: PascalCase (e.g. Session, Environment, BaseModel)
                    is_class = bool(alias.name and alias.name[0].isupper() and not alias.name.isupper())
                    self.symbols[bound_name] = {"dep": dep_name, "is_class": is_class}
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Check base classes (e.g. class Site(BaseModel):)
        for base in node.bases:
            root, _ = self._resolve_chain(base)
            if root in self.symbols:
                dep_name = self.symbols[root]["dep"]
                self._record(dep_name, base.lineno, UsageType.CLASS_INSTANCE)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Track variable assignments from dependency calls or instantiations (e.g. session = S())
        if isinstance(node.value, ast.Call):
            root, _ = self._resolve_chain(node.value.func)
            dep_name = None
            if root in self.symbols:
                dep_name = self.symbols[root]["dep"]
            elif root in self.instances:
                dep_name = self.instances[root]
            if dep_name:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.instances[target.id] = dep_name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._call_func_ids.add(id(node.func))
        root, attrs = self._resolve_chain(node.func)

        if root in self.symbols:
            info = self.symbols[root]
            dep_name = info["dep"]
            # Check if calling a class constructor (either bound class or attr in PascalCase)
            is_class = info["is_class"] or (attrs and attrs[-1][0].isupper() and not attrs[-1].isupper())
            usage_type = UsageType.CLASS_INSTANCE if is_class else UsageType.CALL
            self._record(dep_name, node.lineno, usage_type)
        elif root in self.instances:
            dep_name = self.instances[root]
            self._record(dep_name, node.lineno, UsageType.CALL)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Avoid double-counting attribute accesses that are the func of an enclosing Call
        if id(node) not in self._call_func_ids:
            root, _ = self._resolve_chain(node)
            if root in self.symbols:
                dep_name = self.symbols[root]["dep"]
                self._record(dep_name, node.lineno, UsageType.ATTRIBUTE)
            elif root in self.instances:
                dep_name = self.instances[root]
                self._record(dep_name, node.lineno, UsageType.ATTRIBUTE)

        self.generic_visit(node)


def find_usages_in_file(
    file_path: Union[str, Path],
    dependency_names: list[str],
    display_path: str | None = None,
) -> list[UsageLocation]:
    """Parse a single Python file and map usages of the specified dependencies.

    Translates PyPI package names to their actual import names (e.g.
    python-dateutil -> dateutil, PyYAML -> yaml, beautifulsoup4 -> bs4).

    Args:
        file_path: Absolute or relative filesystem path to the Python source file.
        dependency_names: List of dependency names to search for (PyPI package names).
        display_path: Optional path string used in the resulting UsageLocation objects.

    Returns:
        List of UsageLocation instances found in the file with evidence="observed".
    """
    path = Path(file_path)
    if not path.is_file():
        return []

    # Build translation map: import_root -> original_pypi_dep_name
    dep_map: dict[str, str] = {}
    for dep in dependency_names:
        for root in resolve_import_roots(dep):
            dep_map[root.lower()] = dep

    try:
        source_code = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        warnings.warn(f"Failed to read file {file_path}: {e}", RuntimeWarning)
        return []

    try:
        tree = ast.parse(source_code, filename=str(path))
    except SyntaxError as e:
        warnings.warn(f"Syntax error in {file_path}:{e.lineno}: {e.msg}", SyntaxWarning)
        return []
    except Exception as e:
        warnings.warn(f"Failed to parse AST for {file_path}: {e}", RuntimeWarning)
        return []

    rel_name = display_path or str(path)
    visitor = _DependencyUsageVisitor(rel_name, source_code.splitlines(), dep_map)
    visitor.visit(tree)
    return visitor.usages


def map_usage(
    repo_path: Union[str, Path],
    dependency_names: list[str],
) -> list[UsageLocation]:
    """Walk every .py file in repo_path and map all usages of specified dependencies.

    Uses Python's ast module to detect import statements, function calls,
    class instantiations, and attribute accesses tied to the target dependencies.
    Resolves dependencies by their actual import names (e.g. python-dateutil -> dateutil).

    Args:
        repo_path: Path to the target repository directory.
        dependency_names: Collection of PyPI dependency package names to track.

    Returns:
        List of UsageLocation objects with evidence="observed",
        sorted by dependency name then file path.
    """
    repo = Path(repo_path).resolve()
    if not repo.exists():
        warnings.warn(f"Target repository path '{repo_path}' does not exist.", UserWarning)
        return []

    if not dependency_names:
        return []

    # If repo_path is directly a Python file
    if repo.is_file():
        if repo.suffix == ".py":
            usages = find_usages_in_file(repo, dependency_names, display_path=repo.name)
            usages.sort(key=lambda u: (u.dependency_name.lower(), u.file_path, u.line_number))
            return usages
        return []

    all_usages: list[UsageLocation] = []

    # Walk directory tree, pruning excluded directories like .venv, .git, etc.
    for root, dirs, files in os.walk(repo):
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
        for filename in files:
            if filename.endswith(".py"):
                file_path = Path(root) / filename
                rel_path = os.path.relpath(file_path, repo)
                file_usages = find_usages_in_file(
                    file_path=file_path,
                    dependency_names=dependency_names,
                    display_path=rel_path,
                )
                all_usages.extend(file_usages)

    # Sort results by dependency name then file path then line number
    all_usages.sort(key=lambda u: (u.dependency_name.lower(), u.file_path, u.line_number))
    return all_usages


# Alias for backward compatibility
map_dependency_usage = map_usage


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyzer/usage_mapper.py <repo_path> [dep1,dep2,...]")
        sys.exit(1)

    target_repo = sys.argv[1]
    if len(sys.argv) > 2:
        raw_deps = sys.argv[2]
        deps_to_map = [d.strip() for d in raw_deps.split(",") if d.strip()]
    else:
        # Default fallback: discover dependencies from the target repo
        try:
            from scanner.discover import discover_dependencies

            discovered = discover_dependencies(target_repo)
            deps_to_map = list(dict.fromkeys(d.name for d in discovered))
        except Exception:
            deps_to_map = []

    usages = map_usage(target_repo, deps_to_map)
    print(f"Found {len(usages)} usages across {len(deps_to_map)} dependencies in '{target_repo}':")
    for u in usages:
        utype_str = u.usage_type.value if hasattr(u.usage_type, "value") else str(u.usage_type)
        print(f"  {u.dependency_name:15} | {u.file_path}:{u.line_number:<3} [{utype_str:14}] (evidence={u.evidence}) -> {u.code_snippet}")
