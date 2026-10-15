"""AI module for Gemma model integration and Dependency Exit Capsule construction."""

try:
    from ai.capsule_builder import build_capsule, build_exit_capsule
    from ai.gemma_client import (
        MODEL_INFO,
        GemmaAPIError,
        GemmaClient,
        explain_gaps,
        get_recommendation,
    )
except ImportError:
    from depexit.ai.capsule_builder import build_capsule, build_exit_capsule
    from depexit.ai.gemma_client import (
        MODEL_INFO,
        GemmaAPIError,
        GemmaClient,
        explain_gaps,
        get_recommendation,
    )

__all__ = [
    "GemmaClient",
    "GemmaAPIError",
    "build_capsule",
    "build_exit_capsule",
    "MODEL_INFO",
    "get_recommendation",
    "explain_gaps",
]
