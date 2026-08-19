"""End-to-end pipeline behaviour, with GitHub and Claude both faked."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.models.domain import (
    DocumentationBundle,
    DocumentFile,
    OutcomeKind,
    PullRequestContext,
)
from app.services.github.feedback import FeedbackOrchestrator
from app.services.llm.evaluator import DriftEvaluator
from app.services.orchestrator import ReviewPipeline
from tests.conftest import FakeAnthropicClient, FakeGitHubClient, make_anthropic_response

NEEDS_UPDATE = {
    "status": "NEEDS_UPDATE",
    "reason": "REDIS_URL is required at startup but the README never mentions it.",
    "suggested_edit": "- `REDIS_URL` — Redis connection string.",
}
UP_TO_DATE = {
    "status": "UP_TO_DATE",
    "reason": "The README already documents every changed setting.",
    "suggested_edit": "",
}

DOCS = DocumentationBundle(
    files=(DocumentFile(path="README.md", content="# Widget\n\nSet `DATABASE_URL`.\n"),)
)

WHITESPACE_DIFF = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
    "-  a = 1\n-  b = 2\n+    a = 1\n+    b = 2\n"
)
DOCS_ONLY_DIFF = (
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
    "@@ -1 +1,2 @@\n # Widget\n+Now with Redis.\n"
)
CODE_DIFF = (
    "diff --git a/app/config.py b/app/config.py\n--- a/app/config.py\n+++ b/app/config.py\n"
    '@@ -1 +1,2 @@\n import os\n+REDIS_URL = os.environ["REDIS_URL"]\n'
)


def _build(
    settings: Settings,
    *,
    diff: str,
    documentation: DocumentationBundle = DOCS,
    response: object | None = None,
    llm_error: Exception | None = None,
) -> tuple[ReviewPipeline, FakeGitHubClient, FakeAnthropicClient]:
    github = FakeGitHubClient(diff=diff, documentation=documentation)
    anthropic = FakeAnthropicClient(
        response if response is not None else make_anthropic_response(UP_TO_DATE),
        error=llm_error,
    )
    pipeline = ReviewPipeline(
        github=github,  # type: ignore[arg-type]
        evaluator=DriftEvaluator(client=anthropic, model=settings.anthropic_model),
        feedback=FeedbackOrchestrator(
            github,  # type: ignore[arg-type]
            drift_conclusion=settings.drift_check_conclusion,
        ),
        settings=settings,
    )
    return pipeline, github, anthropic


# --------------------------------------------------------------------------- #
# The drift path
# --------------------------------------------------------------------------- #


async def test_a_new_env_var_without_docs_is_flagged(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, anthropic = _build(
        settings, diff=CODE_DIFF, response=make_anthropic_response(NEEDS_UPDATE)
    )
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.EVALUATED
    assert outcome.verdict_status == "NEEDS_UPDATE"
    assert len(anthropic.calls) == 1
    assert len(github.created_comments) == 1
    assert "REDIS_URL" in github.created_comments[0]


async def test_documented_change_passes_cleanly(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, anthropic = _build(
        settings, diff=CODE_DIFF, response=make_anthropic_response(UP_TO_DATE)
    )
    outcome = await pipeline.review(pr_context)

    assert len(anthropic.calls) == 1
    assert outcome.verdict_status == "UP_TO_DATE"
    assert github.created_comments == []
    assert github.updated_check_runs[-1][1]["conclusion"] == "success"


async def test_the_check_run_opens_in_progress_then_completes(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, _ = _build(settings, diff=CODE_DIFF)
    await pipeline.review(pr_context)

    assert github.created_check_runs[0]["status"] == "in_progress"
    assert github.updated_check_runs[-1][1]["status"] == "completed"


# --------------------------------------------------------------------------- #
# Local short-circuits — the cost control
# --------------------------------------------------------------------------- #


async def test_whitespace_only_pr_never_reaches_the_model(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, anthropic = _build(settings, diff=WHITESPACE_DIFF)
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.SKIPPED_NOISE_ONLY
    assert anthropic.calls == [], "an Opus call on a whitespace PR is pure cost"
    assert github.updated_check_runs[-1][1]["conclusion"] == "success"


async def test_noise_short_circuit_can_be_disabled(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    settings = settings.model_copy(update={"skip_llm_on_noise_only": False})
    pipeline, _, anthropic = _build(settings, diff=WHITESPACE_DIFF)
    outcome = await pipeline.review(pr_context)

    # With the short-circuit off it falls through to the docs-only rule, since
    # a whitespace-only diff still contains no code changes.
    assert outcome.kind is OutcomeKind.SKIPPED_DOCS_ONLY
    assert anthropic.calls == []


async def test_documentation_only_pr_is_skipped(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, _, anthropic = _build(settings, diff=DOCS_ONLY_DIFF)
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.SKIPPED_DOCS_ONLY
    assert anthropic.calls == []


async def test_empty_diff_is_skipped(settings: Settings, pr_context: PullRequestContext) -> None:
    pipeline, _, anthropic = _build(settings, diff="")
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.SKIPPED_NO_FILES
    assert anthropic.calls == []


async def test_code_changes_with_no_matched_signals_still_reach_the_model(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    """The regexes have blind spots; under-calling would ship stale docs."""
    diff = (
        "diff --git a/app/core.py b/app/core.py\n--- a/app/core.py\n+++ b/app/core.py\n"
        "@@ -1 +1,2 @@\n def run():\n+    return compute_totals(strategy='weighted')\n"
    )
    pipeline, _, anthropic = _build(settings, diff=diff)
    outcome = await pipeline.review(pr_context)

    assert outcome.signals == ()
    assert len(anthropic.calls) == 1
    assert outcome.kind is OutcomeKind.EVALUATED


# --------------------------------------------------------------------------- #
# Missing documentation
# --------------------------------------------------------------------------- #


async def test_repository_with_no_docs_reports_neutral_without_nagging(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, anthropic = _build(
        settings, diff=CODE_DIFF, documentation=DocumentationBundle()
    )
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.SKIPPED_NO_DOCUMENTATION
    assert anthropic.calls == []
    assert github.created_comments == []
    assert github.updated_check_runs[-1][1]["conclusion"] == "neutral"


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


async def test_an_llm_failure_yields_a_neutral_check_not_an_exception(
    settings: Settings, pr_context: PullRequestContext
) -> None:
    pipeline, github, _ = _build(settings, diff=CODE_DIFF, llm_error=RuntimeError("upstream 529"))
    outcome = await pipeline.review(pr_context)

    assert outcome.kind is OutcomeKind.FAILED
    assert outcome.error is not None and "529" in outcome.error
    assert github.updated_check_runs[-1][1]["conclusion"] == "neutral"


async def test_a_github_failure_is_contained(
    settings: Settings, pr_context: PullRequestContext, monkeypatch
) -> None:
    pipeline, github, _ = _build(settings, diff=CODE_DIFF)

    async def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("diff fetch exploded")

    monkeypatch.setattr(github, "fetch_pull_request_diff", boom)

    outcome = await pipeline.review(pr_context)
    assert outcome.kind is OutcomeKind.FAILED
    assert github.updated_check_runs[-1][1]["conclusion"] == "neutral"


async def test_review_never_raises_even_when_everything_fails(
    settings: Settings, pr_context: PullRequestContext, monkeypatch
) -> None:
    """It runs at the top of a background task — an escaping exception is lost."""
    pipeline, github, _ = _build(settings, diff=CODE_DIFF)

    async def boom(*args: object, **kwargs: object):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(github, "fetch_pull_request_diff", boom)
    monkeypatch.setattr(github, "create_check_run", boom)
    monkeypatch.setattr(github, "update_check_run", boom)

    outcome = await pipeline.review(pr_context)
    assert outcome.kind is OutcomeKind.FAILED


# --------------------------------------------------------------------------- #
# Configuration is honoured
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("conclusion", ["neutral", "failure", "success"])
async def test_drift_conclusion_is_configurable(
    settings: Settings, pr_context: PullRequestContext, conclusion: str
) -> None:
    settings = settings.model_copy(update={"drift_check_conclusion": conclusion})
    pipeline, github, _ = _build(
        settings, diff=CODE_DIFF, response=make_anthropic_response(NEEDS_UPDATE)
    )
    await pipeline.review(pr_context)

    assert github.updated_check_runs[-1][1]["conclusion"] == conclusion
