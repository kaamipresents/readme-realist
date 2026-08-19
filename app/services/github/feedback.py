"""Feedback Orchestrator.

Turns a `DriftVerdict` into what a reviewer actually sees on the pull request:

* **UP_TO_DATE** — a successful Check Run. If a previous run left a drift
  comment, it is edited in place to say the drift was resolved.
* **NEEDS_UPDATE** — the reason and suggested markdown posted as a PR comment,
  plus a Check Run whose conclusion is configurable (`neutral` by default, so it
  reports without blocking merge).

Comments are *upserted* against a hidden marker rather than appended. A PR that
gets pushed to ten times ends up with one comment reflecting the latest state,
not ten stale ones.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.models.domain import DiffAnalysis, PullRequestContext
from app.services.github.client import GitHubApiError, GitHubClient
from app.services.llm.schema import DriftVerdict

logger = logging.getLogger(__name__)

#: Invisible in rendered markdown; how we find our own previous comment.
BOT_MARKER = "<!-- readme-realist:v1 -->"

DEFAULT_CHECK_RUN_NAME = "ReadMe Realist / documentation drift"

_BACKTICK_RUN = re.compile(r"`+")


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    """What was published, for logging and assertions."""

    conclusion: str
    comment_id: int | None = None
    comment_url: str | None = None
    check_run_id: int | None = None
    comment_action: str | None = None  # "created" | "updated" | None


def _fence_for(content: str) -> str:
    """A code fence long enough to contain `content` verbatim."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(content)), default=0)
    return "`" * max(3, longest + 1)


def _render_signal_details(analysis: DiffAnalysis | None, limit: int = 25) -> str:
    if analysis is None or not analysis.signals:
        return ""
    lines = [signal.render() for signal in analysis.signals[:limit]]
    if len(analysis.signals) > limit:
        lines.append(f"- …and {len(analysis.signals) - limit} more")
    body = "\n".join(lines)
    return (
        "\n<details>\n<summary>Structural changes that triggered this review</summary>\n\n"
        f"{body}\n\n</details>\n"
    )


def render_drift_comment(
    ctx: PullRequestContext,
    verdict: DriftVerdict,
    analysis: DiffAnalysis | None = None,
    *,
    blocking: bool = False,
) -> str:
    """The PR comment shown when documentation is stale."""
    parts = [
        BOT_MARKER,
        "## 📝 Documentation drift detected",
        "",
        f"{verdict.reason.strip()}",
    ]

    if verdict.has_suggestion:
        fence = _fence_for(verdict.suggested_edit)
        parts += [
            "",
            "### Suggested documentation update",
            "",
            f"{fence}markdown",
            verdict.suggested_edit.strip(),
            fence,
        ]

    details = _render_signal_details(analysis)
    if details:
        parts += ["", details]

    gate = (
        "This check is **blocking** — update the docs to turn it green."
        if blocking
        else "This check is advisory and does not block merge."
    )
    parts += [
        "",
        "---",
        f"<sub>ReadMe Realist reviewed `{ctx.head_sha[:7]}`. {gate}</sub>",
    ]
    return "\n".join(parts)


def render_resolved_comment(ctx: PullRequestContext, verdict: DriftVerdict) -> str:
    """Replacement body once a previously-flagged PR comes back clean."""
    return "\n".join(
        [
            BOT_MARKER,
            "## ✅ Documentation is up to date",
            "",
            verdict.reason.strip(),
            "",
            "---",
            f"<sub>ReadMe Realist re-reviewed `{ctx.head_sha[:7]}`. "
            "The drift flagged earlier no longer applies.</sub>",
        ]
    )


class FeedbackOrchestrator:
    """Publishes verdicts to the pull request interface."""

    def __init__(
        self,
        github: GitHubClient,
        *,
        check_run_name: str = DEFAULT_CHECK_RUN_NAME,
        drift_conclusion: str = "neutral",
        post_comment: bool = True,
        publish_check_run: bool = True,
    ) -> None:
        self._github = github
        self._check_run_name = check_run_name
        self._drift_conclusion = drift_conclusion
        self._post_comment = post_comment
        self._publish_check_run = publish_check_run

    # ------------------------------------------------------------------ #

    async def start_check_run(self, ctx: PullRequestContext) -> int | None:
        """Mark the check in progress so the PR shows activity immediately."""
        if not self._publish_check_run:
            return None
        try:
            run = await self._github.create_check_run(
                ctx,
                name=self._check_run_name,
                status="in_progress",
                title="Checking documentation",
                summary="Comparing the diff against the repository's documentation…",
            )
        except Exception as exc:  # noqa: BLE001 - publishing is best-effort
            # `checks:write` may not be granted. Degrade to comment-only rather
            # than failing the review outright.
            logger.warning(
                "could not create check run",
                extra={"error": str(exc), **ctx.log_context()},
            )
            return None
        run_id = run.get("id")
        return int(run_id) if run_id is not None else None

    async def publish(
        self,
        ctx: PullRequestContext,
        verdict: DriftVerdict,
        *,
        analysis: DiffAnalysis | None = None,
        check_run_id: int | None = None,
    ) -> FeedbackResult:
        """Route the verdict to the right PR surface."""
        if verdict.needs_update:
            return await self._publish_drift(ctx, verdict, analysis, check_run_id)
        return await self._publish_clean(ctx, verdict, check_run_id)

    async def publish_skipped(
        self,
        ctx: PullRequestContext,
        *,
        title: str,
        summary: str,
        check_run_id: int | None = None,
        conclusion: str = "success",
    ) -> FeedbackResult:
        """Close out a review that never needed an LLM call."""
        await self._finish_check(
            ctx, check_run_id, conclusion=conclusion, title=title, summary=summary
        )
        return FeedbackResult(conclusion=conclusion, check_run_id=check_run_id)

    async def publish_failure(
        self,
        ctx: PullRequestContext,
        error: str,
        *,
        check_run_id: int | None = None,
    ) -> FeedbackResult:
        """Report our own failure without pretending the docs are fine.

        Deliberately `neutral`, never `failure`: a bug in this tool must not
        block somebody else's merge.
        """
        await self._finish_check(
            ctx,
            check_run_id,
            conclusion="neutral",
            title="Documentation check could not complete",
            summary=(
                "ReadMe Realist hit an error while reviewing this pull request, so no "
                "documentation verdict was produced.\n\n"
                f"```\n{error[:1500]}\n```"
            ),
        )
        return FeedbackResult(conclusion="neutral", check_run_id=check_run_id)

    # ------------------------------------------------------------------ #

    async def _publish_drift(
        self,
        ctx: PullRequestContext,
        verdict: DriftVerdict,
        analysis: DiffAnalysis | None,
        check_run_id: int | None,
    ) -> FeedbackResult:
        comment_id: int | None = None
        comment_url: str | None = None
        comment_action: str | None = None

        if self._post_comment:
            body = render_drift_comment(
                ctx, verdict, analysis, blocking=self._drift_conclusion == "failure"
            )
            comment, comment_action = await self._upsert_comment(ctx, body)
            if comment is not None:
                raw_id = comment.get("id")
                comment_id = int(raw_id) if raw_id is not None else None
                comment_url = comment.get("html_url")

        summary = verdict.reason.strip()
        text = None
        if verdict.has_suggestion:
            fence = _fence_for(verdict.suggested_edit)
            text = (
                "### Suggested documentation update\n\n"
                f"{fence}markdown\n{verdict.suggested_edit.strip()}\n{fence}"
            )

        await self._finish_check(
            ctx,
            check_run_id,
            conclusion=self._drift_conclusion,
            title="Documentation needs updating",
            summary=summary,
            text=text,
        )

        return FeedbackResult(
            conclusion=self._drift_conclusion,
            comment_id=comment_id,
            comment_url=comment_url,
            check_run_id=check_run_id,
            comment_action=comment_action,
        )

    async def _publish_clean(
        self,
        ctx: PullRequestContext,
        verdict: DriftVerdict,
        check_run_id: int | None,
    ) -> FeedbackResult:
        comment_action: str | None = None
        comment_id: int | None = None

        # Only touch comments if we already left one — never open a thread just
        # to say "all good".
        if self._post_comment:
            existing = await self._find_existing_comment(ctx)
            if existing is not None:
                raw_id = existing.get("id")
                if raw_id is not None:
                    comment_id = int(raw_id)
                    try:
                        await self._github.update_issue_comment(
                            ctx, comment_id, render_resolved_comment(ctx, verdict)
                        )
                        comment_action = "updated"
                    except GitHubApiError as exc:
                        logger.warning(
                            "could not update resolved comment",
                            extra={"error": str(exc), **ctx.log_context()},
                        )

        await self._finish_check(
            ctx,
            check_run_id,
            conclusion="success",
            title="Documentation is up to date",
            summary=verdict.reason.strip(),
        )

        return FeedbackResult(
            conclusion="success",
            comment_id=comment_id,
            check_run_id=check_run_id,
            comment_action=comment_action,
        )

    # ------------------------------------------------------------------ #

    async def _find_existing_comment(self, ctx: PullRequestContext) -> dict[str, Any] | None:
        try:
            comments = await self._github.list_issue_comments(ctx)
        except Exception as exc:  # noqa: BLE001 - publishing is best-effort
            logger.warning(
                "could not list PR comments; will post a fresh one",
                extra={"error": str(exc), **ctx.log_context()},
            )
            return None
        for comment in reversed(comments):
            if BOT_MARKER in str(comment.get("body", "")):
                return comment
        return None

    async def _upsert_comment(
        self, ctx: PullRequestContext, body: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        existing = await self._find_existing_comment(ctx)
        try:
            if existing is not None and existing.get("id") is not None:
                updated = await self._github.update_issue_comment(ctx, int(existing["id"]), body)
                return updated, "updated"
            created = await self._github.create_issue_comment(ctx, body)
            return created, "created"
        except Exception as exc:  # noqa: BLE001 - publishing is best-effort
            logger.error(
                "failed to publish PR comment",
                extra={"error": str(exc), **ctx.log_context()},
            )
            return None, None

    async def _finish_check(
        self,
        ctx: PullRequestContext,
        check_run_id: int | None,
        *,
        conclusion: str,
        title: str,
        summary: str,
        text: str | None = None,
    ) -> None:
        if not self._publish_check_run:
            return
        try:
            if check_run_id is None:
                await self._github.create_check_run(
                    ctx,
                    name=self._check_run_name,
                    status="completed",
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                    text=text,
                )
            else:
                await self._github.update_check_run(
                    ctx,
                    check_run_id,
                    status="completed",
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                    text=text,
                )
        except Exception as exc:  # noqa: BLE001 - publishing is best-effort
            # Nothing above this call can recover, and it is frequently invoked
            # from the failure path itself — raising here would mask the
            # original error and break `ReviewPipeline.review`'s no-raise
            # contract.
            logger.warning(
                "could not finalise check run",
                extra={"error": str(exc), **ctx.log_context()},
            )
