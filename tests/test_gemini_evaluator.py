"""The Gemini backend, with the Google Gen AI client faked.

Asserts parity with the Anthropic path: same prompt blueprint, same verdict
shape, same failure semantics — the orchestrator must not be able to tell them
apart.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.domain import DocumentationBundle
from app.parsers.diff import analyse_diff
from app.services.llm.evaluator import EvaluationError
from app.services.llm.gemini import GeminiDriftEvaluator
from app.services.llm.schema import (
    DRIFT_VERDICT_JSON_SCHEMA,
    GEMINI_VERDICT_JSON_SCHEMA,
    DriftStatus,
)

NEEDS_UPDATE = {
    "status": "NEEDS_UPDATE",
    "reason": "REDIS_URL is read at startup but the README never mentions it.",
    "suggested_edit": "- `REDIS_URL` — Redis connection string.",
}
UP_TO_DATE = {
    "status": "UP_TO_DATE",
    "reason": "The change is internal; the setup instructions still hold.",
    "suggested_edit": "",
}


def make_gemini_response(
    payload: dict[str, Any] | str | None,
    *,
    finish_reason: str = "STOP",
    include_thought_part: bool = True,
    model_version: str = "gemini-3.7-flash",
    usage: dict[str, int] | None = None,
) -> SimpleNamespace:
    """A response object shaped like the SDK's.

    A `thought` part is included first by default — Gemini 2.5 thinks, and
    naively reading the first part would return reasoning instead of the answer.
    """
    parts: list[SimpleNamespace] = []
    if include_thought_part:
        parts.append(SimpleNamespace(text="Let me compare the diff...", thought=True))

    text: str | None = None
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        parts.append(SimpleNamespace(text=text, thought=False))

    usage_values = {
        "prompt_token_count": 2000,
        "candidates_token_count": 150,
        "thoughts_token_count": 50,
        "cached_content_token_count": 0,
        "total_token_count": 2200,
    }
    usage_values.update(usage or {})

    return SimpleNamespace(
        # The SDK's `.text` property returns None when there is no answer part.
        text=text,
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=parts),
                finish_reason=SimpleNamespace(value=finish_reason),
            )
        ],
        usage_metadata=SimpleNamespace(**usage_values),
        model_version=model_version,
    )


class FakeGeminiClient:
    """Minimal `client.aio.models.generate_content` stub that records kwargs."""

    def __init__(self, response: Any = None, *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate_content))

    async def _generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response

    @property
    def last_call(self) -> dict[str, Any]:
        assert self.calls, "no Gemini call was made"
        return self.calls[-1]


@pytest.fixture
def analysis(sample_diff: str):
    return analyse_diff(sample_diff)


def _evaluator(client: FakeGeminiClient) -> GeminiDriftEvaluator:
    return GeminiDriftEvaluator(client=client, model="gemini-3.7-flash", max_output_tokens=4096)


# --------------------------------------------------------------------------- #
# Schema adaptation
# --------------------------------------------------------------------------- #


def test_gemini_schema_drops_unsupported_keywords() -> None:
    """Gemini's OpenAPI 3.0 subset rejects `additionalProperties`."""
    assert "additionalProperties" not in GEMINI_VERDICT_JSON_SCHEMA
    assert "additionalProperties" in DRIFT_VERDICT_JSON_SCHEMA


def test_gemini_schema_keeps_the_contract_intact() -> None:
    """Adapting for the dialect must not weaken what the model must return."""
    assert GEMINI_VERDICT_JSON_SCHEMA["required"] == DRIFT_VERDICT_JSON_SCHEMA["required"]
    assert GEMINI_VERDICT_JSON_SCHEMA["properties"] == DRIFT_VERDICT_JSON_SCHEMA["properties"]
    assert GEMINI_VERDICT_JSON_SCHEMA["properties"]["status"]["enum"] == [
        s.value for s in DriftStatus
    ]


def test_gemini_schema_pins_property_order() -> None:
    assert GEMINI_VERDICT_JSON_SCHEMA["propertyOrdering"] == [
        "status",
        "reason",
        "suggested_edit",
    ]


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


async def test_returns_a_needs_update_verdict(documentation: DocumentationBundle, analysis) -> None:
    client = FakeGeminiClient(make_gemini_response(NEEDS_UPDATE))
    result = await _evaluator(client).evaluate(documentation, analysis)

    assert result.verdict.status is DriftStatus.NEEDS_UPDATE
    assert "REDIS_URL" in result.verdict.suggested_edit
    assert result.model == "gemini-3.7-flash"


async def test_returns_an_up_to_date_verdict(documentation: DocumentationBundle, analysis) -> None:
    client = FakeGeminiClient(make_gemini_response(UP_TO_DATE))
    result = await _evaluator(client).evaluate(documentation, analysis)

    assert result.verdict.status is DriftStatus.UP_TO_DATE
    assert result.verdict.has_suggestion is False


async def test_the_answer_is_read_past_a_thinking_part(
    documentation: DocumentationBundle, analysis
) -> None:
    """`.text` is absent on some shapes; the fallback must skip thought parts."""
    response = make_gemini_response(UP_TO_DATE, include_thought_part=True)
    response.text = None  # force the parts-walking fallback

    result = await _evaluator(FakeGeminiClient(response)).evaluate(documentation, analysis)
    assert result.verdict.status is DriftStatus.UP_TO_DATE


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #


async def test_sends_the_adapted_schema_and_json_mime_type(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeGeminiClient(make_gemini_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    call = client.last_call
    assert call["model"] == "gemini-3.7-flash"

    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == GEMINI_VERDICT_JSON_SCHEMA
    assert config.max_output_tokens == 4096


async def test_the_role_instruction_is_the_system_instruction(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeGeminiClient(make_gemini_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    system = client.last_call["config"].system_instruction
    assert "expert technical writer" in system


async def test_prompt_sections_keep_the_blueprint_order(
    documentation: DocumentationBundle, analysis
) -> None:
    """Same contract as the Anthropic path: docs, then diff, then task."""
    client = FakeGeminiClient(make_gemini_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    prompt = client.last_call["contents"]
    positions = [
        prompt.index("[EXISTING DOCUMENTATION]"),
        prompt.index("[INCOMING CODE DIFF]"),
        prompt.index("[TASK]"),
    ]
    assert positions == sorted(positions)
    assert "REDIS_URL" in prompt


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "finish_reason", ["SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII"]
)
async def test_a_blocked_response_is_surfaced_not_treated_as_a_verdict(
    documentation: DocumentationBundle, analysis, finish_reason: str
) -> None:
    response = make_gemini_response(None, finish_reason=finish_reason)
    with pytest.raises(EvaluationError, match="declined"):
        await _evaluator(FakeGeminiClient(response)).evaluate(documentation, analysis)


async def test_hitting_the_output_cap_gives_an_actionable_error(
    documentation: DocumentationBundle, analysis
) -> None:
    response = make_gemini_response(None, finish_reason="MAX_TOKENS")
    with pytest.raises(EvaluationError, match="GEMINI_MAX_OUTPUT_TOKENS"):
        await _evaluator(FakeGeminiClient(response)).evaluate(documentation, analysis)


async def test_malformed_json_becomes_an_evaluation_error(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeGeminiClient(make_gemini_response("this is not json"))
    with pytest.raises(EvaluationError, match="not valid JSON"):
        await _evaluator(client).evaluate(documentation, analysis)


async def test_transport_errors_are_wrapped(documentation: DocumentationBundle, analysis) -> None:
    client = FakeGeminiClient(error=RuntimeError("connection reset"))
    with pytest.raises(EvaluationError, match="Gemini request failed"):
        await _evaluator(client).evaluate(documentation, analysis)


async def test_an_empty_response_is_an_error_not_a_silent_pass(
    documentation: DocumentationBundle, analysis
) -> None:
    response = make_gemini_response(None, include_thought_part=False)
    with pytest.raises(EvaluationError, match="no text content"):
        await _evaluator(FakeGeminiClient(response)).evaluate(documentation, analysis)


# --------------------------------------------------------------------------- #
# Usage accounting
# --------------------------------------------------------------------------- #


async def test_usage_maps_onto_the_shared_shape(
    documentation: DocumentationBundle, analysis
) -> None:
    """Cached tokens leave `input_tokens`; thinking folds into output — so the
    numbers mean the same thing on both providers."""
    client = FakeGeminiClient(
        make_gemini_response(
            UP_TO_DATE,
            usage={
                "prompt_token_count": 2000,
                "cached_content_token_count": 1600,
                "candidates_token_count": 100,
                "thoughts_token_count": 40,
            },
        )
    )
    result = await _evaluator(client).evaluate(documentation, analysis)
    usage = result.usage

    assert usage.input_tokens == 400
    assert usage.cache_read_input_tokens == 1600
    assert usage.output_tokens == 140
    assert usage.total_input == 2000
    assert usage.cache_hit_ratio == pytest.approx(0.8)


async def test_a_response_without_usage_metadata_is_tolerated(
    documentation: DocumentationBundle, analysis
) -> None:
    response = make_gemini_response(UP_TO_DATE)
    response.usage_metadata = None

    result = await _evaluator(FakeGeminiClient(response)).evaluate(documentation, analysis)
    assert result.usage.total_input == 0
