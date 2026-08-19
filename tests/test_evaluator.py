"""The Semantic Verification Module, with Claude faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.domain import DocumentationBundle
from app.parsers.diff import analyse_diff
from app.services.llm.evaluator import DriftEvaluator, EvaluationError, TokenUsage
from app.services.llm.schema import DRIFT_VERDICT_JSON_SCHEMA, DriftStatus
from tests.conftest import FakeAnthropicClient, make_anthropic_response

NEEDS_UPDATE = {
    "status": "NEEDS_UPDATE",
    "reason": "REDIS_URL is read at startup but the README never mentions it.",
    "suggested_edit": "### Environment\n\n- `REDIS_URL` — Redis connection string.",
}
UP_TO_DATE = {
    "status": "UP_TO_DATE",
    "reason": "The change is internal and the setup instructions still hold.",
    "suggested_edit": "",
}


@pytest.fixture
def analysis(sample_diff: str):
    return analyse_diff(sample_diff)


def _evaluator(client: FakeAnthropicClient) -> DriftEvaluator:
    return DriftEvaluator(client=client, model="claude-opus-5", max_tokens=4096, effort="high")


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


async def test_returns_a_needs_update_verdict(documentation: DocumentationBundle, analysis) -> None:
    client = FakeAnthropicClient(make_anthropic_response(NEEDS_UPDATE))
    result = await _evaluator(client).evaluate(documentation, analysis)

    assert result.verdict.status is DriftStatus.NEEDS_UPDATE
    assert "REDIS_URL" in result.verdict.suggested_edit
    assert result.model == "claude-opus-5"


async def test_returns_an_up_to_date_verdict(documentation: DocumentationBundle, analysis) -> None:
    client = FakeAnthropicClient(make_anthropic_response(UP_TO_DATE))
    result = await _evaluator(client).evaluate(documentation, analysis)
    assert result.verdict.status is DriftStatus.UP_TO_DATE
    assert result.verdict.has_suggestion is False


async def test_text_is_read_past_a_leading_thinking_block(
    documentation: DocumentationBundle, analysis
) -> None:
    """Thinking is on by default, so `content[0]` is not the answer."""
    response = make_anthropic_response(UP_TO_DATE, include_thinking_block=True)
    assert response.content[0].type == "thinking"

    result = await _evaluator(FakeAnthropicClient(response)).evaluate(documentation, analysis)
    assert result.verdict.status is DriftStatus.UP_TO_DATE


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #


async def test_sends_the_strict_schema_and_effort(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeAnthropicClient(make_anthropic_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    call = client.last_call
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 4096
    assert call["output_config"]["format"] == {
        "type": "json_schema",
        "schema": DRIFT_VERDICT_JSON_SCHEMA,
    }
    assert call["output_config"]["effort"] == "high"


async def test_cache_breakpoint_sits_after_the_documentation(
    documentation: DocumentationBundle, analysis
) -> None:
    """Docs are stable across pushes; the diff is not. Only docs get cached."""
    client = FakeAnthropicClient(make_anthropic_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    blocks = client.last_call["messages"][0]["content"]
    assert len(blocks) == 2

    docs_block, diff_block = blocks
    assert docs_block["text"].startswith("[EXISTING DOCUMENTATION]")
    assert docs_block["cache_control"] == {"type": "ephemeral"}

    assert diff_block["text"].startswith("[INCOMING CODE DIFF]")
    assert "cache_control" not in diff_block, "the volatile diff must not be cached"


async def test_task_section_trails_the_diff_in_the_same_block(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeAnthropicClient(make_anthropic_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    diff_block = client.last_call["messages"][0]["content"][1]["text"]
    assert diff_block.index("[INCOMING CODE DIFF]") < diff_block.index("[TASK]")


async def test_role_instruction_is_the_system_prompt(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeAnthropicClient(make_anthropic_response(UP_TO_DATE))
    await _evaluator(client).evaluate(documentation, analysis)

    system = client.last_call["system"]
    assert system[0]["text"].startswith("You are an expert technical writer")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


async def test_a_refusal_is_surfaced_not_treated_as_a_verdict(
    documentation: DocumentationBundle, analysis
) -> None:
    """Classifiers decline with HTTP 200 and empty content — check stop_reason first."""
    refusal = SimpleNamespace(
        content=[],
        stop_reason="refusal",
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=0),
        stop_details=SimpleNamespace(category="cyber", explanation="declined"),
    )
    with pytest.raises(EvaluationError, match="declined"):
        await _evaluator(FakeAnthropicClient(refusal)).evaluate(documentation, analysis)


async def test_hitting_max_tokens_gives_an_actionable_error(
    documentation: DocumentationBundle, analysis
) -> None:
    truncated = SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="")],
        stop_reason="max_tokens",
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4096),
        stop_details=None,
    )
    with pytest.raises(EvaluationError, match="ANTHROPIC_MAX_TOKENS"):
        await _evaluator(FakeAnthropicClient(truncated)).evaluate(documentation, analysis)


async def test_malformed_json_becomes_an_evaluation_error(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeAnthropicClient(make_anthropic_response("this is not json"))
    with pytest.raises(EvaluationError, match="not valid JSON"):
        await _evaluator(client).evaluate(documentation, analysis)


async def test_transport_errors_are_wrapped(documentation: DocumentationBundle, analysis) -> None:
    client = FakeAnthropicClient(error=RuntimeError("connection reset"))
    with pytest.raises(EvaluationError, match="connection reset"):
        await _evaluator(client).evaluate(documentation, analysis)


# --------------------------------------------------------------------------- #
# Usage accounting
# --------------------------------------------------------------------------- #


async def test_usage_is_reported_for_cost_tracking(
    documentation: DocumentationBundle, analysis
) -> None:
    client = FakeAnthropicClient(
        make_anthropic_response(
            UP_TO_DATE,
            usage={
                "input_tokens": 400,
                "output_tokens": 120,
                "cache_read_input_tokens": 1600,
                "cache_creation_input_tokens": 0,
            },
        )
    )
    result = await _evaluator(client).evaluate(documentation, analysis)

    assert result.usage.total_input == 2000
    assert result.usage.cache_hit_ratio == pytest.approx(0.8)
    assert result.usage.as_dict()["cache_hit_ratio"] == 0.8


def test_usage_handles_a_response_with_no_cache_fields() -> None:
    usage = TokenUsage()
    assert usage.total_input == 0
    assert usage.cache_hit_ratio == 0.0
