"""AI module for Gemma model integration and Dependency Exit Capsule construction."""

try:
    from ai.capsule_builder import build_exit_capsule
    from ai.gemma_client import GemmaClient
except ImportError:
    from depexit.ai.capsule_builder import build_exit_capsule
    from depexit.ai.gemma_client import GemmaClient

__all__ = ["GemmaClient", "build_exit_capsule"]
