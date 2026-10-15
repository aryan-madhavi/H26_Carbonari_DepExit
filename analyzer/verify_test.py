"""Test verification module for candidate dependency replacements.

Runs the target repository's test suite (e.g. pytest, unittest) against proposed
replacements or migration shims to verify whether compatibility regressions occur.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Optional, Union

# Ensure parent directory is in sys.path for standalone script execution
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

try:
    from schema import VerificationResult, VerificationStatus
except ImportError:
    from depexit.schema import VerificationResult, VerificationStatus

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
    "node_modules",
}

# Known package name to top-level import root mappings
KNOWN_IMPORT_MODULES: dict[str, str] = {
    "python-dateutil": "dateutil",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
    "typing-extensions": "typing_extensions",
    "protobuf": "google.protobuf",
    "pydantic-core": "pydantic_core",
    "pycrypto": "Crypto",
    "pycryptodome": "Crypto",
}


def _get_import_module_name(package_name: str) -> str:
    """Resolve the primary Python importable module name for a package."""
    clean = package_name.lower().strip()
    if clean in KNOWN_IMPORT_MODULES:
        return KNOWN_IMPORT_MODULES[clean]
    # Replace dashes and dots with underscores
    return clean.replace("-", "_").split("==")[0].split(">=")[0].split("<=")[0].strip()


def _is_actual_test_file(file_path: Path) -> bool:
    """Check if a Python file actually contains test cases or test definitions."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return (
            "def test_" in content
            or "TestCase" in content
            or "pytest." in content
            or "import pytest" in content
            or "class Test" in content
        )
    except Exception:
        return False


def detect_test_suite(repo_path: Union[str, Path]) -> tuple[bool, str, list[str]]:
    """Detect whether the repository has an existing test suite and return runner command.

    Args:
        repo_path: Path to the target repository.

    Returns:
        Tuple of (has_tests: bool, framework: str, command: list[str])
    """
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return False, "", []

    test_files: list[Path] = []
    has_test_dir = False

    # Check for conventional test directories
    for t_dir_name in ("tests", "test", "testing"):
        t_dir = repo / t_dir_name
        if t_dir.is_dir():
            for root, dirs, files in os.walk(t_dir):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
                for f in files:
                    if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py") or f == "tests.py"):
                        f_path = Path(root) / f
                        if _is_actual_test_file(f_path):
                            test_files.append(f_path)
            if test_files:
                has_test_dir = True
                break

    # Scan the rest of the repository for test files if not found in test dirs
    if not test_files:
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and d not in ("analyzer", "scanner", "ai", "report")]
            for f in files:
                if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                    f_path = Path(root) / f
                    if _is_actual_test_file(f_path):
                        test_files.append(f_path)

    # Check for pytest configuration files
    has_pytest_config = any(
        (repo / cfg).is_file()
        for cfg in ("pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini")
    )

    if not test_files and not has_pytest_config:
        return False, "", []


    # Check if pytest is available
    pytest_available = False
    try:
        check_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pytest_available = (check_proc.returncode == 0)
    except Exception:
        pytest_available = False

    if pytest_available:
        target_dir = "tests" if (repo / "tests").is_dir() else ("test" if (repo / "test").is_dir() else ".")
        return True, "pytest", [sys.executable, "-m", "pytest", "-q", target_dir]
    else:
        # Fall back to unittest discover
        target_dir = "tests" if (repo / "tests").is_dir() else ("test" if (repo / "test").is_dir() else ".")
        return True, "unittest", [sys.executable, "-m", "unittest", "discover", "-s", target_dir]


def execute_test_suite(
    repo_path: Union[str, Path],
    test_command: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 60,
    test_name: str = "test_suite",
    dependency_name: str = "",
) -> VerificationResult:
    """Execute a specified test command inside the target repository.

    Args:
        repo_path: Path to the target repository.
        test_command: Command list to run (defaults to detected test suite).
        env: Environment variables to pass to the subprocess.
        timeout: Subprocess execution timeout in seconds.
        test_name: Identifier for the test run.
        dependency_name: Name of the dependency being evaluated.

    Returns:
        VerificationResult containing test status and raw output.
    """
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=VerificationStatus.ERROR,
            raw_output=f"Target repository path does not exist: {repo}",
            verified=False,
        )

    if test_command is None:
        has_tests, framework, cmd = detect_test_suite(repo)
        if not has_tests:
            return VerificationResult(
                dependency_name=dependency_name,
                test_name=test_name,
                status=VerificationStatus.NOT_RUN,
                raw_output="No test suite discovered in target repository.",
                verified=False,
            )
        test_command = cmd

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    # Ensure repo_path is in PYTHONPATH so internal modules can be imported
    existing_pp = run_env.get("PYTHONPATH", "")
    run_env["PYTHONPATH"] = f"{repo}{os.pathsep}{existing_pp}" if existing_pp else str(repo)

    try:
        proc = subprocess.run(
            test_command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=run_env,
        )
        exit_code = proc.returncode
        status = VerificationStatus.PASS if exit_code == 0 else VerificationStatus.FAIL
        cmd_str = " ".join(str(c) for c in test_command)
        raw_output = (
            f"Command: {cmd_str}\n"
            f"Exit Code: {exit_code}\n"
            f"--- STDOUT ---\n{proc.stdout}\n"
            f"--- STDERR ---\n{proc.stderr}"
        )
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=status,
            raw_output=raw_output.strip(),
            verified=(status == VerificationStatus.PASS),
        )
    except subprocess.TimeoutExpired as exc:
        cmd_str = " ".join(str(c) for c in test_command)
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=VerificationStatus.ERROR,
            raw_output=(
                f"Command: {cmd_str}\n"
                f"Timed out after {timeout} seconds.\n"
                f"Partial STDOUT:\n{exc.stdout or ''}\n"
                f"Partial STDERR:\n{exc.stderr or ''}"
            ).strip(),
            verified=False,
        )
    except Exception as exc:
        cmd_str = " ".join(str(c) for c in test_command)
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=VerificationStatus.ERROR,
            raw_output=f"Command: {cmd_str}\nExecution failed with error: {exc}",
            verified=False,
        )


def _get_venv_python(venv_dir: Path) -> Path:
    """Get the python executable path inside a virtual environment."""
    bin_dir = venv_dir / "bin" if (venv_dir / "bin").exists() else venv_dir / "Scripts"
    py_path = bin_dir / "python"
    if not py_path.exists():
        py_path = bin_dir / "python.exe"
    return py_path


def _get_top_priority_dependencies(repo_path: Path, max_count: int = 2) -> list[str]:
    """Identify the top 1-2 highest-severity dependencies in the repository."""
    try:
        from scanner.discover import discover_dependencies
        from scanner.risk_assess import assess_risk

        deps = discover_dependencies(repo_path)
        if not deps:
            return []

        flags = assess_risk(deps)
        severity_score = {"HIGH": 3, "MED": 2, "LOW": 1}

        # Calculate max severity score per dependency
        dep_scores: dict[str, int] = {}
        for d in deps:
            dep_scores[d.name] = 0

        for f in flags:
            sev_str = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
            score = severity_score.get(sev_str.upper(), 0)
            dep_name = f.dependency_name
            if dep_name in dep_scores:
                dep_scores[dep_name] = max(dep_scores[dep_name], score)
            else:
                dep_scores[dep_name] = score

        # Sort dependencies by score descending, then by name
        sorted_deps = sorted(dep_scores.items(), key=lambda item: (-item[1], item[0].lower()))
        return [name for name, _ in sorted_deps[:max_count]]
    except Exception:
        # Fallback if discovery or assessment fails
        return []


def run_smoke_test(
    repo_path: Path,
    module_name: str,
    python_executable: Union[str, Path] = sys.executable,
    env: Optional[dict[str, str]] = None,
    timeout: int = 15,
) -> tuple[VerificationStatus, str]:
    """Execute a minimal smoke test by importing the specified module in a subprocess."""
    smoke_script = (
        "import sys\n"
        f"mod_name = '{module_name}'\n"
        "try:\n"
        "    mod = __import__(mod_name)\n"
        "    ver = getattr(mod, '__version__', getattr(mod, 'VERSION', 'unknown'))\n"
        "    print(f'SUCCESS: successfully imported {mod_name} (version: {ver})')\n"
        "    sys.exit(0)\n"
        "except Exception as e:\n"
        "    print(f'FAILURE: could not import {mod_name}: {e}', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    # Ensure repo_path is in PYTHONPATH
    existing_pp = run_env.get("PYTHONPATH", "")
    run_env["PYTHONPATH"] = f"{repo_path}{os.pathsep}{existing_pp}" if existing_pp else str(repo_path)

    try:
        proc = subprocess.run(
            [str(python_executable), "-c", smoke_script],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=run_env,
        )
        status = VerificationStatus.PASS if proc.returncode == 0 else VerificationStatus.FAIL
        output = f"Smoke Test ({module_name}): exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}".strip()
        return status, output
    except subprocess.TimeoutExpired as exc:
        return (
            VerificationStatus.ERROR,
            f"Smoke Test ({module_name}) timed out after {timeout} seconds.\n{exc.stdout or ''}\n{exc.stderr or ''}".strip(),
        )
    except Exception as exc:
        return VerificationStatus.ERROR, f"Smoke Test ({module_name}) failed to execute: {exc}"


def verify_replacement(
    repo_path: str,
    dependency_name: str,
    replacement_name: str,
    migration_steps: Optional[list[str]] = None,
    is_priority: Optional[bool] = None,
    code_patch: Optional[str] = None,
    suggested_tests: Optional[list[str]] = None,
) -> list[VerificationResult]:
    """Verify candidate replacement package against repository test suite or minimal smoke test.

    Workflow:
    1. Checks if repo has existing test suite (pytest/unittest) and runs baseline test suite.
    2. In an isolated sandbox/venv, installs candidate replacement package.
    3. Re-runs test suite (or minimal smoke test importing module if no tests exist).
    4. Compares before/after and returns structured VerificationResult list.

    Args:
        repo_path: Path to the target repository.
        dependency_name: Target dependency being replaced.
        replacement_name: Proposed replacement library name.
        migration_steps: Optional list of migration guidelines/steps.
        is_priority: Force priority smoke testing if no test suite exists. If None, checks top 1-2.
        code_patch: Optional AI-generated code patch string.
        suggested_tests: Optional list of AI-suggested test descriptions.

    Returns:
        List of VerificationResult objects detailing before, installation, and after results.
    """
    repo = Path(repo_path).resolve()
    results: list[VerificationResult] = []
    migration_steps = migration_steps or []

    if not repo.exists():
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name="setup",
                status=VerificationStatus.ERROR,
                raw_output=f"Repository path does not exist: {repo}",
                verified=False,
            )
        )
        return results

    if not replacement_name or replacement_name.strip().lower() in ("none", "n/a", ""):
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name="replacement_check",
                status=VerificationStatus.NOT_RUN,
                raw_output=f"No candidate replacement proposed for '{dependency_name}'.",
                verified=False,
            )
        )
        return results

    # Format migration steps description for context in raw outputs
    migration_summary = ""
    if migration_steps:
        migration_summary += "\nMigration Steps:\n" + "\n".join(f"  - {s}" for s in migration_steps)
    if code_patch:
        migration_summary += f"\nCode Patch Preview:\n{code_patch[:300]}"
    if suggested_tests:
        migration_summary += "\nSuggestedTests:\n" + "\n".join(f"  - {t}" for t in suggested_tests)

    has_test_suite, framework, test_cmd = detect_test_suite(repo)


    # -------------------------------------------------------------------------
    # Scenario A: Existing Test Suite Discovered (pytest / unittest)
    # -------------------------------------------------------------------------
    if has_test_suite:
        # Step 1: Run baseline test suite
        baseline_res = execute_test_suite(
            repo_path=repo,
            test_command=test_cmd,
            test_name=f"baseline_{framework}",
            dependency_name=dependency_name,
            timeout=60,
        )
        results.append(baseline_res)

        # Step 2: Set up isolated environment and install replacement package
        with tempfile.TemporaryDirectory(prefix="depexit_verify_") as tmp_dir:
            sandbox_dir = Path(tmp_dir)
            venv_dir = sandbox_dir / "venv"
            venv_py: Optional[Path] = None
            target_site_packages = sandbox_dir / "site-packages"

            # Attempt virtual environment creation with system site packages
            try:
                venv.create(venv_dir, system_site_packages=True, with_pip=True, clear=True)
                vpy = _get_venv_python(venv_dir)
                if vpy.exists():
                    venv_py = vpy
            except Exception:
                venv_py = None

            install_success = False
            install_cmd: list[str]

            if venv_py:
                install_cmd = [str(venv_py), "-m", "pip", "install", "--no-input", replacement_name]
            else:
                target_site_packages.mkdir(parents=True, exist_ok=True)
                install_cmd = [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--target",
                    str(target_site_packages),
                    replacement_name,
                ]

            try:
                install_proc = subprocess.run(
                    install_cmd,
                    cwd=str(sandbox_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                install_exit = install_proc.returncode
                install_status = VerificationStatus.PASS if install_exit == 0 else VerificationStatus.FAIL
                install_success = (install_status == VerificationStatus.PASS)

                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=f"install_{replacement_name}",
                        status=install_status,
                        raw_output=(
                            f"Command: {' '.join(install_cmd)}\n"
                            f"Exit Code: {install_exit}\n"
                            f"--- STDOUT ---\n{install_proc.stdout}\n"
                            f"--- STDERR ---\n{install_proc.stderr}{migration_summary}"
                        ).strip(),
                        verified=install_success,
                    )
                )
            except subprocess.TimeoutExpired as exc:
                install_success = False
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=f"install_{replacement_name}",
                        status=VerificationStatus.ERROR,
                        raw_output=f"Package installation of '{replacement_name}' timed out after 120s.\n{exc.stdout or ''}\n{exc.stderr or ''}",
                        verified=False,
                    )
                )
            except Exception as exc:
                install_success = False
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=f"install_{replacement_name}",
                        status=VerificationStatus.ERROR,
                        raw_output=f"Package installation of '{replacement_name}' failed with error: {exc}",
                        verified=False,
                    )
                )

            # Step 3: Re-run test suite against swapped environment
            if install_success:
                swapped_env = os.environ.copy()
                if venv_py:
                    # Run test command using venv's python
                    swapped_cmd = [str(venv_py), "-m"] + test_cmd[2:]
                else:
                    swapped_cmd = list(test_cmd)
                    existing_pp = swapped_env.get("PYTHONPATH", "")
                    swapped_env["PYTHONPATH"] = (
                        f"{target_site_packages}{os.pathsep}{existing_pp}"
                        if existing_pp
                        else str(target_site_packages)
                    )

                swapped_res = execute_test_suite(
                    repo_path=repo,
                    test_command=swapped_cmd,
                    env=swapped_env,
                    test_name=f"swapped_{framework}",
                    dependency_name=dependency_name,
                    timeout=60,
                )
                results.append(swapped_res)
            else:
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=f"swapped_{framework}",
                        status=VerificationStatus.ERROR,
                        raw_output=(
                            f"Cannot run swapped test suite: installation of replacement '{replacement_name}' "
                            f"failed during sandbox setup.{migration_summary}"
                        ).strip(),
                        verified=False,
                    )
                )

        return results

    # -------------------------------------------------------------------------
    # Scenario B: No Test Suite Found — Run minimal smoke test for top 1-2
    # -------------------------------------------------------------------------
    # Determine priority for smoke testing
    should_run_smoke = False
    if is_priority is True:
        should_run_smoke = True
    elif is_priority is False:
        should_run_smoke = False
    else:
        # Determine top 1-2 highest severity dependencies in the repo
        top_deps = _get_top_priority_dependencies(repo, max_count=2)
        if not top_deps or dependency_name in top_deps:
            should_run_smoke = True
        else:
            should_run_smoke = False

    if not should_run_smoke:
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name="smoke_test",
                status=VerificationStatus.NOT_RUN,
                raw_output=(
                    f"Untested: dependency '{dependency_name}' is not among top priority dependencies "
                    f"for smoke testing (limited time budget). No test suite exists in repo."
                ),
                verified=False,
            )
        )
        return results

    # Baseline smoke test: import original dependency
    orig_mod = _get_import_module_name(dependency_name)
    base_smoke_status, base_smoke_out = run_smoke_test(repo, orig_mod)
    results.append(
        VerificationResult(
            dependency_name=dependency_name,
            test_name="baseline_smoke_test",
            status=base_smoke_status,
            raw_output=base_smoke_out,
            verified=(base_smoke_status == VerificationStatus.PASS),
        )
    )

    # Isolated installation and swapped smoke test
    with tempfile.TemporaryDirectory(prefix="depexit_verify_") as tmp_dir:
        sandbox_dir = Path(tmp_dir)
        venv_dir = sandbox_dir / "venv"
        venv_py: Optional[Path] = None
        target_site_packages = sandbox_dir / "site-packages"

        try:
            venv.create(venv_dir, system_site_packages=True, with_pip=True, clear=True)
            vpy = _get_venv_python(venv_dir)
            if vpy.exists():
                venv_py = vpy
        except Exception:
            venv_py = None

        install_success = False
        if venv_py:
            install_cmd = [str(venv_py), "-m", "pip", "install", "--no-input", replacement_name]
        else:
            target_site_packages.mkdir(parents=True, exist_ok=True)
            install_cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--target",
                str(target_site_packages),
                replacement_name,
            ]

        try:
            install_proc = subprocess.run(
                install_cmd,
                cwd=str(sandbox_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            install_exit = install_proc.returncode
            install_status = VerificationStatus.PASS if install_exit == 0 else VerificationStatus.FAIL
            install_success = (install_status == VerificationStatus.PASS)

            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name=f"install_{replacement_name}",
                    status=install_status,
                    raw_output=(
                        f"Command: {' '.join(install_cmd)}\n"
                        f"Exit Code: {install_exit}\n"
                        f"--- STDOUT ---\n{install_proc.stdout}\n"
                        f"--- STDERR ---\n{install_proc.stderr}{migration_summary}"
                    ).strip(),
                    verified=install_success,
                )
            )
        except subprocess.TimeoutExpired as exc:
            install_success = False
            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name=f"install_{replacement_name}",
                    status=VerificationStatus.ERROR,
                    raw_output=f"Installation of '{replacement_name}' timed out after 120s.\n{exc.stdout or ''}\n{exc.stderr or ''}",
                    verified=False,
                )
            )
        except Exception as exc:
            install_success = False
            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name=f"install_{replacement_name}",
                    status=VerificationStatus.ERROR,
                    raw_output=f"Installation of '{replacement_name}' failed with error: {exc}",
                    verified=False,
                )
            )

        # Swapped smoke test: import replacement package in isolated environment
        if install_success:
            rep_mod = _get_import_module_name(replacement_name)
            smoke_env = os.environ.copy()
            runner_py = venv_py or sys.executable

            if not venv_py:
                existing_pp = smoke_env.get("PYTHONPATH", "")
                smoke_env["PYTHONPATH"] = (
                    f"{target_site_packages}{os.pathsep}{existing_pp}"
                    if existing_pp
                    else str(target_site_packages)
                )

            swap_smoke_status, swap_smoke_out = run_smoke_test(
                repo_path=repo,
                module_name=rep_mod,
                python_executable=runner_py,
                env=smoke_env,
                timeout=15,
            )
            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name="swapped_smoke_test",
                    status=swap_smoke_status,
                    raw_output=swap_smoke_out,
                    verified=(swap_smoke_status == VerificationStatus.PASS),
                )
            )
        else:
            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name="swapped_smoke_test",
                    status=VerificationStatus.ERROR,
                    raw_output=(
                        f"Cannot run swapped smoke test: installation of replacement '{replacement_name}' "
                        f"failed.{migration_summary}"
                    ).strip(),
                    verified=False,
                )
            )

    return results


# Backward compatibility alias
run_verification_tests = verify_replacement


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify dependency replacements against repository test suite or minimal smoke test."
    )
    parser.add_argument("repo_path", help="Path to target repository to verify")
    parser.add_argument(
        "dependency_name",
        nargs="?",
        default=None,
        help="Original dependency name to replace (e.g. requests)",
    )
    parser.add_argument(
        "replacement_name",
        nargs="?",
        default=None,
        help="Proposed replacement package name (e.g. httpx)",
    )
    parser.add_argument(
        "--steps",
        nargs="*",
        default=[],
        help="Optional migration steps or notes",
    )

    args = parser.parse_args()
    target_repo = Path(args.repo_path).resolve()

    if not target_repo.exists():
        print(f"Error: Target repository does not exist: {target_repo}", file=sys.stderr)
        sys.exit(1)

    raw_dep = args.dependency_name
    raw_rep = args.replacement_name

    dep_pairs: list[tuple[str, str]] = []

    # If dependency and replacement are not specified, discover from repo
    if not raw_dep:
        print(f"No specific dependency given for '{target_repo}'. Discovering top priorities...")
        try:
            from scanner.discover import discover_dependencies

            discovered = discover_dependencies(target_repo)
            if not discovered:
                print("No dependencies discovered in repository.")
                sys.exit(0)

            top_deps = _get_top_priority_dependencies(target_repo, max_count=2)
            candidates = top_deps if top_deps else [d.name for d in discovered[:2]]
            for c_dep in candidates:
                c_rep = "httpx" if "request" in c_dep.lower() else "urllib3"
                dep_pairs.append((c_dep, c_rep))
            print(f"Selected priority dependencies: {', '.join(d[0] for d in dep_pairs)}")
        except Exception as e:
            print(f"Failed to discover dependencies automatically: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # User specified dependency/dependencies
        if "," in raw_dep:
            dep_list = [d.strip() for d in raw_dep.split(",") if d.strip()]
            rep_list = [r.strip() for r in (raw_rep.split(",") if raw_rep else []) if r.strip()]
            for idx, d_item in enumerate(dep_list):
                r_item = rep_list[idx] if idx < len(rep_list) else ("httpx" if "request" in d_item.lower() else "urllib3")
                dep_pairs.append((d_item, r_item))
        else:
            rep_item = raw_rep or ("httpx" if "request" in raw_dep.lower() else "urllib3")
            dep_pairs.append((raw_dep.strip(), rep_item.strip()))

    all_results: list[VerificationResult] = []
    for d_name, r_name in dep_pairs:
        print(f"\n--- Running Verification for '{d_name}' -> '{r_name}' in '{target_repo}' ---")
        v_results = verify_replacement(
            repo_path=str(target_repo),
            dependency_name=d_name,
            replacement_name=r_name,
            migration_steps=args.steps,
            is_priority=True,
        )
        all_results.extend(v_results)

    print(f"\nTotal Verification Results: {len(all_results)}\n" + "=" * 60)
    for res in all_results:
        status_val = res.status.value if hasattr(res.status, "value") else str(res.status)
        v_flag = "PASS" if res.verified else "FAIL/UNVERIFIED"
        print(f"[{status_val:7}] Dep: {res.dependency_name:12} | Test: {res.test_name:<22} | Verified: {v_flag}")
        print("-" * 60)
        # Indent raw output preview
        for line in res.raw_output.splitlines()[:6]:
            print(f"    {line}")
        if len(res.raw_output.splitlines()) > 6:
            print("    ...")
        print("=" * 60)

