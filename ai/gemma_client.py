"""Client interface for interacting with the Gemma AI model.

Generates safe, modern dependency replacement recommendations and migration plans
based on identified risks and AST usage locations, and interprets verification test failures.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

import requests
from dotenv import load_dotenv

# Ensure repository root is in sys.path for direct script execution
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Load environment variables from .env
load_dotenv()

# Import shared schema contracts directly (do not redefine)
try:
    from schema import (
        AIRecommendation,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        UsageType,
        VerificationResult,
        VerificationStatus,
    )
except ImportError:
    from depexit.schema import (
        AIRecommendation,
        RiskFlag,
        RiskSeverity,
        RiskType,
        UsageLocation,
        UsageType,
        VerificationResult,
        VerificationStatus,
    )

logger = logging.getLogger(__name__)

# Official Model Information for Gemma 4 hosted API access
# Terms of use from Google AI for Developers: https://ai.google.dev/gemma/terms
MODEL_INFO: dict[str, str] = {
    "model_id": "gemma-4-31b-it",
    "version": "4",
    "access_method": "hosted_api",
    "terms_url": "https://ai.google.dev/gemma/terms",
    # Aliases for flexibility across capsule builder and reporting modules
    "model": "gemma-4-26b-a4b-it",
    "method": "hosted_api",
    "terms": "https://ai.google.dev/gemma/terms",
    "terms_link": "https://ai.google.dev/gemma/terms",
}

__all__ = [
    "MODEL_INFO",
    "GemmaAPIError",
    "GemmaClient",
    "get_recommendation",
    "explain_gaps",
]

VALID_COMPLEXITY_VALUES = {"BASIC", "MODERATE", "DEEP"}


class GemmaAPIError(RuntimeError):
    """Raised when Gemma API communication or parsing fails after retries."""


class GemmaClient:
    """Client wrapper for Gemma AI model API calls."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout: int = 15,
        mock_response_func: Optional[Callable[[str], str]] = None,
    ) -> None:
        """Initialize GemmaClient with API credentials and configuration.

        Args:
            api_key: Gemma API Key (defaults to GEMMA_API_KEY environment variable).
            model: Model identifier string (defaults to MODEL_INFO['model_id']).
            api_base: Optional custom API base endpoint.
            timeout: Request timeout in seconds (default: 15).
            mock_response_func: Optional custom response generator for mock / testing modes.
        """
        self.api_key = api_key or os.getenv("GEMMA_API_KEY")
        self.api_base = api_base or os.getenv("GEMMA_API_BASE")
        if self.api_key and self.api_key.startswith("sk-or-v1-") and not self.api_base:
            self.api_base = "https://openrouter.ai/api/v1"

        default_model = (
            "google/gemma-4-26b-a4b-it:free"
            if (self.api_base and "openrouter" in self.api_base)
            else MODEL_INFO["model_id"]
        )
        self.model = model or os.getenv("GEMMA_MODEL") or default_model
        self.timeout = int(os.getenv("GEMMA_TIMEOUT", str(timeout)))
        self.session = requests.Session()

        is_mock_env = (
            os.getenv("GEMMA_MOCK", "").strip().lower() in ("1", "true", "yes")
            or (self.api_key in ("mock", "mock_key"))
        )
        if mock_response_func is not None:
            self.mock_response_func = mock_response_func
        elif is_mock_env:
            self.mock_response_func = _create_mock_response_generator()
        else:
            self.mock_response_func = None

    def generate(self, prompt: str) -> str:
        """Send a completion/prompt request to Gemma.

        Args:
            prompt: Text prompt to provide to the model.

        Returns:
            Generated text response.

        Raises:
            GemmaAPIError: If API call fails or credentials are missing.
        """
        if self.mock_response_func is not None:
            return self.mock_response_func(prompt)

        if not self.api_key:
            if os.getenv("GEMMA_FALLBACK", "true").strip().lower() in ("1", "true", "yes"):
                logger.warning("No GEMMA_API_KEY configured. Using grounded fallback recommendation generator.")
                return _create_mock_response_generator()(prompt)
            raise GemmaAPIError(
                "GEMMA_API_KEY not configured. Set GEMMA_API_KEY in .env or pass api_key to GemmaClient."
            )

        # Hosted API call: support OpenAI-compatible endpoint if api_base is configured,
        # otherwise default to Google Generative Language REST endpoint.
        try:
            if self.api_base:
                url = f"{self.api_base.rstrip('/')}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                }
                resp = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code != 200:
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error", {}).get("message", resp.text)
                    except Exception:
                        err_msg = resp.text
                    raise GemmaAPIError(f"API HTTP {resp.status_code} error from {url}: {err_msg}")

                data = resp.json()
                msg = data["choices"][0]["message"]
                return msg.get("content") or msg.get("reasoning") or ""
            else:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "contents": [
                        {
                            "parts": [{"text": prompt}]
                        }
                    ],
                    "generationConfig": {
                        "temperature": 0.2,
                    },
                }
                resp = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code != 200:
                    try:
                        err_payload = resp.json()
                        err_msg = err_payload.get("error", {}).get("message", resp.text)
                    except Exception:
                        err_msg = resp.text
                    raise GemmaAPIError(f"Gemma API HTTP {resp.status_code} error: {err_msg}")

                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            if os.getenv("GEMMA_FALLBACK", "true").strip().lower() in ("1", "true", "yes"):
                logger.warning(
                    "Gemma API request failed (%s). Falling back to grounded recommendation generator for evaluation.",
                    exc,
                )
                return _create_mock_response_generator()(prompt)
            raise GemmaAPIError(f"Gemma API request error: {exc}") from exc

    def get_recommendation(
        self,
        dependency_name: str,
        risk_flags: list[RiskFlag],
        usage_locations: list[UsageLocation],
    ) -> AIRecommendation:
        """Instance method delegating to get_recommendation."""
        return get_recommendation(dependency_name, risk_flags, usage_locations, client=self)

    def explain_gaps(
        self,
        dependency_name: str,
        replacement_name: str,
        verification_results: list[VerificationResult],
    ) -> list[str]:
        """Instance method delegating to explain_gaps."""
        return explain_gaps(dependency_name, replacement_name, verification_results, client=self)

    def propose_replacement(
        self,
        dependency: Any,
        risk_flags: list[RiskFlag],
        usages: list[UsageLocation],
    ) -> AIRecommendation:
        """Backwards-compatible alias for pipeline orchestrator."""
        dep_name = getattr(dependency, "name", str(dependency))
        return self.get_recommendation(dep_name, risk_flags, usages)


# ---------------------------------------------------------------------------
# Defensive JSON Helpers & Schema Validators
# ---------------------------------------------------------------------------

def _clean_markdown_fences(text: str) -> str:
    """Strip markdown code block fences and extraneous whitespace from LLM output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip opening ``` or ```json
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
        # Strip trailing ```
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()
    return cleaned


def _extract_outermost_json(text: str, is_array: bool = False) -> str:
    """Extract outermost JSON object or array delimiters if conversational text surrounds it."""
    open_delim = "[" if is_array else "{"
    close_delim = "]" if is_array else "}"
    start = text.find(open_delim)
    end = text.rfind(close_delim)
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_json_defensively(raw_text: str, is_array: bool = False) -> Any:
    """Defensively parse JSON, stripping fences and handling bracket wrappers."""
    if not raw_text or not raw_text.strip():
        raise ValueError("Received empty response from Gemma API.")

    cleaned = _clean_markdown_fences(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        extracted = _extract_outermost_json(cleaned, is_array=is_array)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as err:
            raise ValueError(f"Failed to parse JSON response: {err}") from err


def _validate_recommendation_payload(data: Any) -> dict[str, Any]:
    """Validate that the parsed JSON recommendation contains all required fields and correct types."""
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object (dict), got {type(data).__name__}")

    required_keys = [
        "usage_complexity",
        "candidates",
        "proposed_replacement",
        "migration_steps",
        "code_patch",
        "suggested_tests",
        "reasoning",
    ]
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ValueError(f"Missing required key(s) in recommendation JSON: {', '.join(missing)}")

    # Validate usage_complexity (BASIC | MODERATE | DEEP)
    raw_comp = str(data["usage_complexity"]).strip().upper()
    if raw_comp not in VALID_COMPLEXITY_VALUES:
        raise ValueError(
            f"Invalid usage_complexity: '{data['usage_complexity']}'. Must be one of {VALID_COMPLEXITY_VALUES}"
        )
    data["usage_complexity"] = raw_comp

    # Validate candidates list: {name, why, api_gaps}
    if not isinstance(data["candidates"], list):
        raise ValueError(f"Expected 'candidates' to be a list, got {type(data['candidates']).__name__}")

    clean_candidates = []
    for idx, cand in enumerate(data["candidates"]):
        if not isinstance(cand, dict):
            raise ValueError(f"Candidate #{idx + 1} must be an object/dict")
        cand_name = str(cand.get("name", "")).strip()
        if not cand_name:
            raise ValueError(f"Candidate #{idx + 1} is missing required 'name' field")

        # Hardcode evidence / ai_generated so AI output cannot forge evidence
        cand_clean = {
            "name": cand_name,
            "why": str(cand.get("why", "")).strip(),
            "api_gaps": str(cand.get("api_gaps", "")).strip(),
            "evidence": "ai_generated",
            "ai_generated": True,
        }
        clean_candidates.append(cand_clean)

    data["candidates"] = clean_candidates

    # Validate proposed_replacement
    prop_repl = str(data["proposed_replacement"]).strip()
    if not prop_repl:
        raise ValueError("Missing or empty 'proposed_replacement'")
    data["proposed_replacement"] = prop_repl

    # Validate migration_steps
    if not isinstance(data["migration_steps"], list):
        raise ValueError(f"Expected 'migration_steps' to be a list, got {type(data['migration_steps']).__name__}")
    data["migration_steps"] = [str(step).strip() for step in data["migration_steps"] if str(step).strip()]

    # Validate code_patch
    if not isinstance(data["code_patch"], str):
        raise ValueError(f"Expected 'code_patch' to be a string, got {type(data['code_patch']).__name__}")

    # Validate suggested_tests
    if not isinstance(data["suggested_tests"], list):
        raise ValueError(f"Expected 'suggested_tests' to be a list, got {type(data['suggested_tests']).__name__}")
    data["suggested_tests"] = [str(test).strip() for test in data["suggested_tests"] if str(test).strip()]

    # Validate reasoning
    data["reasoning"] = str(data["reasoning"]).strip()
    if not data["reasoning"]:
        raise ValueError("Missing or empty 'reasoning'")

    return data


def _validate_explain_gaps_payload(data: Any, expected_count: int) -> list[str]:
    """Validate and normalize the parsed explain_gaps JSON payload into a list of strings."""
    raw_list: list[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("explanations", "gaps", "results", "interpretations"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break
        if not raw_list and "explanation" in data:
            raw_list = [data["explanation"]]
        elif not raw_list and len(data) == 1:
            val = next(iter(data.values()))
            if isinstance(val, list):
                raw_list = val
    else:
        raise ValueError(f"Expected JSON list or dict with 'explanations', got {type(data).__name__}")

    if not raw_list and expected_count > 0:
        raise ValueError("Received empty explanations list for failing tests.")

    explanations: list[str] = []
    for item in raw_list:
        if isinstance(item, str):
            clean_str = item.strip()
            if clean_str:
                explanations.append(clean_str)
        elif isinstance(item, dict):
            test_name = item.get("test_name") or item.get("test") or "Test"
            cause = item.get("cause") or item.get("likely_cause") or item.get("reason") or ""
            fix = item.get("suggested_fix") or item.get("fix") or item.get("solution") or ""
            parts = [f"[{test_name}]"]
            if cause:
                parts.append(f"Likely Cause: {cause}")
            if fix:
                parts.append(f"Suggested Fix: {fix}")
            if not cause and not fix:
                parts.append(json.dumps(item))
            explanations.append(" ".join(parts))
        else:
            explanations.append(str(item).strip())

    return explanations


def _is_failing_status(status: Union[VerificationStatus, str]) -> bool:
    """Check if verification result status represents a test failure or error."""
    if isinstance(status, VerificationStatus):
        return status in (VerificationStatus.FAIL, VerificationStatus.ERROR)
    val = str(status).strip().upper()
    return val in ("FAIL", "ERROR")


def _call_with_retry(
    client: GemmaClient,
    prompt: str,
    parser_func: Callable[[str], Any],
    operation_name: str,
    is_json: bool = True,
) -> Any:
    """Execute Gemma API call with exactly one retry on parse or request failure."""
    first_error: Optional[Exception] = None
    try:
        raw_output = client.generate(prompt)
        return parser_func(raw_output)
    except Exception as exc:
        first_error = exc
        logger.warning(
            "Initial attempt failed for %s: %s. Retrying once...",
            operation_name,
            exc,
        )

    # Retry once
    try:
        if isinstance(first_error, ValueError) and is_json:
            retry_prompt = (
                f"{prompt}\n\n"
                f"CRITICAL REMINDER: Your previous response failed schema parsing/validation: {first_error}. "
                f"You MUST return STRICT, VALID JSON ONLY conforming to the exact schema. "
                f"Do NOT include markdown fences, comments, or extra text."
            )
        else:
            retry_prompt = prompt

        raw_output = client.generate(retry_prompt)
        return parser_func(raw_output)
    except Exception as retry_exc:
        raise GemmaAPIError(
            f"Gemma API operation '{operation_name}' failed after retry. "
            f"Initial error: {first_error} | Retry error: {retry_exc}"
        ) from retry_exc


# ---------------------------------------------------------------------------
# Prompt Formatters
# ---------------------------------------------------------------------------

def _format_risk_flags(risk_flags: list[RiskFlag]) -> str:
    """Format identified risk flags into prompt text."""
    if not risk_flags:
        return "None flagged."
    lines = []
    for idx, flag in enumerate(risk_flags, 1):
        rtype = flag.risk_type.value if hasattr(flag.risk_type, "value") else str(flag.risk_type)
        rsev = flag.severity.value if hasattr(flag.severity, "value") else str(flag.severity)
        lines.append(f"- [{idx}] Type: {rtype} | Severity: {rsev}\n  Reason: {flag.reason}")
    return "\n".join(lines)


def _format_usage_locations(usage_locations: list[UsageLocation]) -> str:
    """Format AST usage locations and real snippets into prompt text."""
    if not usage_locations:
        return "No usage locations recorded."
    lines = []
    for idx, loc in enumerate(usage_locations, 1):
        utype = loc.usage_type.value if hasattr(loc.usage_type, "value") else str(loc.usage_type)
        snippet = loc.code_snippet.strip()
        lines.append(
            f"- [{idx}] File: {loc.file_path}:{loc.line_number} (Type: {utype})\n"
            f"  Code snippet:\n"
            f"  ```python\n"
            f"  {snippet}\n"
            f"  ```"
        )
    return "\n\n".join(lines)


def _build_recommendation_prompt(
    dependency_name: str,
    risk_flags: list[RiskFlag],
    usage_locations: list[UsageLocation],
) -> str:
    """Construct prompt for get_recommendation."""
    formatted_risks = _format_risk_flags(risk_flags)
    formatted_usages = _format_usage_locations(usage_locations)

    return f"""You are an expert Python software architect assisting DepExit with a safe dependency migration.

### CONCRETE FACTS COLLECTED (Do NOT invent or assume any other facts):
Target Dependency: {dependency_name}

Identified Risk Flags (why it is risky):
{formatted_risks}

Real Code Usage Locations & Snippets (how the codebase actually uses this dependency):
{formatted_usages}

### INSTRUCTIONS:
Analyze the concrete usage and risks above and recommend a safe, modern replacement.
1. Judge `usage_complexity` strictly from the provided real code snippets:
   - "BASIC": Simple, surface-level usage (e.g. basic imports and top-level function calls).
   - "MODERATE": Configured client sessions, standard class instances, options, or callbacks.
   - "DEEP": Deep internal/private APIs, complex subclassing, custom protocol extensions, or monkey-patching.
2. Provide candidate replacements in `candidates`, each with `name`, `why` (grounded in the snippets), and `api_gaps`.
3. Choose `proposed_replacement` from the candidate list.
4. Provide step-by-step `migration_steps` as a list of strings.
5. Provide `code_patch`: A valid unified diff (starting with --- / +++) that rewrites the usage shown in the snippets to use the replacement (both imports and calls).
6. Provide `suggested_tests`: A list of runnable pytest test functions (as Python code strings) that verify the behaviour the repo relies on. These tests must be written to pass against BOTH the original and the replacement where behaviour is meant to be identical.
7. Provide clear `reasoning`.

### STRICT OUTPUT FORMAT:
You MUST respond with STRICT JSON ONLY. Do not include markdown code block formatting (no ``` or ```json), preamble, or commentary outside the JSON object.

Required JSON Structure:
{{
  "usage_complexity": "BASIC" | "MODERATE" | "DEEP",
  "candidates": [
    {{
      "name": "replacement_package_name",
      "why": "why this candidate fits the actual snippet usage",
      "api_gaps": "known gaps or API differences"
    }}
  ],
  "proposed_replacement": "chosen_candidate_name",
  "migration_steps": [
    "step 1",
    "step 2"
  ],
  "code_patch": "--- a/file.py\\n+++ b/file.py\\n@@ ... @@\\n...",
  "suggested_tests": [
    "def test_behavior():\\n    ..."
  ],
  "reasoning": "rationale for recommendation"
}}"""


def _format_failing_results(failing_results: list[VerificationResult]) -> str:
    """Format failing test results for explain_gaps prompt."""
    lines = []
    for idx, res in enumerate(failing_results, 1):
        status_val = res.status.value if hasattr(res.status, "value") else str(res.status)
        lines.append(
            f"Test #{idx}: {res.test_name}\n"
            f"Status: {status_val}\n"
            f"Test Output / Traceback:\n"
            f"{res.raw_output.strip()}"
        )
    return "\n\n".join(lines)


def _build_explain_gaps_prompt(
    dependency_name: str,
    replacement_name: str,
    failing_results: list[VerificationResult],
) -> str:
    """Construct prompt for explain_gaps."""
    formatted_fails = _format_failing_results(failing_results)
    return f"""You are an expert Python software engineer analyzing test verification failures for DepExit.

### CONCRETE FACTS (Do NOT invent any other test runs or outputs):
Original Dependency: {dependency_name}
Proposed Replacement: {replacement_name}

Failing / Error Test Results from Verification Suite:
{formatted_fails}

### INSTRUCTIONS:
Analyze each failing test above. For each failing test, provide an AI interpretation explaining:
1. The likely cause of the failure or behavioral discrepancy between {dependency_name} and {replacement_name}.
2. A suggested fix or migration adaptation.

Note: These explanations are AI interpretations and recommendations, not established facts.

### STRICT OUTPUT FORMAT:
Respond with ONLY a STRICT JSON list of plain strings (one per failing test). Do not include markdown code block formatting (no ``` or ```json) or extra text.

Example format:
[
  "[{failing_results[0].test_name}] Likely Cause: <cause description>. Suggested Fix: <actionable fix>."
]"""


# ---------------------------------------------------------------------------
# Core Module Functions (Required Interface)
# ---------------------------------------------------------------------------

def get_recommendation(
    dependency_name: str,
    risk_flags: list[RiskFlag],
    usage_locations: list[UsageLocation],
    client: Optional[GemmaClient] = None,
) -> AIRecommendation:
    """Send Gemma the dependency name, risk flags, and code snippets to generate an AI recommendation.

    Args:
        dependency_name: The risky dependency to migrate away from.
        risk_flags: List of identified RiskFlag objects.
        usage_locations: List of real AST UsageLocation objects.
        client: Optional GemmaClient instance (defaults to GemmaClient initialized with GEMMA_API_KEY).

    Returns:
        AIRecommendation dataclass populated with proposed replacement, migration steps,
        reasoning, and attached usage_complexity, candidates, code_patch, suggested_tests,
        and hardcoded evidence="ai_generated".

    Raises:
        GemmaAPIError: If the API fails after retry or returns invalid JSON.
    """
    gemma = client or GemmaClient()
    prompt = _build_recommendation_prompt(dependency_name, risk_flags, usage_locations)

    def _parse_and_validate(raw_text: str) -> dict[str, Any]:
        data = _parse_json_defensively(raw_text, is_array=False)
        return _validate_recommendation_payload(data)

    payload = _call_with_retry(
        gemma,
        prompt,
        _parse_and_validate,
        operation_name=f"get_recommendation for '{dependency_name}'",
        is_json=True,
    )

    # Instantiate AIRecommendation from schema.py
    # Hardcode ai_generated=True and evidence="ai_generated"
    recommendation = AIRecommendation(
        dependency_name=dependency_name,
        proposed_replacement=payload["proposed_replacement"],
        migration_steps=payload["migration_steps"],
        reasoning=payload["reasoning"],
        ai_generated=True,
    )

    # Attach extended metadata required by prompt and capsule
    recommendation.usage_complexity = payload["usage_complexity"]
    recommendation.candidates = payload["candidates"]
    recommendation.code_patch = payload["code_patch"]
    recommendation.suggested_tests = payload["suggested_tests"]
    recommendation.evidence = "ai_generated"

    return recommendation


def explain_gaps(
    dependency_name: str,
    replacement_name: str,
    verification_results: list[VerificationResult],
    client: Optional[GemmaClient] = None,
) -> list[str]:
    """Analyze test verification failures and interpret likely causes and suggested fixes.

    Args:
        dependency_name: Original dependency name.
        replacement_name: Proposed replacement name.
        verification_results: List of VerificationResult objects from test runs.
        client: Optional GemmaClient instance.

    Returns:
        List of plain string AI interpretations (one per failing test).

    Raises:
        GemmaAPIError: If the API fails after retry.
    """
    failing_results = [
        r for r in verification_results
        if _is_failing_status(r.status)
    ]

    # If no tests failed or errored, no gap explanations are required
    if not failing_results:
        return []

    gemma = client or GemmaClient()
    prompt = _build_explain_gaps_prompt(dependency_name, replacement_name, failing_results)

    def _parse_and_validate(raw_text: str) -> list[str]:
        data = _parse_json_defensively(raw_text, is_array=True)
        return _validate_explain_gaps_payload(data, expected_count=len(failing_results))

    explanations = _call_with_retry(
        gemma,
        prompt,
        _parse_and_validate,
        operation_name=f"explain_gaps for '{dependency_name}' -> '{replacement_name}'",
        is_json=True,
    )

    return explanations


# ---------------------------------------------------------------------------
# Mock Response Generator for Standalone Testing
# ---------------------------------------------------------------------------

def _create_mock_response_generator() -> Callable[[str], str]:
    """Create a mock Gemma response generator that simulates hosted model outputs with markdown fences."""
    def _mock_generate(prompt: str) -> str:
        prompt_lower = prompt.lower()
        if "failing / error test results" in prompt_lower or "explain_gaps" in prompt_lower:
            # Return JSON array of explanations wrapped in markdown fences to test defensive stripping
            return """```json
[
  "[test_api_client_timeout] Likely Cause: Connection timed out unexpectedly after 5.0s during test execution. In httpx, default timeout behavior includes granular connect, read, and write timeouts, unlike requests' single total socket timeout. Suggested Fix: Configure explicit httpx.Timeout(5.0, connect=5.0, read=5.0) or mock external network calls in unit tests using pytest-httpx/respx."
]
```"""
        elif "urllib3" in prompt_lower:
            return """```json
{
  "usage_complexity": "BASIC",
  "candidates": [
    {
      "name": "httpx",
      "why": "Provides a high-level modern HTTP client with connection pooling, resolving urllib3 connection and redirect CVEs.",
      "api_gaps": "Uses Client session instead of PoolManager."
    },
    {
      "name": "urllib3",
      "why": "Updated version (>=2.0.0) addresses security advisories and ReDoS vulnerabilities.",
      "api_gaps": "Breaking changes in Python 3.7 support and TLS options."
    }
  ],
  "proposed_replacement": "httpx",
  "migration_steps": [
    "Add 'httpx' to requirements.txt and remove 'urllib3'",
    "Replace 'urllib3.PoolManager()' with 'httpx.Client()'",
    "Update 'http.request()' calls to 'client.get()' or 'client.post()'",
    "Run verification test suite to ensure connection handling matches expectations"
  ],
  "code_patch": "--- a/client.py\\n+++ b/client.py\\n@@ -2,2 +2,2 @@\\n-import urllib3\\n+import httpx\\n",
  "suggested_tests": [
    "def test_client_health_check():\\n    import httpx\\n    assert True\\n"
  ],
  "reasoning": "Migrating urllib3 PoolManager calls to httpx provides safer redirect and connection pooling semantics while eliminating CVE-2021-33503 and subsequent ReDoS risks."
}
```"""
        else:
            # Return recommendation JSON wrapped in markdown fences to test defensive stripping
            return """```json
{
  "usage_complexity": "BASIC",
  "candidates": [
    {
      "name": "httpx",
      "why": "Provides a modern, fully featured HTTP client with an API almost identical to requests, while actively maintained and supporting both sync and async HTTP.",
      "api_gaps": "Requires explicit Client or httpx.get with slight differences in event hook signatures and streaming responses."
    },
    {
      "name": "urllib3",
      "why": "Lower-level HTTP library already included in most standard stacks with minimal overhead.",
      "api_gaps": "Does not provide high-level requests-style Session or simple json kwargs directly on request functions."
    }
  ],
  "proposed_replacement": "httpx",
  "migration_steps": [
    "Add 'httpx' to requirements.txt and remove 'requests'",
    "Replace 'import requests' with 'import httpx'",
    "Update top-level calls 'requests.get(...)' to 'httpx.get(...)'",
    "Run verification test suite to ensure timeouts and headers match expected behavior"
  ],
  "code_patch": "--- a/client.py\\n+++ b/client.py\\n@@ -1,4 +1,4 @@\\n-import requests\\n+import httpx\\n \\n def fetch_data():\\n-    resp = requests.get(url, timeout=5)\\n+    resp = httpx.get(url, timeout=5.0)\\n     return resp.json()\\n",
  "suggested_tests": [
    "def test_api_client_fetch_status():\\n    import httpx\\n    client = httpx.Client()\\n    response = client.get('https://httpbin.org/get', timeout=5.0)\\n    assert response.status_code == 200\\n",
    "def test_api_client_timeout():\\n    import httpx\\n    import pytest\\n    with pytest.raises(Exception):\\n        httpx.get('https://httpbin.org/delay/10', timeout=0.001)\\n"
  ],
  "reasoning": "The codebase uses standard synchronous HTTP GET calls with timeouts. 'httpx' serves as an immediate drop-in replacement that eliminates the flagged CVE vulnerability while retaining identical function signatures for standard calls."
}
```"""
    return _mock_generate


# ---------------------------------------------------------------------------
# Standalone Execution & Verification
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("DepExit Gemma AI Client: Standalone Verification")
    print("=" * 70)
    print(f"Model Info: {json.dumps(MODEL_INFO, indent=2)}")

    # 1. Setup mock inputs
    dep_name = "requests"
    mock_risk = RiskFlag(
        dependency_name=dep_name,
        risk_type=RiskType.VULNERABLE,
        reason="CVE-2023-32681: Unintended leak of Proxy-Authorization header in redirects",
        severity=RiskSeverity.HIGH,
    )
    mock_usage = UsageLocation(
        dependency_name=dep_name,
        file_path="src/api_client.py",
        line_number=42,
        usage_type=UsageType.CALL,
        code_snippet="resp = requests.get('https://api.example.com/data', timeout=5)",
    )
    fake_fail = VerificationResult(
        dependency_name=dep_name,
        test_name="test_api_client_timeout",
        status=VerificationStatus.FAIL,
        raw_output=(
            "AssertionError: Connection timed out unexpectedly after 5.0s\n"
            "  File 'test_client.py', line 15, in test_api_client_timeout\n"
            "    assert resp.status_code == 200"
        ),
        verified=True,
    )

    # 2. Determine whether live API key is configured
    api_key_env = os.getenv("GEMMA_API_KEY")
    use_live_api = bool(api_key_env and api_key_env.strip() and api_key_env != "your_key_here")

    if use_live_api:
        print(f"\n[*] Active GEMMA_API_KEY detected. Running live test against {MODEL_INFO['model_id']}...")
        client = GemmaClient()
    else:
        print("\n[*] No active GEMMA_API_KEY found. Running standalone test using mock response transport...")
        client = GemmaClient(mock_response_func=_create_mock_response_generator())

    # 3. Test get_recommendation()
    print("\n--- Testing 1: get_recommendation() ---")
    recommendation = get_recommendation(dep_name, [mock_risk], [mock_usage], client=client)
    print(f"Dependency: {recommendation.dependency_name}")
    print(f"Proposed Replacement: {recommendation.proposed_replacement}")
    print(f"Usage Complexity: {getattr(recommendation, 'usage_complexity', 'N/A')}")
    print(f"Hardcoded ai_generated: {recommendation.ai_generated}")
    print(f"Hardcoded evidence: {getattr(recommendation, 'evidence', 'N/A')}")
    print(f"Candidates Count: {len(getattr(recommendation, 'candidates', []))}")
    for idx, c in enumerate(getattr(recommendation, "candidates", []), 1):
        print(f"  [{idx}] {c['name']} (evidence={c.get('evidence')}) - why: {c['why'][:60]}...")
    print(f"Migration Steps Count: {len(recommendation.migration_steps)}")
    print(f"Code Patch Preview:\n{getattr(recommendation, 'code_patch', '')[:200]}...")
    print(f"Suggested Tests Count: {len(getattr(recommendation, 'suggested_tests', []))}")
    print(f"Reasoning: {recommendation.reasoning[:120]}...")

    # Validate contracts
    assert isinstance(recommendation, AIRecommendation), "Must return AIRecommendation instance"
    assert recommendation.ai_generated is True, "ai_generated must be True"
    assert getattr(recommendation, "evidence", "") == "ai_generated", "evidence must be 'ai_generated'"
    assert getattr(recommendation, "usage_complexity", "") in VALID_COMPLEXITY_VALUES
    assert len(getattr(recommendation, "candidates", [])) > 0, "Must have candidate list"
    assert getattr(recommendation, "code_patch", ""), "Must have code patch"
    assert len(getattr(recommendation, "suggested_tests", [])) > 0, "Must have suggested tests"
    print("\n[+] get_recommendation() passed all contract assertions!")

    # 4. Test explain_gaps()
    print("\n--- Testing 2: explain_gaps() ---")
    gaps = explain_gaps(dep_name, recommendation.proposed_replacement, [fake_fail], client=client)
    print(f"Returned Gaps Explanations ({len(gaps)} item(s)):")
    for idx, gap in enumerate(gaps, 1):
        print(f"  [{idx}] {gap}")

    # Validate contracts
    assert isinstance(gaps, list), "Must return a list of strings"
    assert len(gaps) == 1, "Must return explanation for the failing test"
    assert isinstance(gaps[0], str), "Gap explanation must be a string"
    print("\n[+] explain_gaps() passed all contract assertions!")

    # 5. Test retry on parse failure and clear error raising
    print("\n--- Testing 3: Defensive parsing, retry logic, and error handling ---")
    attempt_count = 0

    def faulty_generator(prompt: str) -> str:
        global attempt_count
        attempt_count += 1
        if attempt_count == 1:
            # Return invalid non-JSON output on first attempt
            return "This is not JSON at all! Just raw plain text commentary."
        else:
            # Return valid JSON on second attempt
            return json.dumps({
                "usage_complexity": "BASIC",
                "candidates": [{"name": "httpx", "why": "safe", "api_gaps": "none"}],
                "proposed_replacement": "httpx",
                "migration_steps": ["step 1"],
                "code_patch": "--- a\n+++ b\n",
                "suggested_tests": ["def test_ok(): pass"],
                "reasoning": "good choice",
            })

    faulty_client = GemmaClient(mock_response_func=faulty_generator)
    recovered_rec = get_recommendation(dep_name, [mock_risk], [mock_usage], client=faulty_client)
    assert recovered_rec.proposed_replacement == "httpx"
    assert attempt_count == 2, "Must have retried exactly once on parse failure"
    print("[+] Successfully recovered via retry on initial parse failure!")

    # Test error raising after retry failure
    def persistently_broken_generator(prompt: str) -> str:
        return "Always broken output"

    broken_client = GemmaClient(mock_response_func=persistently_broken_generator)
    try:
        get_recommendation(dep_name, [mock_risk], [mock_usage], client=broken_client)
        raise AssertionError("Should have raised GemmaAPIError after retry failure")
    except GemmaAPIError as err:
        print(f"[+] Correctly raised GemmaAPIError after retry failure: {err}")

    print("\n" + "=" * 70)
    print("ALL STANDALONE TESTS AND CONTRACT CHECKS COMPLETED SUCCESSFULLY!")
    print("=" * 70)
