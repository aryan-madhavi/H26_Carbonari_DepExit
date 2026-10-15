"""DepExit CLI entrypoint.

Analyzes a target Python repository for risky dependencies, maps their usage,
uses an AI model to propose replacements, verifies them with tests, and outputs
a structured Dependency Exit Capsule report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

try:
    from schema import ExitCapsule
except ImportError:
    from depexit.schema import ExitCapsule


def run_depexit(
    repo_path: str,
    output_path: Optional[str] = None,
    report_format: str = "markdown",
) -> list[ExitCapsule]:
    """Execute the end-to-end DepExit analysis and reporting pipeline.

    Args:
        repo_path: Path to the target Python repository to analyze.
        output_path: Optional destination file path for the output report.
        report_format: Format of the report ('markdown' or 'html').

    Returns:
        List of generated ExitCapsule objects.
    """
    # TODO: Step 1 - Discover dependencies via depexit.scanner.discover
    # TODO: Step 2 - Assess dependency risks via depexit.scanner.risk_assess
    # TODO: Step 3 - Map dependency usage via depexit.analyzer.usage_mapper
    # TODO: Step 4 - Generate AI replacement recommendations via depexit.ai.gemma_client
    # TODO: Step 5 - Run test verifications via depexit.analyzer.verify_test
    # TODO: Step 6 - Build Exit Capsules via depexit.ai.capsule_builder
    # TODO: Step 7 - Render structured report via depexit.report.render
    raise NotImplementedError("TODO: implement run_depexit pipeline")


def main() -> None:
    """Parse command-line arguments and run the DepExit CLI tool."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="depexit",
        description="DepExit: Analyze risky dependencies and generate Dependency Exit Capsules.",
    )
    parser.add_argument(
        "repo_path",
        type=str,
        help="Path to the target Python repository to analyze.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Path to save the generated Dependency Exit Capsule report.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "html"],
        default="markdown",
        help="Output report format (default: markdown).",
    )

    args = parser.parse_args()

    # TODO: Execute run_depexit with parsed arguments
    raise NotImplementedError("TODO: implement CLI orchestration in main()")


if __name__ == "__main__":
    main()
