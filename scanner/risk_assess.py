"""Risk assessment module for dependencies.

Audits dependencies for vulnerabilities, unmaintained/deprecated packages,
EOL status, and licensing/incompatibility risks using PyPI metadata and pip-audit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure parent directory is in sys.path for standalone script execution
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

try:
    from schema import Dependency, RiskFlag, RiskSeverity, RiskType
except ImportError:
    from depexit.schema import Dependency, RiskFlag, RiskSeverity, RiskType

import requests
from packaging.specifiers import SpecifierSet


def check_pypi_metadata(
    package_name: str,
    declared_version: str = "",
    session: Optional[requests.Session] = None,
) -> list[RiskFlag]:
    """Query PyPI JSON API and assess maintenance, deprecation, and compatibility risks.

    Checks:
    1. Last PyPI release date (> 2 years without release -> UNMAINTAINED, MED)
    2. Deprecated/yanked status -> DEPRECATED, HIGH
    3. Python 3.12/3.13 compatibility via classifiers/requires_python -> INCOMPATIBLE, HIGH

    Args:
        package_name: Name of the PyPI package.
        declared_version: Declared version or specifier from manifest.
        session: Optional cached requests.Session.

    Returns:
        List of RiskFlag instances identified from PyPI metadata.
    """
    flags: list[RiskFlag] = []
    http = session or requests

    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        response = http.get(url, timeout=10)
    except Exception:
        return flags

    if response.status_code == 404:
        flags.append(
            RiskFlag(
                dependency_name=package_name,
                risk_type=RiskType.UNMAINTAINED,
                reason=f"Package '{package_name}' was not found on PyPI (HTTP 404)",
                severity=RiskSeverity.MED,
                evidence="observed",
            )
        )
        return flags

    if response.status_code != 200:
        return flags

    try:
        data = response.json()
    except Exception:
        return flags

    info = data.get("info", {})
    releases = data.get("releases", {})

    # -------------------------------------------------------------
    # 1. Check last release date (UNMAINTAINED if > 2 years)
    # -------------------------------------------------------------
    upload_times: list[str] = []
    for version_files in releases.values():
        if isinstance(version_files, list):
            for file_info in version_files:
                if isinstance(file_info, dict):
                    iso = file_info.get("upload_time_iso_8601") or file_info.get("upload_time")
                    if iso:
                        upload_times.append(iso)

    if not upload_times and data.get("urls"):
        for file_info in data.get("urls", []):
            if isinstance(file_info, dict):
                iso = file_info.get("upload_time_iso_8601") or file_info.get("upload_time")
                if iso:
                    upload_times.append(iso)

    if upload_times:
        try:
            latest_iso = max(upload_times)
            rel_dt = datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_since = (now - rel_dt).days
            if days_since > 730:  # 2 years
                years = days_since / 365.25
                flags.append(
                    RiskFlag(
                        dependency_name=package_name,
                        risk_type=RiskType.UNMAINTAINED,
                        reason=(
                            f"No PyPI release in {years:.1f} years "
                            f"(last release was on {rel_dt.strftime('%Y-%m-%d')})"
                        ),
                        severity=RiskSeverity.MED,
                        evidence="observed",
                    )
                )
        except Exception:
            pass

    # -------------------------------------------------------------
    # 2. Check deprecated / yanked status (DEPRECATED)
    # -------------------------------------------------------------
    is_yanked = bool(info.get("yanked"))
    yanked_reason = info.get("yanked_reason") or ""

    clean_version = declared_version.strip()
    if clean_version.startswith("=="):
        clean_version = clean_version[2:].strip()

    if clean_version and clean_version in releases:
        v_files = releases[clean_version]
        if isinstance(v_files, list) and any(f.get("yanked") for f in v_files if isinstance(f, dict)):
            is_yanked = True
            if not yanked_reason:
                yanked_reason = next(
                    (f.get("yanked_reason") for f in v_files if isinstance(f, dict) and f.get("yanked_reason")),
                    f"Version {clean_version} yanked",
                )

    classifiers = info.get("classifiers", []) or []
    is_inactive = "Development Status :: 7 - Inactive" in classifiers

    summary = (info.get("summary") or "").lower()
    description_head = (info.get("description") or "")[:400].lower()
    is_deprecated_text = (
        "[deprecated]" in summary
        or "deprecated" in summary
        or description_head.startswith("# deprecated")
        or description_head.startswith("deprecated")
        or "[deprecated]" in description_head
    )

    if is_yanked:
        flags.append(
            RiskFlag(
                dependency_name=package_name,
                risk_type=RiskType.DEPRECATED,
                reason=f"Package/release is yanked on PyPI: {yanked_reason or 'Marked yanked'}",
                severity=RiskSeverity.HIGH,
                evidence="observed",
            )
        )
    elif is_inactive:
        flags.append(
            RiskFlag(
                dependency_name=package_name,
                risk_type=RiskType.DEPRECATED,
                reason="Package marked as inactive on PyPI (Development Status :: 7 - Inactive)",
                severity=RiskSeverity.HIGH,
                evidence="observed",
            )
        )
    elif is_deprecated_text:
        flags.append(
            RiskFlag(
                dependency_name=package_name,
                risk_type=RiskType.DEPRECATED,
                reason=f"Package flagged as deprecated by maintainers: {info.get('summary') or 'Deprecated'}",
                severity=RiskSeverity.HIGH,
                evidence="observed",
            )
        )

    # -------------------------------------------------------------
    # 4. Check Python 3.12 / 3.13 compatibility (INCOMPATIBLE)
    # -------------------------------------------------------------
    # a) Evaluate specific Python 3 minor version classifiers
    py3_minor = [c for c in classifiers if re.match(r"^Programming Language :: Python :: 3\.\d+", c)]
    if py3_minor:
        has_312_or_313 = any("3.12" in c or "3.13" in c for c in py3_minor)
        if not has_312_or_313:
            max_ver_tag = sorted(
                py3_minor,
                key=lambda c: [int(x) for x in re.findall(r"\d+", c.split("::")[-1])],
            )[-1].split("::")[-1].strip()
            flags.append(
                RiskFlag(
                    dependency_name=package_name,
                    risk_type=RiskType.INCOMPATIBLE,
                    reason=(
                        f"PyPI classifiers indicate support only up to Python {max_ver_tag}, "
                        "missing Python 3.12/3.13 support"
                    ),
                    severity=RiskSeverity.HIGH,
                    evidence="observed",
                )
            )

    # b) Evaluate requires_python metadata constraint
    requires_python = info.get("requires_python")
    if requires_python:
        try:
            spec = SpecifierSet(requires_python)
            if "3.12" not in spec and "3.13" not in spec:
                flags.append(
                    RiskFlag(
                        dependency_name=package_name,
                        risk_type=RiskType.INCOMPATIBLE,
                        reason=f"requires_python constraint ('{requires_python}') excludes Python 3.12 and 3.13",
                        severity=RiskSeverity.HIGH,
                        evidence="observed",
                    )
                )
        except Exception:
            pass

    # c) Check Python 2 only packages
    has_py2 = any("Programming Language :: Python :: 2" in c for c in classifiers)
    has_py3 = any("Programming Language :: Python :: 3" in c for c in classifiers)
    if has_py2 and not has_py3:
        flags.append(
            RiskFlag(
                dependency_name=package_name,
                risk_type=RiskType.INCOMPATIBLE,
                reason="PyPI classifiers declare support only for Python 2",
                severity=RiskSeverity.HIGH,
                evidence="observed",
            )
        )

    return flags


def _audit_requirements_file(
    req_file_path: str,
    pkg_map: dict[str, str],
    global_seen_vulns: set[tuple[str, str]],
) -> list[RiskFlag]:
    """Helper to run pip-audit on a specific requirements file and parse results."""
    flags: list[RiskFlag] = []
    cmd = [sys.executable, "-m", "pip_audit", "-r", req_file_path, "--format", "json", "--no-deps"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, FileNotFoundError):
        audit_bin = shutil.which("pip-audit")
        if not audit_bin:
            return flags
        cmd = [audit_bin, "-r", req_file_path, "--format", "json", "--no-deps"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if not proc.stdout.strip():
        return flags

    try:
        data = json.loads(proc.stdout)
    except Exception:
        return flags

    for item in data.get("dependencies", []):
        raw_name = item.get("name", "")
        norm_name = raw_name.lower().replace("_", "-")
        dep_name = pkg_map.get(norm_name, raw_name)
        vulns = item.get("vulns", [])

        for vuln in vulns:
            vuln_id = vuln.get("id", "VULNERABILITY")
            vuln_key = (dep_name.lower(), vuln_id)
            if vuln_key in global_seen_vulns:
                continue
            global_seen_vulns.add(vuln_key)

            aliases = sorted(vuln.get("aliases", []))
            fix_versions = vuln.get("fix_versions", [])
            desc = vuln.get("description", "").split("\n")[0].strip()

            reason_parts = [f"Known vulnerability {vuln_id}"]
            if aliases:
                reason_parts.append(f"({', '.join(aliases)})")
            if fix_versions:
                reason_parts.append(f"[fixed in: {', '.join(fix_versions)}]")
            if desc:
                desc_snippet = desc[:150] + ("..." if len(desc) > 150 else "")
                reason_parts.append(f"- {desc_snippet}")

            flags.append(
                RiskFlag(
                    dependency_name=dep_name,
                    risk_type=RiskType.VULNERABLE,
                    reason=" ".join(reason_parts),
                    severity=RiskSeverity.HIGH,
                    evidence="observed",
                )
            )

    return flags


def run_pip_audit(dependencies: list[Dependency]) -> list[RiskFlag]:
    """Execute pip-audit in subprocess mode and parse vulnerability findings.

    Dependencies are grouped by manifest source_file to avoid cross-manifest
    dependency resolution conflicts when running pip-audit.

    Args:
        dependencies: Discovered dependencies to audit.

    Returns:
        List of RiskFlag objects with risk_type=VULNERABLE, severity=HIGH, evidence="observed".
    """
    flags: list[RiskFlag] = []
    if not dependencies:
        return flags

    by_source: dict[str, list[Dependency]] = {}
    for dep in dependencies:
        src = dep.source_file or "requirements.txt"
        by_source.setdefault(src, []).append(dep)

    global_seen_vulns: set[tuple[str, str]] = set()

    for src_file, dep_list in by_source.items():
        pkg_map: dict[str, str] = {}
        lines: list[str] = []

        for dep in dep_list:
            norm_key = dep.name.lower().replace("_", "-")
            pkg_map[norm_key] = dep.name
            ver = dep.declared_version.strip() if dep.declared_version else ""
            if ver:
                if any(ver.startswith(op) for op in ("==", ">=", "<=", "~=", "!=", "<", ">")):
                    lines.append(f"{dep.name}{ver}")
                else:
                    lines.append(f"{dep.name}=={ver}")
            else:
                lines.append(dep.name)

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf_path = tf.name
            tf.write("\n".join(lines) + "\n")

        try:
            batch_flags = _audit_requirements_file(tf_path, pkg_map, global_seen_vulns)
            flags.extend(batch_flags)
        finally:
            try:
                os.unlink(tf_path)
            except OSError:
                pass

    return flags


def assess_risk(dependencies: list[Dependency]) -> list[RiskFlag]:
    """Assess risks for a collection of dependencies.

    Checks each dependency for:
    1. Last PyPI release date (UNMAINTAINED if no release in 2+ years, severity MED)
    2. Deprecated/yanked status from PyPI API (DEPRECATED, severity HIGH)
    3. Known vulnerabilities via pip-audit subprocess (VULNERABLE, severity HIGH)
    4. Python 3.12/3.13 compatibility from PyPI classifiers/requires_python (INCOMPATIBLE, severity HIGH)

    Every RiskFlag sets evidence="observed" explicitly.

    Args:
        dependencies: Discovered dependencies to evaluate.

    Returns:
        A list of identified RiskFlag objects. Pure function, no printing.
    """
    if not dependencies:
        return []

    flags: list[RiskFlag] = []
    seen_flag_keys: set[tuple[str, RiskType, str, str]] = set()

    # 1. Run pip-audit for vulnerabilities (subprocess, JSON output)
    vuln_flags = run_pip_audit(dependencies)
    for flag in vuln_flags:
        key = (flag.dependency_name.lower(), flag.risk_type, flag.reason, flag.evidence)
        if key not in seen_flag_keys:
            seen_flag_keys.add(key)
            flags.append(flag)

    # 2. Check PyPI JSON API for release date, deprecation, and Python compatibility
    pypi_cache: dict[str, list[RiskFlag]] = {}
    session = requests.Session()

    for dep in dependencies:
        norm_name = dep.name.lower().replace("_", "-")
        cache_key = f"{norm_name}:{dep.declared_version}"

        if cache_key in pypi_cache:
            pypi_flags = pypi_cache[cache_key]
        else:
            pypi_flags = check_pypi_metadata(
                package_name=dep.name,
                declared_version=dep.declared_version,
                session=session,
            )
            pypi_cache[cache_key] = pypi_flags

        for flag in pypi_flags:
            key = (dep.name.lower(), flag.risk_type, flag.reason, flag.evidence)
            if key not in seen_flag_keys:
                seen_flag_keys.add(key)
                flags.append(
                    RiskFlag(
                        dependency_name=dep.name,
                        risk_type=flag.risk_type,
                        reason=flag.reason,
                        severity=flag.severity,
                        evidence="observed",
                    )
                )

    return flags


# Alias for backward compatibility
assess_risks = assess_risk


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    target_path = Path(target)

    if target_path.exists():
        from scanner.discover import discover_dependencies

        deps = discover_dependencies(target_path)
    else:
        # Fallback: treat positional arguments as package names
        pkg_names = sys.argv[1:]
        deps = [Dependency(name=p.strip(), declared_version="", source_file="manual") for p in pkg_names]

    risk_results = assess_risk(deps)
    print(f"Assessed {len(deps)} dependencies. Found {len(risk_results)} risk flags:")
    for rf in risk_results:
        print(f"  [{rf.severity.value:4}] {rf.risk_type.value:12} {rf.dependency_name:15} (evidence={rf.evidence}) -> {rf.reason}")
