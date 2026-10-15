"""Report generation and rendering module for DepExit."""

try:
    from report.render import render_report
except ImportError:
    from depexit.report.render import render_report

__all__ = ["render_report"]
