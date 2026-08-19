"""The Feedback Orchestrator: what a reviewer actually sees on the PR."""

from __future__ import annotations

import pytest

from app.models.domain import PullRequestContext
from app.parsers.diff import analyse_diff
from app.services.github.client import GitHubApiError
from app.services.github.feedback import (
    BOT_MARKER,
    FeedbackOrchestrator,
    _fence_for,
    render_drift_comment,
)
from app.services.llm.schema import DriftStatus, DriftVerdict
from tests.conftest import FakeGitHubClient

DRIFT = DriftVerdict(
    status=DriftStatus.NEEDS_UPDATE,
    reason="REDIS_URL is read at startup but the README never mentions it.",
    suggested_edit="### Environment\n\n- `REDIS_URL` — Redis connection string.",
)
CLEAN = DriftVerdict(
    status=DriftStatus.UP_TO_DATE,
    reason="The change is internal; the setup instructions still hold.",
)


def _orchestrator(github: FakeGitHubClient, **kwargs: object) -> FeedbackOrchestrator:
    return FeedbackOrchestrator(github, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# NEEDS_UPDATE
# --------------------------------------------------------------------------- #


async def test_drift_posts_a_comment_and_a_neutral_check(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    result = await _orchestrator(fake_github).publish(pr_context, DRIFT, check_run_id=1)

    assert result.conclusion == "neutral"
    assert result.comment_action == "created"
    assert len(fake_github.created_comments) == 1

    body = fake_github.created_comments[0]
    assert BOT_MARKER in body
    assert DRIFT.reason in body
    assert "REDIS_URL" in body
    assert "does not block merge" in body

    (_, payload) = fake_github.updated_check_runs[0]
    assert payload["conclusion"] == "neutral"
    assert payload["status"] == "completed"


async def test_failure_conclusion_marks_the_comment_as_blocking(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    await _orchestrator(fake_github, drift_conclusion="failure").publish(
        pr_context, DRIFT, check_run_id=1
    )
    body = fake_github.created_comments[0]
    assert "**blocking**" in body
    assert fake_github.updated_check_runs[0][1]["conclusion"] == "failure"


async def test_the_suggested_edit_is_carried_into_the_check_run(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    await _orchestrator(fake_github).publish(pr_context, DRIFT, check_run_id=1)
    text = fake_github.updated_check_runs[0][1]["text"]
    assert "REDIS_URL" in text


async def test_signals_are_disclosed_in_a_details_block(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient, sample_diff: str
) -> None:
    analysis = analyse_diff(sample_diff)
    await _orchestrator(fake_github).publish(pr_context, DRIFT, analysis=analysis, check_run_id=1)
    body = fake_github.created_comments[0]
    assert "<details>" in body
    assert "environment_variable" in body


# --------------------------------------------------------------------------- #
# Comment upsert
# --------------------------------------------------------------------------- #


async def test_a_second_push_edits_the_comment_instead_of_adding_one(
    pr_context: PullRequestContext,
) -> None:
    github = FakeGitHubClient(
        existing_comments=[{"id": 500, "body": f"{BOT_MARKER}\n## 📝 Documentation drift detected"}]
    )
    result = await _orchestrator(github).publish(pr_context, DRIFT, check_run_id=1)

    assert result.comment_action == "updated"
    assert github.created_comments == []
    assert github.updated_comments[0][0] == 500


async def test_other_peoples_comments_are_left_alone(
    pr_context: PullRequestContext,
) -> None:
    github = FakeGitHubClient(
        existing_comments=[
            {"id": 1, "body": "LGTM, ship it"},
            {"id": 2, "body": "did you remember the docs?"},
        ]
    )
    await _orchestrator(github).publish(pr_context, DRIFT, check_run_id=1)

    assert github.updated_comments == []
    assert len(github.created_comments) == 1


# --------------------------------------------------------------------------- #
# UP_TO_DATE
# --------------------------------------------------------------------------- #


async def test_clean_verdict_never_opens_a_comment_thread(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    result = await _orchestrator(fake_github).publish(pr_context, CLEAN, check_run_id=1)

    assert fake_github.created_comments == []
    assert fake_github.updated_comments == []
    assert result.conclusion == "success"
    assert fake_github.updated_check_runs[0][1]["conclusion"] == "success"


async def test_a_resolved_drift_rewrites_the_earlier_comment(
    pr_context: PullRequestContext,
) -> None:
    github = FakeGitHubClient(
        existing_comments=[{"id": 500, "body": f"{BOT_MARKER}\n## 📝 Documentation drift"}]
    )
    result = await _orchestrator(github).publish(pr_context, CLEAN, check_run_id=1)

    assert result.comment_action == "updated"
    body = github.updated_comments[0][1]
    assert "up to date" in body.lower()
    assert BOT_MARKER in body


# --------------------------------------------------------------------------- #
# Skips and failures
# --------------------------------------------------------------------------- #


async def test_a_skipped_review_still_closes_the_check(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    result = await _orchestrator(fake_github).publish_skipped(
        pr_context, title="No documentation impact", summary="whitespace only", check_run_id=9
    )
    assert result.conclusion == "success"
    assert fake_github.updated_check_runs[0][1]["title"] == "No documentation impact"
    assert fake_github.created_comments == []


async def test_our_own_failure_is_neutral_never_a_build_break(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    """A bug in this tool must not block somebody else's merge."""
    result = await _orchestrator(fake_github, drift_conclusion="failure").publish_failure(
        pr_context, "the model timed out", check_run_id=3
    )

    assert result.conclusion == "neutral"
    payload = fake_github.updated_check_runs[0][1]
    assert payload["conclusion"] == "neutral"
    assert "the model timed out" in payload["summary"]


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


async def test_missing_checks_permission_degrades_to_comment_only(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient, monkeypatch
) -> None:
    async def deny(*args: object, **kwargs: object) -> dict[str, object]:
        raise GitHubApiError("Resource not accessible by integration", status_code=403)

    monkeypatch.setattr(fake_github, "create_check_run", deny)

    orchestrator = _orchestrator(fake_github)
    check_run_id = await orchestrator.start_check_run(pr_context)
    assert check_run_id is None

    result = await orchestrator.publish(pr_context, DRIFT, check_run_id=None)
    assert len(fake_github.created_comments) == 1
    assert result.comment_action == "created"


async def test_comment_failure_does_not_raise(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient, monkeypatch
) -> None:
    async def deny(*args: object, **kwargs: object) -> dict[str, object]:
        raise GitHubApiError("Resource not accessible by integration", status_code=403)

    monkeypatch.setattr(fake_github, "create_issue_comment", deny)

    result = await _orchestrator(fake_github).publish(pr_context, DRIFT, check_run_id=1)
    assert result.comment_id is None
    assert fake_github.updated_check_runs, "the check run should still be published"


async def test_comments_can_be_disabled(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    await _orchestrator(fake_github, post_comment=False).publish(pr_context, DRIFT, check_run_id=1)
    assert fake_github.created_comments == []
    assert fake_github.updated_check_runs


async def test_check_runs_can_be_disabled(
    pr_context: PullRequestContext, fake_github: FakeGitHubClient
) -> None:
    orchestrator = _orchestrator(fake_github, publish_check_run=False)
    assert await orchestrator.start_check_run(pr_context) is None
    await orchestrator.publish(pr_context, DRIFT, check_run_id=None)

    assert fake_github.created_check_runs == []
    assert fake_github.updated_check_runs == []
    assert len(fake_github.created_comments) == 1


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain text", "```"),
        ("has ``` a fence", "````"),
        ("has ```` four", "`````"),
    ],
)
def test_fences_are_long_enough_to_contain_the_snippet(content: str, expected: str) -> None:
    assert _fence_for(content) == expected


def test_a_suggestion_containing_a_fence_is_not_broken(
    pr_context: PullRequestContext,
) -> None:
    """A suggested edit is markdown and can legitimately contain code fences."""
    verdict = DriftVerdict(
        status=DriftStatus.NEEDS_UPDATE,
        reason="Install steps changed.",
        suggested_edit="Run:\n\n```bash\npip install -e .\n```",
    )
    body = render_drift_comment(pr_context, verdict)

    assert "````markdown" in body
    assert "```bash" in body


def test_the_short_sha_is_shown_not_the_full_one(pr_context: PullRequestContext) -> None:
    body = render_drift_comment(pr_context, DRIFT)
    assert f"`{'a' * 7}`" in body
