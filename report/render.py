"""Report renderer module for Dependency Exit Capsules.

Generates structured JSON and responsive HTML reports summarizing risks,
AST code usages, AI recommendations, and verification results, and prints
a terminal summary for demo presentations.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

try:
    from schema import ExitCapsule
except ImportError:
    from depexit.schema import ExitCapsule


def _serialize_capsule(capsule: ExitCapsule) -> dict[str, Any]:
    """Serialize an ExitCapsule dataclass into a JSON-serializable dictionary."""
    def _convert(obj: Any) -> Any:
        if isinstance(obj, Enum):
            return obj.value
        if is_dataclass(obj):
            d = asdict(obj)
            return {k: _convert(v) for k, v in d.items()}
        if isinstance(obj, list):
            return [_convert(item) for item in obj]
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        return obj

    data = _convert(capsule)
    # Ensure dynamically attached fields are also captured
    for extra_field in ("gap_explanations", "gap_explanations_evidence", "model_info", "verified"):
        if hasattr(capsule, extra_field):
            data[extra_field] = _convert(getattr(capsule, extra_field))
    return data


def render_html(capsules: list[ExitCapsule]) -> str:
    """Generate a responsive HTML report for all ExitCapsules."""
    capsules_data = [_serialize_capsule(c) for c in capsules]

    cards_html = []
    for c in capsules_data:
        dep = c.get("dependency", {})
        dep_name = dep.get("name", "Unknown")
        dep_ver = dep.get("declared_version", "unspecified")
        source_file = dep.get("source_file", "manifest")

        rec = c.get("ai_recommendation") or {}
        repl_name = rec.get("proposed_replacement", "None proposed")
        rec_verified = rec.get("verified", False) or c.get("verified", False)
        ver_badge = (
            '<span class="badge badge-pass">VERIFIED PASS</span>'
            if rec_verified
            else '<span class="badge badge-warn">UNVERIFIED</span>'
        )

        # Risk Flags
        risk_rows = []
        for r in c.get("risk_flags", []):
            sev = r.get("severity", "MED")
            sev_class = "badge-high" if sev == "HIGH" else ("badge-med" if sev == "MED" else "badge-low")
            risk_rows.append(
                f'<tr><td><span class="badge {sev_class}">{sev}</span></td>'
                f'<td><code>{r.get("risk_type", "")}</code></td>'
                f'<td>{r.get("reason", "")}</td></tr>'
            )
        risk_table = (
            f'<table class="table"><thead><tr><th>Severity</th><th>Type</th><th>Reason</th></tr></thead>'
            f'<tbody>{"".join(risk_rows)}</tbody></table>'
            if risk_rows
            else "<p>No risk flags recorded.</p>"
        )

        # Usage Locations
        usage_rows = []
        for u in c.get("usage_locations", [])[:8]:  # Preview up to 8
            usage_rows.append(
                f'<tr><td><code>{u.get("file_path", "")}:{u.get("line_number", "")}</code></td>'
                f'<td><span class="badge badge-info">{u.get("usage_type", "")}</span></td>'
                f'<td><pre class="snippet">{u.get("code_snippet", "")}</pre></td></tr>'
            )
        usage_table = (
            f'<table class="table"><thead><tr><th>Location</th><th>Type</th><th>Snippet</th></tr></thead>'
            f'<tbody>{"".join(usage_rows)}</tbody></table>'
            if usage_rows
            else "<p>No source code usages found.</p>"
        )

        # Verification Results
        ver_rows = []
        for v in c.get("verification_results", []):
            st = v.get("status", "NOT_RUN")
            st_class = "badge-pass" if st == "PASS" else ("badge-fail" if st in ("FAIL", "ERROR") else "badge-warn")
            ver_rows.append(
                f'<tr><td><strong>{v.get("test_name", "")}</strong></td>'
                f'<td><span class="badge {st_class}">{st}</span></td>'
                f'<td><pre class="raw-output">{v.get("raw_output", "")[:200]}</pre></td></tr>'
            )
        ver_table = (
            f'<table class="table"><thead><tr><th>Test</th><th>Status</th><th>Output Preview</th></tr></thead>'
            f'<tbody>{"".join(ver_rows)}</tbody></table>'
            if ver_rows
            else "<p>No verification tests executed.</p>"
        )

        # Open Concerns & Gaps
        concerns_html = "".join(f"<li>⚠️ {con}</li>" for con in c.get("open_concerns", []))
        gaps_html = "".join(f"<li>🔍 {gap}</li>" for gap in c.get("compatibility_gaps", []))
        explanations_html = "".join(f"<li>💡 {exp}</li>" for exp in c.get("gap_explanations", []))

        # Migration Steps
        steps_html = "".join(f"<li>{s}</li>" for s in rec.get("migration_steps", []))

        card = f"""
        <div class="card">
            <div class="card-header">
                <div>
                    <h2>{dep_name} <span class="version">v{dep_ver}</span></h2>
                    <span class="source-file">Discovered in: <code>{source_file}</code></span>
                </div>
                <div>{ver_badge}</div>
            </div>
            <div class="card-body">
                <div class="rec-banner">
                    <strong>Proposed Replacement:</strong> <code class="replacement">{repl_name}</code>
                    <p class="reasoning"><em>{rec.get("reasoning", "")}</em></p>
                </div>

                <h3>Identified Risk Flags</h3>
                {risk_table}

                <h3>Code Usages ({len(c.get("usage_locations", []))} total)</h3>
                {usage_table}

                <h3>Verification Results</h3>
                {ver_table}

                {f"<h3>Migration Steps</h3><ol>{steps_html}</ol>" if steps_html else ""}
                {f"<h3>Compatibility Gaps</h3><ul>{gaps_html}</ul>" if gaps_html else ""}
                {f"<h3>AI Gap Explanations</h3><ul>{explanations_html}</ul>" if explanations_html else ""}
                {f"<h3>Open Concerns</h3><ul class='concerns-list'>{concerns_html}</ul>" if concerns_html else ""}
            </div>
        </div>
        """
        cards_html.append(card)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DepExit - Dependency Exit Capsules Report</title>
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-color: #f8fafc;
            --text-muted: #94a3b8;
            --border-color: #334155;
            --accent-color: #38bdf8;
            --pass-color: #22c55e;
            --fail-color: #ef4444;
            --warn-color: #f59e0b;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 2rem 1rem;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
        }}
        header {{
            margin-bottom: 2.5rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1.5rem;
        }}
        h1 {{
            font-size: 2.2rem;
            margin: 0 0 0.5rem 0;
            color: var(--accent-color);
        }}
        .lead {{
            color: var(--text-muted);
            font-size: 1.1rem;
            margin: 0;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            margin-bottom: 2rem;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.25rem 1.5rem;
            background: rgba(255, 255, 255, 0.02);
            border-bottom: 1px solid var(--border-color);
        }}
        .card-header h2 {{
            margin: 0;
            font-size: 1.4rem;
        }}
        .version {{
            color: var(--text-muted);
            font-size: 0.95rem;
            font-weight: normal;
        }}
        .source-file {{
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        .card-body {{
            padding: 1.5rem;
        }}
        .rec-banner {{
            background: rgba(56, 189, 248, 0.1);
            border-left: 4px solid var(--accent-color);
            padding: 1rem;
            border-radius: 4px;
            margin-bottom: 1.5rem;
        }}
        .replacement {{
            font-size: 1.15rem;
            color: var(--accent-color);
            font-weight: bold;
        }}
        .reasoning {{
            margin: 0.5rem 0 0 0;
            color: #cbd5e1;
        }}
        h3 {{
            font-size: 1.1rem;
            margin: 1.5rem 0 0.75rem 0;
            color: #e2e8f0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 0.3rem;
        }}
        .badge {{
            display: inline-block;
            padding: 0.25rem 0.6rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }}
        .badge-high {{ background: #7f1d1d; color: #fecaca; }}
        .badge-med {{ background: #78350f; color: #fef3c7; }}
        .badge-low {{ background: #1e3a5f; color: #bfdbfe; }}
        .badge-pass {{ background: #14532d; color: #bbf7d0; }}
        .badge-fail {{ background: #7f1d1d; color: #fecaca; }}
        .badge-warn {{ background: #78350f; color: #fde68a; }}
        .badge-info {{ background: #1e293b; color: #94a3b8; border: 1px solid var(--border-color); }}
        .table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
            margin-bottom: 1rem;
        }}
        .table th, .table td {{
            text-align: left;
            padding: 0.6rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }}
        .table th {{
            color: var(--text-muted);
            font-weight: 600;
        }}
        code, pre {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }}
        .snippet, .raw-output {{
            background: #090d16;
            padding: 0.4rem;
            border-radius: 4px;
            font-size: 0.8rem;
            overflow-x: auto;
            margin: 0;
        }}
        .concerns-list li {{
            color: #fde68a;
            margin-bottom: 0.3rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 DepExit Capsule Report</h1>
            <p class="lead">Automated Dependency Risk Assessment, Usage Mapping & Migration Verification</p>
        </header>
        <main>
            {"".join(cards_html) if cards_html else "<p>No risky dependencies flagged or analyzed.</p>"}
        </main>
    </div>
</body>
</html>
"""


def render_report(
    capsules: list[ExitCapsule],
    output_path: Optional[Union[str, Path]] = None,
    format: str = "all",
) -> str:
    """Render ExitCapsules into structured JSON and HTML format files.

    Args:
        capsules: List of ExitCapsule instances.
        output_path: Target directory or file path. If None, defaults to 'output'.
        format: Format to render ('json', 'html', or 'all').

    Returns:
        Rendered content string or path description.
    """
    target = Path(output_path) if output_path else Path("output")
    
    # Determine directory vs file
    if target.suffix in (".json", ".html", ".md"):
        out_dir = target.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".json" or format == "json":
            json_data = [_serialize_capsule(c) for c in capsules]
            content = json.dumps(json_data, indent=2)
            target.write_text(content, encoding="utf-8")
            return content
        elif target.suffix == ".html" or format == "html":
            content = render_html(capsules)
            target.write_text(content, encoding="utf-8")
            return content
    else:
        out_dir = target
        out_dir.mkdir(parents=True, exist_ok=True)

    # Output both JSON and HTML
    json_path = out_dir / "capsule.json"
    html_path = out_dir / "capsule.html"

    json_data = [_serialize_capsule(c) for c in capsules]
    json_content = json.dumps(json_data, indent=2)
    json_path.write_text(json_content, encoding="utf-8")

    html_content = render_html(capsules)
    html_path.write_text(html_content, encoding="utf-8")

    return html_content


def print_summary(capsules: list[ExitCapsule]) -> None:
    """Print a clean, structured summary table to stdout for terminal demo."""
    print("\n" + "=" * 80)
    print("🚀 DepExit Pipeline Summary: Dependency Exit Capsules")
    print("=" * 80)
    print(f"{'Dependency':<16} {'Severity':<9} {'Replacement':<16} {'Verified':<10} {'Tests':<6} {'Usages':<7}")
    print("-" * 80)

    for c in capsules:
        dep_name = c.dependency.name
        # Highest severity from risk flags
        sev = "MED"
        for rf in c.risk_flags:
            s_val = rf.severity.value if hasattr(rf.severity, "value") else str(rf.severity)
            if s_val == "HIGH":
                sev = "HIGH"
                break

        repl = c.ai_recommendation.proposed_replacement if c.ai_recommendation else "None"
        v_flag = "PASS" if getattr(c, "verified", False) else "UNVERIFIED"
        num_tests = len(c.verification_results)
        num_usages = len(c.usage_locations)

        print(f"{dep_name:<16} {sev:<9} {repl:<16} {v_flag:<10} {num_tests:<6} {num_usages:<7}")

    print("=" * 80)
    print("Outputs generated:")
    print("  📄 JSON Report: output/capsule.json")
    print("  🌐 HTML Report: output/capsule.html")
    print("=" * 80 + "\n")
