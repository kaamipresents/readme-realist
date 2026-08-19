"""Selects and constructs the model backend.

The rest of the application depends on `SupportsDriftEvaluation`, never on a
concrete provider — so switching backends is one environment variable, not a
code change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import LLMProvider, Settings
from app.services.llm.evaluator import DriftEvaluator, SupportsDriftEvaluation
from app.services.llm.gemini import GeminiDriftEvaluator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMBackend:
    """A constructed evaluator plus whatever needs closing at shutdown."""

    evaluator: SupportsDriftEvaluation
    provider: LLMProvider
    model: str
    _client: object = None

    async def aclose(self) -> None:
        """Release the underlying HTTP client, if it exposes a closer."""
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is None:
            return
        result = closer()
        if hasattr(result, "__await__"):
            await result


def build_backend(settings: Settings) -> LLMBackend:
    """Construct the evaluator selected by `LLM_PROVIDER`."""
    if settings.llm_provider is LLMProvider.ANTHROPIC:
        return _build_anthropic(settings)
    return _build_gemini(settings)


def _build_anthropic(settings: Settings) -> LLMBackend:
    import anthropic

    assert settings.anthropic_api_key is not None  # enforced by Settings validation
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.anthropic_timeout_seconds,
        max_retries=settings.anthropic_max_retries,
    )
    evaluator = DriftEvaluator(
        client=client,
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        effort=settings.anthropic_effort.value,
    )
    logger.info(
        "model backend ready",
        extra={
            "provider": "anthropic",
            "model": settings.anthropic_model,
            "effort": settings.anthropic_effort.value,
        },
    )
    return LLMBackend(
        evaluator=evaluator,
        provider=LLMProvider.ANTHROPIC,
        model=settings.anthropic_model,
        _client=client,
    )


def _build_gemini(settings: Settings) -> LLMBackend:
    from google import genai
    from google.genai import types

    assert settings.gemini_api_key is not None  # enforced by Settings validation
    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            # The SDK expects milliseconds here, unlike the Anthropic client.
            timeout=int(settings.gemini_timeout_seconds * 1000),
            # Gemini returns 503 UNAVAILABLE under load often enough that a
            # single spike would otherwise fail a review. The Anthropic client
            # retries out of the box; this brings the Gemini path to parity.
            retry_options=types.HttpRetryOptions(
                attempts=settings.gemini_max_retries,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        ),
    )
    evaluator = GeminiDriftEvaluator(
        client=client,
        model=settings.gemini_model,
        max_output_tokens=settings.gemini_max_output_tokens,
    )
    logger.info(
        "model backend ready",
        extra={"provider": "gemini", "model": settings.gemini_model},
    )
    return LLMBackend(
        evaluator=evaluator,
        provider=LLMProvider.GEMINI,
        model=settings.gemini_model,
        _client=client,
    )
