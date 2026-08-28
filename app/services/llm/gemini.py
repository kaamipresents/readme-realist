"""Gemini implementation of the Semantic Verification Module.

Mirrors `DriftEvaluator` (Anthropic) exactly: same prompt blueprint, same
strict verdict schema, same `EvaluationResult` out. The orchestrator cannot
tell the two apart.

Two provider differences worth knowing:

* **Structured output** is `response_mime_type` + `response_json_schema` rather
  than `output_config.format`. Gemini's schema dialect is an OpenAPI 3.0
  subset, so the canonical schema is adapted in `schema.py`.
* **Caching.** The Anthropic path sets an explicit cache breakpoint after the
  documentation block. Gemini has no per-block breakpoint — it applies implicit
  caching to a repeated prompt prefix automatically. The block order here is
  still stable-prefix-first (system → docs → diff) so implicit caching has the
  best chance of hitting; `cached_content_token_count` is reported so you can
  see whether it did.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.domain import DiffAnalysis, DocumentationBundle
from app.services.llm.evaluator import (
    EvaluationError,
    EvaluationResult,
    TokenUsage,
)
from app.services.llm.prompts import (
    ROLE_INSTRUCTION,
    render_diff_block,
    render_documentation_block,
    render_task_block,
)
from app.services.llm.schema import GEMINI_VERDICT_JSON_SCHEMA, DriftVerdict

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

#: Finish reasons meaning the model declined rather than answered. Treated the
#: same way as an Anthropic `stop_reason: "refusal"` — surfaced, never mistaken
#: for a verdict.
_REFUSAL_FINISH_REASONS: frozenset[str] = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
    }
)


class GeminiDriftEvaluator:
    """Wraps the Google Gen AI client with this application's prompt and schema."""

    def __init__(
        self,
        *,
        client: Any,  # google.genai.Client — loose so tests can inject a fake
        model: str = DEFAULT_GEMINI_MODEL,
        max_output_tokens: int = 8000,
    ) -> None:
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def evaluate(
        self, documentation: DocumentationBundle, analysis: DiffAnalysis
    ) -> EvaluationResult:
        """Ask Gemini whether this diff leaves the documentation stale."""
        # Imported lazily so the Anthropic-only path never needs the SDK.
        from google.genai import types

        prompt = "\n\n".join(
            [
                render_documentation_block(documentation),
                render_diff_block(analysis),
                render_task_block(),
            ]
        )

        config = types.GenerateContentConfig(
            system_instruction=ROLE_INSTRUCTION,
            response_mime_type="application/json",
            response_json_schema=GEMINI_VERDICT_JSON_SCHEMA,
            max_output_tokens=self._max_output_tokens,
            # We send no tools, and leaving automatic function calling on makes
            # the SDK emit a warning on every single call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
        except Exception as exc:
            raise EvaluationError(f"Gemini request failed: {exc}") from exc

        finish_reason = _finish_reason(response)

        if finish_reason in _REFUSAL_FINISH_REASONS:
            raise EvaluationError(
                f"Gemini declined this request (finish_reason={finish_reason}); no verdict produced"
            )

        text = _response_text(response)
        if not text:
            if finish_reason == "MAX_TOKENS":
                raise EvaluationError(
                    "response hit max_output_tokens before emitting a verdict — "
                    "raise GEMINI_MAX_OUTPUT_TOKENS"
                )
            raise EvaluationError(
                f"no text content in Gemini response (finish_reason={finish_reason!r})"
            )

        try:
            verdict = DriftVerdict.from_model_text(text)
        except ValueError as exc:
            raise EvaluationError(str(exc)) from exc

        usage = _extract_usage(response)
        model_version = str(getattr(response, "model_version", None) or self._model)

        logger.info(
            "drift evaluation complete",
            extra={
                "provider": "gemini",
                "status": verdict.status.value,
                "model": model_version,
                **usage.as_dict(),
            },
        )

        return EvaluationResult(
            verdict=verdict,
            usage=usage,
            model=model_version,
            stop_reason=finish_reason,
        )


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    raw = getattr(candidates[0], "finish_reason", None)
    if raw is None:
        return None
    # The SDK returns an enum whose `.value` is the wire string.
    return str(getattr(raw, "value", raw))


def _response_text(response: Any) -> str | None:
    """The response's JSON body.

    Prefers the SDK's `.text` convenience property, falling back to walking the
    candidate parts — and skipping any marked `thought`, which is reasoning
    rather than the answer.
    """
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                return part_text
    return None


def _extract_usage(response: Any) -> TokenUsage:
    """Map Gemini's usage metadata onto the shared `TokenUsage` shape.

    Cached tokens are subtracted out of the prompt count so `input_tokens`
    means "uncached input" on both providers, and thinking tokens are folded
    into output — matching how Anthropic bills and reports them.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return TokenUsage()

    def value(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    cached = value("cached_content_token_count")
    prompt_tokens = value("prompt_token_count")

    return TokenUsage(
        input_tokens=max(prompt_tokens - cached, 0),
        output_tokens=value("candidates_token_count") + value("thoughts_token_count"),
        cache_read_input_tokens=cached,
        # Gemini's implicit caching has no separate write-side charge to report.
        cache_creation_input_tokens=0,
    )
