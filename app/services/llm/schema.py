"""The structured response contract.

The JSON Schema is hand-written rather than derived from the Pydantic model so
it uses only the keywords the structured-outputs API accepts: no `$defs`, no
string-length constraints, `additionalProperties: false`, and every property
required. The Pydantic model then validates and normalises whatever comes back.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DriftStatus(StrEnum):
    """The two validation states the pipeline tracks."""

    UP_TO_DATE = "UP_TO_DATE"
    NEEDS_UPDATE = "NEEDS_UPDATE"


#: Sent as `output_config.format.schema`; the API constrains generation to it.
DRIFT_VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["UP_TO_DATE", "NEEDS_UPDATE"],
            "description": (
                "UP_TO_DATE if the documentation still describes the code accurately; "
                "NEEDS_UPDATE if this diff makes any part of it wrong or incomplete."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "A brief explanation of why an update is necessary, or why the existing "
                "documentation is already sufficient."
            ),
        },
        "suggested_edit": {
            "type": "string",
            "description": (
                "When status is NEEDS_UPDATE, the exact markdown snippet that should be "
                "added or updated. An empty string when status is UP_TO_DATE."
            ),
        },
    },
    "required": ["status", "reason", "suggested_edit"],
    "additionalProperties": False,
}


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical schema to Gemini's structured-output dialect.

    Gemini accepts an OpenAPI 3.0 subset, which has no `additionalProperties`,
    and it honours `propertyOrdering` to keep generated fields in a stable
    order. Derived from `DRIFT_VERDICT_JSON_SCHEMA` rather than hand-written so
    the two cannot drift apart.
    """
    adapted = {key: value for key, value in schema.items() if key != "additionalProperties"}
    adapted["propertyOrdering"] = list(schema["properties"])
    return adapted


#: Sent as `response_json_schema` on the Gemini request.
GEMINI_VERDICT_JSON_SCHEMA: dict[str, Any] = _to_gemini_schema(DRIFT_VERDICT_JSON_SCHEMA)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class DriftVerdict(BaseModel):
    """A validated evaluation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DriftStatus
    reason: str = Field(min_length=1)
    suggested_edit: str = ""

    @model_validator(mode="after")
    def _normalise(self) -> DriftVerdict:
        """Keep the two fields consistent with the status.

        Structured outputs make a malformed shape very unlikely, but the
        *semantic* pairing is not schema-enforceable: the model can still emit
        UP_TO_DATE alongside a suggested edit. Reconcile rather than reject —
        a usable verdict beats a failed review.
        """
        edit = self.suggested_edit.strip()
        if self.status is DriftStatus.UP_TO_DATE and edit:
            object.__setattr__(self, "suggested_edit", "")
        elif self.status is DriftStatus.NEEDS_UPDATE:
            object.__setattr__(self, "suggested_edit", edit)
        return self

    @property
    def needs_update(self) -> bool:
        return self.status is DriftStatus.NEEDS_UPDATE

    @property
    def has_suggestion(self) -> bool:
        return bool(self.suggested_edit.strip())

    @classmethod
    def from_model_text(cls, text: str) -> DriftVerdict:
        """Parse the model's text block into a verdict.

        Strips an accidental markdown fence before parsing — cheap insurance
        that costs nothing when structured outputs behave as expected.
        """
        cleaned = _FENCE_RE.sub("", text.strip()).strip()
        if not cleaned:
            raise ValueError("model returned an empty response body")
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"model response was not valid JSON ({exc}): {cleaned[:300]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        return cls.model_validate(payload)


def local_verdict(reason: str) -> DriftVerdict:
    """An UP_TO_DATE verdict decided by the parser, with no LLM call."""
    return DriftVerdict(status=DriftStatus.UP_TO_DATE, reason=reason, suggested_edit="")
