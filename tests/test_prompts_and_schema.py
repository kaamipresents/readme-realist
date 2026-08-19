"""Prompt fidelity and the strict response contract."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models.domain import DocumentationBundle
from app.parsers.diff import analyse_diff
from app.services.llm.prompts import (
    DIFF_HEADER,
    DOCUMENTATION_HEADER,
    ROLE_INSTRUCTION,
    TASK_SECTION,
    render_diff_block,
    render_documentation_block,
    render_full_prompt,
)
from app.services.llm.schema import (
    DRIFT_VERDICT_JSON_SCHEMA,
    DriftStatus,
    DriftVerdict,
    local_verdict,
)

# --------------------------------------------------------------------------- #
# Prompt blueprint fidelity
# --------------------------------------------------------------------------- #


def test_role_instruction_matches_the_blueprint() -> None:
    assert ROLE_INSTRUCTION == (
        "You are an expert technical writer and code reviewer. Your job is to prevent "
        '"documentation drift" by ensuring documentation matches recent code updates.'
    )


def test_task_section_names_all_three_json_keys() -> None:
    for key in ('"status"', '"reason"', '"suggested_edit"'):
        assert key in TASK_SECTION
    assert '"UP_TO_DATE"' in TASK_SECTION
    assert '"NEEDS_UPDATE"' in TASK_SECTION
    assert TASK_SECTION.startswith("[TASK]")


def test_sections_appear_in_blueprint_order(
    documentation: DocumentationBundle, sample_diff: str
) -> None:
    prompt = render_full_prompt(documentation, analyse_diff(sample_diff))

    positions = [
        prompt.index(ROLE_INSTRUCTION),
        prompt.index(DOCUMENTATION_HEADER),
        prompt.index(DIFF_HEADER),
        prompt.index("[TASK]"),
    ]
    assert positions == sorted(positions), "prompt sections are out of blueprint order"


def test_documentation_block_carries_file_contents(
    documentation: DocumentationBundle,
) -> None:
    block = render_documentation_block(documentation)
    assert block.startswith(DOCUMENTATION_HEADER)
    assert "README.md" in block
    assert "DATABASE_URL" in block


def test_empty_documentation_renders_a_sentinel_not_a_blank() -> None:
    block = render_documentation_block(DocumentationBundle())
    assert "no documentation files were found" in block


def test_diff_block_lists_signals_as_hints_not_a_ceiling(sample_diff: str) -> None:
    block = render_diff_block(analyse_diff(sample_diff))
    assert block.startswith(DIFF_HEADER)
    assert "[STRUCTURAL SIGNALS]" in block
    assert "hints, not" in block
    assert "REDIS_URL" in block


def test_diff_block_discloses_truncation() -> None:
    huge = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n" + (
        "+line = 1\n" * 3000
    )
    block = render_diff_block(analyse_diff(huge, max_chars=400))
    assert "truncated" in block.lower()


def test_diff_block_names_omitted_noise_and_generated_files(sample_diff: str) -> None:
    block = render_diff_block(analyse_diff(sample_diff))
    assert "app/utils.py" in block  # dropped as formatting-only, but disclosed
    assert "poetry.lock" in block  # dropped as generated, but disclosed


# --------------------------------------------------------------------------- #
# JSON schema shape
# --------------------------------------------------------------------------- #


def test_schema_uses_only_supported_keywords() -> None:
    """Structured outputs rejects `$defs`, `$ref`, and length constraints."""
    serialised = json.dumps(DRIFT_VERDICT_JSON_SCHEMA)
    for unsupported in ("$defs", "$ref", "minLength", "maxLength", "minimum", "maximum"):
        assert unsupported not in serialised


def test_schema_forbids_extra_properties_and_requires_every_field() -> None:
    assert DRIFT_VERDICT_JSON_SCHEMA["additionalProperties"] is False
    assert set(DRIFT_VERDICT_JSON_SCHEMA["required"]) == {
        "status",
        "reason",
        "suggested_edit",
    }
    assert set(DRIFT_VERDICT_JSON_SCHEMA["properties"]) == set(
        DRIFT_VERDICT_JSON_SCHEMA["required"]
    )


def test_schema_status_enum_matches_the_python_enum() -> None:
    assert DRIFT_VERDICT_JSON_SCHEMA["properties"]["status"]["enum"] == [
        s.value for s in DriftStatus
    ]


# --------------------------------------------------------------------------- #
# Verdict parsing and normalisation
# --------------------------------------------------------------------------- #


def test_parses_a_well_formed_verdict() -> None:
    verdict = DriftVerdict.from_model_text(
        json.dumps(
            {
                "status": "NEEDS_UPDATE",
                "reason": "REDIS_URL is now required but undocumented.",
                "suggested_edit": "- `REDIS_URL` — Redis connection string.",
            }
        )
    )
    assert verdict.needs_update is True
    assert verdict.has_suggestion is True


def test_strips_an_accidental_markdown_fence() -> None:
    fenced = '```json\n{"status": "UP_TO_DATE", "reason": "ok", "suggested_edit": ""}\n```'
    assert DriftVerdict.from_model_text(fenced).status is DriftStatus.UP_TO_DATE


def test_up_to_date_with_a_stray_edit_is_reconciled_not_rejected() -> None:
    """The schema cannot enforce the semantic pairing; the model can.

    A usable verdict beats a failed review, so the edit is cleared rather than
    raising.
    """
    verdict = DriftVerdict(
        status=DriftStatus.UP_TO_DATE,
        reason="Docs already mention it.",
        suggested_edit="## Something",
    )
    assert verdict.suggested_edit == ""
    assert verdict.has_suggestion is False


def test_needs_update_edit_is_whitespace_trimmed() -> None:
    verdict = DriftVerdict(
        status=DriftStatus.NEEDS_UPDATE,
        reason="Missing env var.",
        suggested_edit="\n\n  ## Env\n\n",
    )
    assert verdict.suggested_edit == "## Env"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not json at all", "[1, 2, 3]", '{"status": "MAYBE", "reason": "x"}'],
)
def test_unusable_responses_raise(text: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        DriftVerdict.from_model_text(text)


def test_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DriftVerdict.from_model_text(
            json.dumps(
                {
                    "status": "UP_TO_DATE",
                    "reason": "fine",
                    "suggested_edit": "",
                    "confidence": 0.9,
                }
            )
        )


def test_empty_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DriftVerdict.from_model_text(
            json.dumps({"status": "UP_TO_DATE", "reason": "", "suggested_edit": ""})
        )


def test_local_verdict_is_always_clean() -> None:
    verdict = local_verdict("whitespace only")
    assert verdict.status is DriftStatus.UP_TO_DATE
    assert verdict.needs_update is False
    assert verdict.suggested_edit == ""


def test_verdicts_are_immutable() -> None:
    verdict = local_verdict("ok")
    with pytest.raises(ValidationError):
        verdict.reason = "changed"  # type: ignore[misc]
