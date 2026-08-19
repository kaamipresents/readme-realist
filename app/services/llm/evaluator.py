"""The Claude call that decides whether documentation has drifted.

Uses structured outputs (`output_config.format`) so the response is constrained
to the drift-verdict schema at generation time, rather than parsed hopefully
afterwards.

Prompt caching: the system block and the documentation block form a stable
prefix across pushes to the same pull request, so the cache breakpoint sits at
the end of the documentation. The volatile diff follows it and is never cached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.domain import DiffAnalysis, DocumentationBundle
from app.services.llm.prompts import (
    ROLE_INSTRUCTION,
    render_diff_block,
    render_documentation_block,
    render_task_block,
)
from app.services.llm.schema import DRIFT_VERDICT_JSON_SCHEMA, DriftVerdict

logger = logging.getLogger(__name__)


class EvaluationError(RuntimeError):
    """The model could not produce a usable verdict."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Per-call token accounting — the input to any cost dashboard."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @property
    def cache_hit_ratio(self) -> float:
        return self.cache_read_input_tokens / self.total_input if self.total_input else 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_hit_ratio": round(self.cache_hit_ratio, 3),
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    verdict: DriftVerdict
    usage: TokenUsage
    model: str
    stop_reason: str | None = None


class SupportsDriftEvaluation(Protocol):
    """What the orchestrator needs from a model backend.

    Both `DriftEvaluator` (Anthropic) and `GeminiDriftEvaluator` satisfy this,
    so the pipeline is provider-agnostic.
    """

    async def evaluate(
        self, documentation: DocumentationBundle, analysis: DiffAnalysis
    ) -> EvaluationResult: ...


class DriftEvaluator:
    """Wraps the Anthropic client with this application's prompt and schema."""

    def __init__(
        self,
        *,
        client: Any,  # anthropic.AsyncAnthropic — loose so tests can inject a fake
        model: str = "claude-opus-5",
        max_tokens: int = 8000,
        effort: str = "high",
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort

    async def evaluate(
        self, documentation: DocumentationBundle, analysis: DiffAnalysis
    ) -> EvaluationResult:
        """Ask Claude whether this diff leaves the documentation stale."""
        system = [{"type": "text", "text": ROLE_INSTRUCTION}]
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": render_documentation_block(documentation),
                        # Cache breakpoint: docs rarely change between pushes to
                        # the same PR, so re-reviews replay this prefix cheaply.
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": f"{render_diff_block(analysis)}\n\n{render_task_block()}",
                    },
                ],
            }
        ]

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": DRIFT_VERDICT_JSON_SCHEMA,
                    },
                    "effort": self._effort,
                },
            )
        except Exception as exc:
            raise EvaluationError(f"Claude request failed: {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)

        # Safety classifiers can decline with HTTP 200 and an empty content
        # array, so check the stop reason before touching `content`.
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise EvaluationError(
                f"Claude declined this request (category={category!r}); no verdict produced"
            )

        text = _first_text_block(response)
        if text is None:
            if stop_reason == "max_tokens":
                raise EvaluationError(
                    "response hit max_tokens before emitting a verdict — "
                    "raise ANTHROPIC_MAX_TOKENS or lower ANTHROPIC_EFFORT"
                )
            raise EvaluationError(f"no text block in Claude response (stop_reason={stop_reason!r})")

        try:
            verdict = DriftVerdict.from_model_text(text)
        except ValueError as exc:
            raise EvaluationError(str(exc)) from exc

        usage = _extract_usage(response)
        logger.info(
            "drift evaluation complete",
            extra={
                "status": verdict.status.value,
                "model": getattr(response, "model", self._model),
                **usage.as_dict(),
            },
        )

        return EvaluationResult(
            verdict=verdict,
            usage=usage,
            model=str(getattr(response, "model", self._model)),
            stop_reason=stop_reason,
        )


def _first_text_block(response: Any) -> str | None:
    """The first `text` block's content, or None when there is none.

    Thinking blocks precede text in the content array, so index-0 access would
    be wrong on any thinking-enabled model.
    """
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                return str(text)
    return None


def _extract_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
