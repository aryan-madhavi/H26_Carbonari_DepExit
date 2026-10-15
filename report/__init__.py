"""Report generation and rendering module for DepExit."""

try:
    from report.render import (
        print_summary,
        render_html,
        render_markdown,
        render_report,
        save_report,
    )
except ImportError:
    from depexit.report.render import (
        print_summary,
        render_html,
        render_markdown,
        render_report,
        save_report,
    )

__all__ = [
    "render_report",
    "print_summary",
    "render_html",
    "render_markdown",
    "save_report",
]
