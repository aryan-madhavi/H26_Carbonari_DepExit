"""Report generation and rendering module for DepExit."""

try:
    from report.render import print_summary, render_html, render_report
except ImportError:
    from depexit.report.render import print_summary, render_html, render_report

__all__ = ["render_report", "render_html", "print_summary"]
