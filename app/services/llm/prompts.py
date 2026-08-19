"""Prompt construction for the documentation-drift evaluation.

The prompt blueprint is implemented verbatim. It is split across the API's
`system` field and two user content blocks purely so a cache breakpoint can sit
between the documentation (stable across pushes to the same PR) and the diff
(different on every push). Rendered in order, the model still sees exactly:

    <role instruction>
    [EXISTING DOCUMENTATION]
    ...
    [INCOMING CODE DIFF]
    ...
    [TASK]
    ...

`render_full_prompt` reassembles that literal form for debugging and tests.
"""

from __future__ import annotations

from app.models.domain import DiffAnalysis, DocumentationBundle

ROLE_INSTRUCTION = (
    "You are an expert technical writer and code reviewer. Your job is to prevent "
    '"documentation drift" by ensuring documentation matches recent code updates.'
)

DOCUMENTATION_HEADER = "[EXISTING DOCUMENTATION]"
DIFF_HEADER = "[INCOMING CODE DIFF]"

TASK_SECTION = """[TASK]
1. Read the code diff carefully. Look for structural alterations such as modified \
environment variables, installation instructions, execution flags, or changed endpoints.
2. Determine if the current documentation remains accurate or if it misses vital updates \
introduced by this diff.
3. Output your response in a strict JSON format with the following keys:
   - "status": Either "UP_TO_DATE" or "NEEDS_UPDATE".
   - "reason": A brief explanation explaining why an update is necessary or why it is \
already sufficient.
   - "suggested_edit": If "NEEDS_UPDATE", provide the exact markdown snippet that should be \
added or updated. Otherwise, leave this string empty."""

#: Appended to the diff block. Static-parser output is offered as a hint, not as
#: a substitute for the model reading the diff — the parser's regexes have
#: blind spots and must never cap what the model is willing to flag.
_SIGNALS_PREAMBLE = (
    "The following structural signals were isolated from the diff by a static parser. "
    "Treat them as hints, not as an exhaustive list — judge the diff on its own merits."
)

_TRUNCATION_NOTICE = (
    "NOTE: this diff was truncated to fit the context budget. Base your judgement on "
    "what is shown, and say so in your reason if the truncation limits your confidence."
)


def render_documentation_block(documentation: DocumentationBundle) -> str:
    """The `[EXISTING DOCUMENTATION]` section."""
    return f"{DOCUMENTATION_HEADER}\n{documentation.render()}"


def render_diff_block(analysis: DiffAnalysis) -> str:
    """The `[INCOMING CODE DIFF]` section, plus isolated structural signals."""
    parts = [DIFF_HEADER]

    diff_text = analysis.filtered_diff.strip() or "(no substantive code changes)"
    parts.append(diff_text)

    if analysis.truncated:
        parts.append(_TRUNCATION_NOTICE)

    if analysis.noise_only_paths:
        listed = ", ".join(f"`{p}`" for p in analysis.noise_only_paths[:20])
        parts.append(f"Files omitted as whitespace/formatting-only changes: {listed}")

    if analysis.excluded_paths:
        listed = ", ".join(f"`{p}`" for p in analysis.excluded_paths[:20])
        parts.append(f"Generated or vendored files omitted: {listed}")

    parts.append(f"[STRUCTURAL SIGNALS]\n{_SIGNALS_PREAMBLE}\n\n{analysis.render_signals()}")

    return "\n\n".join(parts)


def render_task_block() -> str:
    """The `[TASK]` section, verbatim."""
    return TASK_SECTION


def render_full_prompt(documentation: DocumentationBundle, analysis: DiffAnalysis) -> str:
    """The complete prompt as one string, in blueprint order.

    Used by tests to assert section ordering and by `--dry-run` style debugging;
    the evaluator sends the same content split across system/user blocks.
    """
    return "\n\n".join(
        [
            ROLE_INSTRUCTION,
            render_documentation_block(documentation),
            render_diff_block(analysis),
            render_task_block(),
        ]
    )
