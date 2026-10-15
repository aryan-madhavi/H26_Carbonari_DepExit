"""Test verification module for candidate dependency replacements.

Executes baseline and replacement test suites (existing repo tests + AI-generated tests)
in an isolated sandbox to detect compatibility regressions and verify proposed migrations.
"""

from __future__ import annotations
import importlib.util

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

logger = logging.getLogger(__name__)

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

# Regex to match pytest test lines with status in verbose output
PYTEST_LINE_PATTERN = re.compile(
    r"^([^\s:]+(?:::[\w.\[\]-]+)+)\s+(PASSED|FAILED|ERROR|SKIPPED)\b",
    re.MULTILINE,
)

# Regex to match unittest verbose lines (e.g. test_foo (tests.test_bar.TestFoo) ... ok)
UNITTEST_LINE_PATTERN = re.compile(
    r"^([\w.]+)\s+\([^)]+\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)\b",
    re.MULTILINE,
)


def _find_repo_python(repo_path: Path) -> tuple[Path, Optional[Path]]:
    """Discover the virtual environment Python inside the target repository if present.

    Returns:
        (python_executable, venv_dir_or_None)
    """
    for vname in (".venv", "venv", "env"):
        vdir = repo_path / vname
        if vdir.is_dir():
            vpy = vdir / "bin" / "python"
            if not vpy.exists():
                vpy = vdir / "bin" / "python3"
            if not vpy.exists():
                vpy = vdir / "Scripts" / "python.exe"
            if vpy.exists():
                return vpy, vdir

    # Fall back to current process Python
    return Path(sys.executable), None


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

    # Check conventional test directories
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
                break

    # Scan repository if no conventional test directory was found
    if not test_files:
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and d not in ("analyzer", "scanner", "ai", "report")]
            for f in files:
                if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                    f_path = Path(root) / f
                    if _is_actual_test_file(f_path):
                        test_files.append(f_path)

    has_pytest_config = any(
        (repo / cfg).is_file()
        for cfg in ("pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini")
    )

    if not test_files and not has_pytest_config:
        return False, "", []

    python_exe, _ = _find_repo_python(repo)

    # Check if pytest is importable/executable in the discovered python environment
    pytest_available = False
    try:
        check_proc = subprocess.run(
            [str(python_exe), "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pytest_available = (check_proc.returncode == 0)
    except Exception:
        pytest_available = False

    target_dir = "tests" if (repo / "tests").is_dir() else ("test" if (repo / "test").is_dir() else ".")
    if pytest_available:
        return True, "pytest", [str(python_exe), "-m", "pytest", "-v", "--tb=short", target_dir]
    else:
        return True, "unittest", [str(python_exe), "-m", "unittest", "discover", "-v", "-s", target_dir]


def _clean_code_patch(code_patch: str) -> str:
    """Strip markdown code fences from a unified diff if present."""
    cleaned = code_patch.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _apply_code_patch(sandbox_repo: Path, patch_text: str) -> tuple[bool, str]:
    """Apply a unified diff code_patch to the sandbox repository.

    Returns:
        (success: bool, message_or_error: str)
    """
    patch_clean = _clean_code_patch(patch_text)
    if not patch_clean:
        return True, "Empty code patch, skipped application."

    patch_file = sandbox_repo / "depexit_migration.patch"
    patch_file.write_text(patch_clean + "\n", encoding="utf-8")

    # Initialize a temporary git repo in the sandbox to enable git apply
    try:
        if not (sandbox_repo / ".git").exists():
            subprocess.run(["git", "init"], cwd=str(sandbox_repo), capture_output=True, check=False)
            subprocess.run(["git", "config", "user.name", "DepExit"], cwd=str(sandbox_repo), capture_output=True, check=False)
            subprocess.run(["git", "config", "user.email", "depexit@example.com"], cwd=str(sandbox_repo), capture_output=True, check=False)
            subprocess.run(["git", "add", "-A"], cwd=str(sandbox_repo), capture_output=True, check=False)
            subprocess.run(["git", "commit", "-m", "baseline", "--allow-empty"], cwd=str(sandbox_repo), capture_output=True, check=False)

        # Try git apply with whitespace tolerance
        apply_res = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-v", str(patch_file.name)],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if apply_res.returncode == 0:
            return True, "Patch applied successfully via git apply."

        # Try git apply with 3way fallback
        apply_3way = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_file.name)],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if apply_3way.returncode == 0:
            return True, "Patch applied successfully via git apply --3way."

        # Try standard patch utility if git apply failed
        patch_cmd = subprocess.run(
            ["patch", "-p1", "-i", str(patch_file.name)],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if patch_cmd.returncode == 0:
            return True, "Patch applied successfully via patch -p1."

        patch_p0 = subprocess.run(
            ["patch", "-p0", "-i", str(patch_file.name)],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if patch_p0.returncode == 0:
            return True, "Patch applied successfully via patch -p0."

        err_msg = apply_res.stderr or apply_res.stdout or patch_cmd.stderr or "git apply and patch failed"
        return False, err_msg.strip()
    except Exception as exc:
        return False, f"Error executing patch application: {exc}"
    finally:
        if patch_file.exists():
            try:
                patch_file.unlink()
            except Exception:
                pass


def _extract_failure_traceback(full_output: str, test_identifier: str) -> str:
    """Extract relevant failure traceback or error block for a specific test from output."""
    short_name = test_identifier.split("::")[-1]
    # Look for pytest failure header: ________________ test_name ________________
    pattern = re.compile(
        rf"_{{5,}}\s+{re.escape(short_name)}\s+_{{5,}}(.*?)(?=(?:_{{5,}}|\Z|=+\s+(?:FAILURES|short test summary info)))",
        re.DOTALL,
    )
    match = pattern.search(full_output)
    if match:
        snippet = match.group(1).strip()
        if snippet:
            return snippet

    # Fallback to last 25 lines of output
    lines = full_output.splitlines()
    return "\n".join(lines[-25:]).strip() if len(lines) > 25 else full_output.strip()


def _run_test_suite_internal(
    repo_dir: Path,
    python_exe: Path,
    framework: str,
    env: dict[str, str],
    dependency_name: str,
    run_type: str,     # "BASELINE" or "REPLACEMENT"
    test_source: str,  # "REPO" or "AI_GENERATED"
    target_test_path: Optional[str] = None,
    timeout: int = 45,
) -> list[VerificationResult]:
    """Execute test command and parse per-test results into VerificationResult objects."""
    results: list[VerificationResult] = []

    if framework == "pytest":
        cmd = [str(python_exe), "-m", "pytest", "-v", "--tb=short"]
        if target_test_path:
            cmd.append(target_test_path)
        else:
            target_dir = "tests" if (repo_dir / "tests").is_dir() else ("test" if (repo_dir / "test").is_dir() else ".")
            cmd.append(target_dir)
    else:
        cmd = [str(python_exe), "-m", "unittest", "-v"]
        if target_test_path:
            p = Path(target_test_path)
            cmd.extend(["discover", "-v", "-s", str(p.parent), "-p", str(p.name)])
        else:
            target_dir = "tests" if (repo_dir / "tests").is_dir() else ("test" if (repo_dir / "test").is_dir() else ".")
            cmd.extend(["discover", "-v", "-s", target_dir])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        full_output = f"Command: {' '.join(cmd)}\nExit Code: {proc.returncode}\n{proc.stdout}\n{proc.stderr}".strip()

        # Parse per-test outputs
        if framework == "pytest":
            matches = PYTEST_LINE_PATTERN.findall(proc.stdout)
            for test_name_raw, status_str in matches:
                status = VerificationStatus.PASS if status_str == "PASSED" else (
                    VerificationStatus.NOT_RUN if status_str == "SKIPPED" else VerificationStatus.FAIL
                )
                raw_out = (
                    proc.stdout[:400]
                    if status == VerificationStatus.PASS
                    else _extract_failure_traceback(proc.stdout, test_name_raw)
                )
                clean_test_name = test_name_raw.split("::")[-1] if "test_ai_generated_" in test_name_raw else test_name_raw
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=clean_test_name,
                        status=status,
                        raw_output=raw_out,
                        verified=(status == VerificationStatus.PASS),
                        evidence="observed",
                        run_type=run_type,
                        test_source=test_source,
                    )
                )
        else:
            matches = UNITTEST_LINE_PATTERN.findall(proc.stderr + "\n" + proc.stdout)
            for test_name_raw, status_str in matches:
                status = VerificationStatus.PASS if status_str == "ok" else (
                    VerificationStatus.NOT_RUN if status_str == "skipped" else VerificationStatus.FAIL
                )
                raw_out = (
                    full_output[:400]
                    if status == VerificationStatus.PASS
                    else _extract_failure_traceback(full_output, test_name_raw)
                )
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=test_name_raw,
                        status=status,
                        raw_output=raw_out,
                        verified=(status == VerificationStatus.PASS),
                        evidence="observed",
                        run_type=run_type,
                        test_source=test_source,
                    )
                )

        # If no individual test matched regex (e.g. fatal syntax error or collection failure), record overall result
        if not results:
            overall_status = VerificationStatus.PASS if proc.returncode == 0 else VerificationStatus.FAIL
            results.append(
                VerificationResult(
                    dependency_name=dependency_name,
                    test_name=f"{test_source.lower()}_test_suite",
                    status=overall_status,
                    raw_output=full_output,
                    verified=(overall_status == VerificationStatus.PASS),
                    evidence="observed",
                    run_type=run_type,
                    test_source=test_source,
                )
            )

    except subprocess.TimeoutExpired as exc:
        timeout_out = (
            f"Command: {' '.join(cmd)}\n"
            f"Timed out after {timeout} seconds.\n"
            f"Partial STDOUT:\n{exc.stdout or ''}\n"
            f"Partial STDERR:\n{exc.stderr or ''}"
        ).strip()
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name=f"{test_source.lower()}_timeout",
                status=VerificationStatus.ERROR,
                raw_output=timeout_out,
                verified=False,
                evidence="observed",
                run_type=run_type,
                test_source=test_source,
            )
        )
    except Exception as exc:
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name=f"{test_source.lower()}_error",
                status=VerificationStatus.ERROR,
                raw_output=f"Command: {' '.join(cmd)}\nFailed to execute: {exc}",
                verified=False,
                evidence="observed",
                run_type=run_type,
                test_source=test_source,
            )
        )

    return results


def _build_suggested_tests_file(suggested_tests: list[str]) -> Optional[str]:
    """Write suggested AI test functions into a clean standalone pytest file."""
    if not suggested_tests:
        return None

    cleaned_snippets: list[str] = []
    for test_snippet in suggested_tests:
        cleaned = _clean_code_patch(test_snippet)
        if cleaned:
            cleaned_snippets.append(cleaned)

    if not cleaned_snippets:
        return None

    content = """# Auto-generated by DepExit AI verification engine
import sys
import os
import pytest

""" + "\n\n".join(cleaned_snippets) + "\n"

    tf = tempfile.NamedTemporaryFile(suffix="_test.py", prefix="test_ai_generated_", mode="w", delete=False)
    tf.write(content)
    tf.flush()
    tf.close()
    return tf.name


def is_stdlib_module(module_name: str) -> bool:
    """Check if a given module or package name belongs to the Python standard library."""
    if not module_name or not module_name.strip():
        return False
    clean = module_name.strip().split('.')[0]
    if hasattr(sys, 'stdlib_module_names') and clean in sys.stdlib_module_names:
        return True
    try:
        spec = importlib.util.find_spec(clean)
        if spec is not None:
            if spec.origin in (None, 'built-in', 'frozen'):
                return True
            origin = str(spec.origin)
            return 'site-packages' not in origin and 'dist-packages' not in origin
    except Exception:
        pass
    return False


def verify_replacement(
    repo_path: str,
    dependency_name: str,
    replacement_name: str,
    code_patch: str = "",
    suggested_tests: Optional[list[str]] = None,
    *,
    migration_steps: Optional[list[str]] = None,
    is_priority: Optional[bool] = None,
    timeout: int = 45,
    **kwargs,
) -> list[VerificationResult]:
    """Execute baseline and replacement verification runs for a risky dependency.

    Workflow:
    1. BASELINE run: run repo's existing test suite (pytest/unittest) -> run_type=BASELINE, test_source=REPO.
    2. Isolated sandbox copy: apply code_patch, install replacement library into isolated site-packages.
    3. REPLACEMENT run: re-run the same existing test suite -> run_type=REPLACEMENT, test_source=REPO.
    4. Suggested tests run: run AI-suggested tests against both baseline and replacement (test_source=AI_GENERATED).
    5. If no repo test suite found, skips steps 1 and 3 entirely, relying solely on AI_GENERATED tests.
    6. For skipped/unrun steps or errors, returns VerificationResult with status=NOT_RUN or ERROR and clear reason.

    Args:
        repo_path: Path to the target repository directory.
        dependency_name: Original risky dependency name (e.g. 'requests').
        replacement_name: Proposed replacement package name (e.g. 'httpx').
        code_patch: Unified diff code patch to apply to the sandbox copy.
        suggested_tests: Optional list of AI-generated test functions.
        migration_steps: Optional human-readable migration guidelines.
        is_priority: Flag indicating top priority dependency.
        timeout: Subprocess timeout in seconds per test run.

    Returns:
        List of VerificationResult objects detailing individual test results and statuses.
    """
    repo = Path(repo_path).resolve()
    results: list[VerificationResult] = []
    suggested_tests = suggested_tests or []

    if not repo.exists():
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name="setup",
                status=VerificationStatus.ERROR,
                raw_output=f"Repository path does not exist: {repo}",
                verified=False,
                evidence="observed",
                run_type="BASELINE",
                test_source="REPO",
            )
        )
        return results

    # Discover target repo's Python executable and virtual environment
    python_exe, venv_dir = _find_repo_python(repo)

    # Base environment: activate venv variables and set PYTHONPATH
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = f"{repo}{os.pathsep}{base_env.get('PYTHONPATH', '')}"
    if venv_dir:
        base_env["VIRTUAL_ENV"] = str(venv_dir)
        base_env["PATH"] = f"{venv_dir / 'bin'}{os.pathsep}{base_env.get('PATH', '')}"

    has_repo_tests, framework, _ = detect_test_suite(repo)

    # -------------------------------------------------------------------------
    # 1. BASELINE run: Existing repo test suite
    # -------------------------------------------------------------------------
    if has_repo_tests:
        repo_baseline = _run_test_suite_internal(
            repo_dir=repo,
            python_exe=python_exe,
            framework=framework,
            env=base_env,
            dependency_name=dependency_name,
            run_type="BASELINE",
            test_source="REPO",
            timeout=timeout,
        )
        results.extend(repo_baseline)

    # -------------------------------------------------------------------------
    # 2. BASELINE run: AI-generated suggested_tests
    # -------------------------------------------------------------------------
    ai_test_file = _build_suggested_tests_file(suggested_tests)
    if ai_test_file:
        try:
            ai_baseline = _run_test_suite_internal(
                repo_dir=repo,
                python_exe=python_exe,
                framework="pytest",
                env=base_env,
                dependency_name=dependency_name,
                run_type="BASELINE",
                test_source="AI_GENERATED",
                target_test_path=ai_test_file,
                timeout=timeout,
            )
            results.extend(ai_baseline)
        finally:
            if os.path.exists(ai_test_file):
                try:
                    os.unlink(ai_test_file)
                except Exception:
                    pass

    # If neither repo test suite nor AI suggested tests exist:
    if not has_repo_tests and not suggested_tests:
        results.append(
            VerificationResult(
                dependency_name=dependency_name,
                test_name="no_tests_discovered",
                status=VerificationStatus.NOT_RUN,
                raw_output="skipped: no existing test suite and no suggested tests provided",
                verified=False,
                evidence="observed",
                run_type="BASELINE",
                test_source="REPO",
            )
        )
        return results

    # -------------------------------------------------------------------------
    # 3. Isolated Sandbox Setup & Patch Application
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="depexit_verify_") as tmp_dir:
        sandbox_root = Path(tmp_dir)
        sandbox_repo = sandbox_root / "repo"
        sandbox_site_packages = sandbox_root / "site-packages"

        # Copy repository into sandbox, excluding virtualenvs and cache dirs
        shutil.copytree(
            repo,
            sandbox_repo,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".tox", "build", "dist"
            ),
        )

        # Apply code_patch if provided
        patch_applied = True
        if code_patch and code_patch.strip():
            success, patch_msg = _apply_code_patch(sandbox_repo, code_patch)
            if not success:
                patch_applied = False
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name="apply_patch",
                        status=VerificationStatus.NOT_RUN,
                        raw_output=f"skipped: patch failed to apply\n{patch_msg}",
                        verified=False,
                        evidence="observed",
                        run_type="REPLACEMENT",
                        test_source="REPO",
                    )
                )

        # Install replacement package into isolated site-packages folder
        install_success = True
        if replacement_name and replacement_name.strip().lower() not in ("none", "n/a", ""):
            # Check if replacement is standard library module
            if is_stdlib_module(replacement_name):
                logger.info(
                    "Replacement '%s' is a Python standard-library module. Skipping pip install.",
                    replacement_name,
                )
                results.append(
                    VerificationResult(
                        dependency_name=dependency_name,
                        test_name=f"install_{replacement_name}",
                        status=VerificationStatus.NOT_RUN,
                        raw_output="stdlib module, no install needed",
                        verified=False,
                        evidence="observed",
                        run_type="REPLACEMENT",
                        test_source="REPO",
                    )
                )
            else:
                sandbox_site_packages.mkdir(parents=True, exist_ok=True)
                install_cmd = [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--target",
                    str(sandbox_site_packages),
                    replacement_name,
                ]
                try:
                    install_proc = subprocess.run(
                        install_cmd,
                        cwd=str(sandbox_repo),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=60,
                    )
                    if install_proc.returncode != 0:
                        install_success = False
                        results.append(
                            VerificationResult(
                                dependency_name=dependency_name,
                                test_name=f"install_{replacement_name}",
                                status=VerificationStatus.ERROR,
                                raw_output=f"skipped: installation of replacement '{replacement_name}' failed (exit {install_proc.returncode})\n{install_proc.stderr}",
                                verified=False,
                                evidence="observed",
                                run_type="REPLACEMENT",
                                test_source="REPO",
                            )
                        )
                except subprocess.TimeoutExpired:
                    install_success = False
                    results.append(
                        VerificationResult(
                            dependency_name=dependency_name,
                            test_name=f"install_{replacement_name}",
                            status=VerificationStatus.ERROR,
                            raw_output=f"skipped: installation of replacement '{replacement_name}' timed out after 60s",
                            verified=False,
                            evidence="observed",
                            run_type="REPLACEMENT",
                            test_source="REPO",
                        )
                    )

        # If patch failed or install failed, replacement test runs cannot proceed
        if not patch_applied or not install_success:
            return results

        # Replacement environment: sandbox site-packages FIRST in PYTHONPATH so replacement takes precedence
        repl_env = base_env.copy()
        repl_env["PYTHONPATH"] = f"{sandbox_site_packages}{os.pathsep}{sandbox_repo}{os.pathsep}{repl_env.get('PYTHONPATH', '')}"

        # ---------------------------------------------------------------------
        # 4. REPLACEMENT run: Existing repo test suite on patched copy
        # ---------------------------------------------------------------------
        if has_repo_tests:
            repo_replacement = _run_test_suite_internal(
                repo_dir=sandbox_repo,
                python_exe=python_exe,
                framework=framework,
                env=repl_env,
                dependency_name=dependency_name,
                run_type="REPLACEMENT",
                test_source="REPO",
                timeout=timeout,
            )
            results.extend(repo_replacement)

        # ---------------------------------------------------------------------
        # 5. REPLACEMENT run: AI-generated suggested_tests on patched copy
        # ---------------------------------------------------------------------
        ai_repl_file = _build_suggested_tests_file(suggested_tests)
        if ai_repl_file:
            try:
                ai_replacement = _run_test_suite_internal(
                    repo_dir=sandbox_repo,
                    python_exe=python_exe,
                    framework="pytest",
                    env=repl_env,
                    dependency_name=dependency_name,
                    run_type="REPLACEMENT",
                    test_source="AI_GENERATED",
                    target_test_path=ai_repl_file,
                    timeout=timeout,
                )
                results.extend(ai_replacement)
            finally:
                if os.path.exists(ai_repl_file):
                    try:
                        os.unlink(ai_repl_file)
                    except Exception:
                        pass

    return results


# Backward compatibility aliases
run_verification_tests = verify_replacement


def execute_test_suite(
    repo_path: Union[str, Path],
    test_command: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 60,
    test_name: str = "test_suite",
    dependency_name: str = "",
) -> VerificationResult:
    """Execute a specified test command inside the target repository."""
    repo = Path(repo_path).resolve()
    python_exe, venv_dir = _find_repo_python(repo)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env["PYTHONPATH"] = f"{repo}{os.pathsep}{run_env.get('PYTHONPATH', '')}"
    if venv_dir:
        run_env["VIRTUAL_ENV"] = str(venv_dir)
        run_env["PATH"] = f"{venv_dir / 'bin'}{os.pathsep}{run_env.get('PATH', '')}"

    cmd = test_command or [str(python_exe), "-m", "pytest", "-q"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=run_env,
        )
        status = VerificationStatus.PASS if proc.returncode == 0 else VerificationStatus.FAIL
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=status,
            raw_output=f"{proc.stdout}\n{proc.stderr}".strip(),
            verified=(status == VerificationStatus.PASS),
            evidence="observed",
            run_type="BASELINE",
            test_source="REPO",
        )
    except Exception as exc:
        return VerificationResult(
            dependency_name=dependency_name,
            test_name=test_name,
            status=VerificationStatus.ERROR,
            raw_output=str(exc),
            verified=False,
            evidence="observed",
            run_type="BASELINE",
            test_source="REPO",
        )


# ---------------------------------------------------------------------------
# Standalone Execution & Verification
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify dependency replacements against repository test suite and AI-generated tests."
    )
    parser.add_argument("repo_path", nargs="?", default="../linkwatch", help="Path to target repository")
    parser.add_argument("dependency_name", nargs="?", default="python-dateutil", help="Dependency being replaced")
    parser.add_argument("replacement_name", nargs="?", default="pendulum", help="Proposed replacement library")
    args = parser.parse_args()

    target_repo = Path(args.repo_path).resolve()
    print("=" * 75)
    print("DepExit Verification Test Harness: Standalone Verification")
    print("=" * 75)
    print(f"Target Repository : {target_repo}")
    print(f"Dependency Target : {args.dependency_name}")
    print(f"Replacement Target: {args.replacement_name}")

    # Mock code patch migrating linkwatch/dates.py to datetime
    mock_patch = """--- a/linkwatch/dates.py
+++ b/linkwatch/dates.py
@@ -1,3 +1,4 @@
+import datetime
 from dateutil import parser
 
 
"""

    # Mock AI-generated pytest functions
    mock_suggested_tests = [
        "def test_date_parser_smoke():\n    import datetime\n    now = datetime.datetime.now()\n    assert now.year >= 2024\n",
        "def test_ai_suggested_failing_sample():\n    # Intentionally failing test to demonstrate fail capture\n    assert 1 == 2, 'Expected failure test for Person C gap analysis'\n",
    ]

    print("\n[*] Running verify_replacement()...")
    results = verify_replacement(
        repo_path=str(target_repo),
        dependency_name=args.dependency_name,
        replacement_name=args.replacement_name,
        code_patch=mock_patch,
        suggested_tests=mock_suggested_tests,
    )

    print(f"\n[+] Total Verification Results: {len(results)}")
    print("-" * 75)
    print(f"{'STATUS':<8} | {'RUN_TYPE':<12} | {'SOURCE':<13} | {'VERIFIED':<8} | TEST NAME")
    print("-" * 75)
    for r in results:
        status_val = r.status.value if hasattr(r.status, 'value') else str(r.status)
        v_flag = "TRUE" if r.verified else "FALSE"
        print(f"{status_val:<8} | {r.run_type:<12} | {r.test_source:<13} | {v_flag:<8} | {r.test_name}")

    # Display sample failure traceback if any
    failures = [r for r in results if r.status != VerificationStatus.PASS]
    if failures:
        print("\n[*] Sample Failure Capture (for Person C explain_gaps):")
        sample_fail = failures[0]
        print(f"Test: {sample_fail.test_name} ({sample_fail.run_type}/{sample_fail.test_source})")
        print("Traceback Preview:")
        for line in sample_fail.raw_output.splitlines()[:10]:
            print(f"  > {line}")

    print("\n" + "=" * 75)
    print("STANDALONE VERIFICATION COMPLETED SUCCESSFULLY!")
    print("=" * 75)
