"""The review pipeline.

    diff → Code Delta Parser → documentation → LLM → PR feedback

Every early exit is deliberate. Calling a frontier model on a whitespace-only
PR is pure cost, so the parser resolves those locally; anything with real code
in it goes to the model, even when the static signal scan came up empty (those
regexes have blind spots, and a missed drift is the failure that matters).
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.models.domain import (
    DiffAnalysis,
    DocumentationBundle,
    OutcomeKind,
    PullRequestContext,
    ReviewOutcome,
)
from app.parsers.diff import analyse_diff
from app.services.github.client import GitHubClient
from app.services.github.feedback import FeedbackOrchestrator
from app.services.llm.evaluator import EvaluationError, SupportsDriftEvaluation
from app.services.llm.schema import local_verdict

logger = logging.getLogger(__name__)


class ReviewPipeline:
    """Runs one pull request end to end."""

    def __init__(
        self,
        *,
        github: GitHubClient,
        evaluator: SupportsDriftEvaluation,
        feedback: FeedbackOrchestrator,
        settings: Settings,
    ) -> None:
        self._github = github
        self._evaluator = evaluator
        self._feedback = feedback
        self._settings = settings

    async def review(self, ctx: PullRequestContext) -> ReviewOutcome:
        """Never raises — failures come back as an outcome and a neutral check."""
        check_run_id: int | None = None
        try:
            check_run_id = await self._feedback.start_check_run(ctx)
            return await self._run(ctx, check_run_id)
        except Exception as exc:
            logger.exception("review failed", extra=ctx.log_context())
            # Reporting the failure can itself fail (a revoked token takes both
            # calls down together). Swallow that too: the outcome below is what
            # the caller relies on, and this method must never raise.
            try:
                await self._feedback.publish_failure(ctx, str(exc), check_run_id=check_run_id)
            except Exception:
                logger.exception("could not report failure", extra=ctx.log_context())
            return ReviewOutcome(
                kind=OutcomeKind.FAILED,
                context=ctx,
                summary="review failed",
                check_run_id=check_run_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #

    async def _run(self, ctx: PullRequestContext, check_run_id: int | None) -> ReviewOutcome:
        settings = self._settings

        # --- Stage 1: the diff ------------------------------------------
        diff_text = await self._github.fetch_pull_request_diff(ctx)
        analysis = analyse_diff(diff_text, max_chars=settings.diff_max_chars)

        logger.info(
            "diff analysed",
            extra={
                "files": len(analysis.files),
                "substantive_files": len(analysis.substantive_files),
                "noise_files": len(analysis.noise_only_paths),
                "excluded_files": len(analysis.excluded_paths),
                "signals": len(analysis.signals),
                **ctx.log_context(),
            },
        )

        skip = self._should_skip(analysis)
        if skip is not None:
            kind, title, summary = skip
            await self._feedback.publish_skipped(
                ctx, title=title, summary=summary, check_run_id=check_run_id
            )
            return ReviewOutcome(
                kind=kind,
                context=ctx,
                summary=summary,
                verdict_status="UP_TO_DATE",
                check_run_id=check_run_id,
                signals=analysis.signals,
            )

        # --- Stage 2: the documentation ---------------------------------
        documentation = await self._github.fetch_documentation(
            ctx,
            globs=tuple(settings.docs_globs),
            max_files=settings.docs_max_files,
            max_total_chars=settings.docs_max_total_chars,
            max_file_chars=settings.docs_max_file_chars,
        )

        if documentation.is_empty:
            summary = (
                "No documentation matched "
                f"`{', '.join(settings.docs_globs)}` on this branch, so there is "
                "nothing to check the diff against."
            )
            await self._feedback.publish_skipped(
                ctx,
                title="No documentation found",
                summary=summary,
                check_run_id=check_run_id,
                conclusion="neutral",
            )
            return ReviewOutcome(
                kind=OutcomeKind.SKIPPED_NO_DOCUMENTATION,
                context=ctx,
                summary=summary,
                check_run_id=check_run_id,
                signals=analysis.signals,
            )

        logger.info(
            "documentation retrieved",
            extra={
                "doc_files": len(documentation.files),
                "doc_chars": documentation.total_chars,
                "doc_truncated": documentation.truncated,
                **ctx.log_context(),
            },
        )

        # --- Stage 3: semantic verification -----------------------------
        try:
            evaluation = await self._evaluator.evaluate(documentation, analysis)
        except EvaluationError as exc:
            logger.error("evaluation failed", extra={"error": str(exc), **ctx.log_context()})
            await self._feedback.publish_failure(ctx, str(exc), check_run_id=check_run_id)
            return ReviewOutcome(
                kind=OutcomeKind.FAILED,
                context=ctx,
                summary="evaluation failed",
                check_run_id=check_run_id,
                signals=analysis.signals,
                error=str(exc),
            )

        # --- Stage 4: feedback ------------------------------------------
        result = await self._feedback.publish(
            ctx, evaluation.verdict, analysis=analysis, check_run_id=check_run_id
        )

        logger.info(
            "review published",
            extra={
                "status": evaluation.verdict.status.value,
                "conclusion": result.conclusion,
                "comment_action": result.comment_action,
                **evaluation.usage.as_dict(),
                **ctx.log_context(),
            },
        )

        return ReviewOutcome(
            kind=OutcomeKind.EVALUATED,
            context=ctx,
            summary=evaluation.verdict.reason,
            verdict_status=evaluation.verdict.status.value,
            comment_url=result.comment_url,
            check_run_id=check_run_id,
            signals=analysis.signals,
        )

    # ------------------------------------------------------------------ #

    def _should_skip(self, analysis: DiffAnalysis) -> tuple[OutcomeKind, str, str] | None:
        """Decide whether this diff is worth an LLM call.

        Returns `(outcome kind, check title, summary)` when the review can be
        resolved locally, or None to proceed to evaluation.
        """
        if not analysis.files:
            return (
                OutcomeKind.SKIPPED_NO_FILES,
                "No changes to review",
                "This pull request's diff contains no file changes.",
            )

        if self._settings.skip_llm_on_noise_only and analysis.is_noise_only:
            return (
                OutcomeKind.SKIPPED_NOISE_ONLY,
                "No documentation impact",
                "Every change in this pull request normalises to whitespace or "
                "formatting, so no documentation can have drifted.",
            )

        if not analysis.has_code_changes:
            docs_changed = ", ".join(f"`{f.path}`" for f in analysis.substantive_files[:10])
            return (
                OutcomeKind.SKIPPED_DOCS_ONLY,
                "Documentation-only change",
                "This pull request only touches documentation "
                f"({docs_changed or 'no code files'}), so there is no code change "
                "that could have made it stale.",
            )

        return None


def build_local_outcome_verdict(summary: str) -> object:
    """Convenience for callers that want the same verdict shape as the LLM path."""
    return local_verdict(summary)


__all__ = ["DocumentationBundle", "ReviewPipeline", "build_local_outcome_verdict"]
