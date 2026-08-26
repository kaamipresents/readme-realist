"""Command-line entry point.

    python -m app.cli review <owner/repo> <pr-number>

Runs exactly the same `ReviewPipeline` the webhook server runs, but reaches
GitHub with a plain token instead of a GitHub App JWT. That difference is the
whole point: a GitHub Actions runner already holds a usable `GITHUB_TOKEN`, so
this path needs no App registration, no webhook, no tunnel, and no server.

Two shapes of use:

* ``--dry-run`` prints the verdict and writes nothing to the pull request.
  This is the offline-safe way to try the tool against a real PR.
* Without it, the comment and Check Run are published exactly as the server
  would publish them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

import httpx

from app import __version__
from app.config import GitHubAuthMode, LLMProvider, Settings
from app.logging_config import configure_logging
from app.models.domain import OutcomeKind, PullRequestContext, ReviewOutcome
from app.services.github.auth import StaticTokenAuth
from app.services.github.client import GitHubClient
from app.services.github.feedback import FeedbackOrchestrator
from app.services.llm.factory import build_backend
from app.services.orchestrator import ReviewPipeline

logger = logging.getLogger(__name__)

#: Returned when drift is found *and* the caller asked for that to be fatal.
EXIT_DRIFT = 1
#: Returned when the review could not be completed at all.
EXIT_ERROR = 2

_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


class CliError(RuntimeError):
    """A problem worth reporting as a clean message rather than a traceback."""


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Check a pull request's documentation against its code changes.",
    )
    parser.add_argument("--version", action="version", version=f"readme-realist {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    review = subcommands.add_parser(
        "review",
        help="Review one pull request.",
        description=(
            "Fetch a pull request's diff and documentation, evaluate them for "
            "drift, and (unless --dry-run) publish the verdict to the PR."
        ),
    )
    review.add_argument("repository", metavar="OWNER/REPO", help="e.g. octocat/hello-world")
    review.add_argument("pull_number", metavar="PR", type=int, help="Pull request number")
    review.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the verdict without posting a comment or Check Run.",
    )
    review.add_argument(
        "--fail-on-drift",
        action="store_true",
        help=(
            "Exit non-zero when documentation is stale. Off by default so the "
            "tool reports without breaking a build until you have tuned it."
        ),
    )
    review.add_argument(
        "--token",
        default=None,
        help=(
            "GitHub token. Defaults to $GITHUB_TOKEN or $GH_TOKEN, which a "
            "GitHub Actions runner supplies automatically."
        ),
    )
    review.add_argument(
        "--docs",
        default=None,
        metavar="GLOBS",
        help="Comma-separated documentation globs; overrides DOCS_GLOBS.",
    )
    review.add_argument(
        "--provider",
        choices=[provider.value for provider in LLMProvider],
        default=None,
        help="Model backend; overrides LLM_PROVIDER.",
    )
    return parser


def split_repository(value: str) -> tuple[str, str]:
    owner, separator, name = value.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise CliError(f"repository must be in OWNER/REPO form, got {value!r}")
    return owner, name


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in _TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    raise CliError(
        "no GitHub token found — pass --token, or set "
        f"{' or '.join(f'${name}' for name in _TOKEN_ENV_VARS)}. "
        "Inside a GitHub Actions workflow, set: env: { GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }} }"
    )


def build_settings(args: argparse.Namespace, token: str) -> Settings:
    """Settings for a token-mode run.

    App credentials are absent by construction here, which is why
    `GITHUB_AUTH_MODE` exists: it tells `Settings` which credentials to insist
    on rather than demanding every field for every deployment shape.
    """
    overrides: dict[str, Any] = {
        "github_auth_mode": GitHubAuthMode.TOKEN,
        "github_token": token,
    }
    if args.docs:
        overrides["docs_globs"] = args.docs
    if args.provider:
        overrides["llm_provider"] = args.provider

    if args.dry_run:
        # The single switch that makes a run side-effect free. Both are checked
        # inside FeedbackOrchestrator before any write.
        overrides["post_pr_comment"] = False
        overrides["publish_check_run"] = False

    try:
        return Settings(**overrides)
    except Exception as exc:
        raise CliError(f"configuration error:\n{exc}") from exc


# --------------------------------------------------------------------------- #
# Context assembly
# --------------------------------------------------------------------------- #


def context_from_api(
    payload: dict[str, Any], owner: str, repo: str, pull_number: int
) -> PullRequestContext:
    """Build the same context the webhook parser builds, from the REST payload."""
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    head_sha = str(head.get("sha") or "")
    if not head_sha:
        raise CliError(
            f"pull request {owner}/{repo}#{pull_number} returned no head SHA; "
            "it may be from a deleted fork"
        )

    return PullRequestContext(
        repo_owner=owner,
        repo_name=repo,
        pull_number=pull_number,
        head_sha=head_sha,
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or ""),
        # There is no installation in token mode; StaticTokenAuth ignores this.
        installation_id=0,
        action="cli",
        is_draft=bool(payload.get("draft", False)),
        title=str(payload.get("title") or ""),
        html_url=str(payload.get("html_url") or ""),
        delivery_id="cli",
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_HEADLINES = {
    OutcomeKind.EVALUATED: "Evaluated",
    OutcomeKind.SKIPPED_NO_FILES: "Skipped — no file changes",
    OutcomeKind.SKIPPED_NOISE_ONLY: "Skipped — formatting only",
    OutcomeKind.SKIPPED_DOCS_ONLY: "Skipped — documentation only",
    OutcomeKind.SKIPPED_NO_DOCUMENTATION: "Skipped — no documentation found",
    OutcomeKind.FAILED: "Failed",
}


def render_outcome(outcome: ReviewOutcome, *, dry_run: bool) -> str:
    ctx = outcome.context
    lines = [
        "",
        f"  {ctx.slug} @ {ctx.head_sha[:7]}",
        f"  {'-' * 60}",
        f"  Result   : {_HEADLINES.get(outcome.kind, outcome.kind.value)}",
    ]
    if outcome.verdict_status:
        lines.append(f"  Verdict  : {outcome.verdict_status}")
    if outcome.signals:
        lines.append(f"  Signals  : {len(outcome.signals)} structural change(s) detected")
    lines.append(f"  Summary  : {outcome.summary}")
    if outcome.comment_url:
        lines.append(f"  Comment  : {outcome.comment_url}")
    if outcome.error:
        lines.append(f"  Error    : {outcome.error}")
    if dry_run:
        lines.append("  (dry run — nothing was posted to the pull request)")
    lines.append("")
    return "\n".join(lines)


def write_step_summary(outcome: ReviewOutcome) -> None:
    """Append a short report to the workflow run summary, when running in CI.

    Best-effort: a failure to write the summary must never fail the review.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    status = outcome.verdict_status or outcome.kind.value
    icon = "📝" if status == "NEEDS_UPDATE" else "✅"
    body = (
        f"## {icon} ReadMe Realist\n\n"
        f"**{outcome.context.slug}** @ `{outcome.context.head_sha[:7]}`\n\n"
        f"- **Result:** {_HEADLINES.get(outcome.kind, outcome.kind.value)}\n"
        f"- **Verdict:** {status}\n\n"
        f"{outcome.summary}\n"
    )
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:  # pragma: no cover - depends on runner filesystem
        logger.warning("could not write step summary", extra={"error": str(exc)})


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def run_review(args: argparse.Namespace) -> int:
    owner, repo = split_repository(args.repository)
    token = resolve_token(args.token)
    settings = build_settings(args, token)

    configure_logging(
        level=settings.log_level,
        json_output=settings.log_format.value == "json",
        secrets=settings.secret_values(),
    )

    backend = build_backend(settings)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.github_timeout_seconds),
        follow_redirects=True,
    )

    try:
        github = GitHubClient(
            http=http,
            auth=StaticTokenAuth(settings.access_token),
            api_base_url=settings.github_api_base_url,
            max_retries=settings.github_max_retries,
        )

        try:
            payload = await github.fetch_pull_request(owner, repo, args.pull_number)
        except Exception as exc:
            raise CliError(f"could not fetch {owner}/{repo}#{args.pull_number}: {exc}") from exc

        ctx = context_from_api(payload, owner, repo, args.pull_number)

        if ctx.is_draft and settings.skip_draft_pull_requests:
            print(f"\n  {ctx.slug} is a draft — skipped.")
            print("  Pass SKIP_DRAFT_PULL_REQUESTS=false to review drafts.\n")
            return 0

        pipeline = ReviewPipeline(
            github=github,
            evaluator=backend.evaluator,
            feedback=FeedbackOrchestrator(
                github,
                drift_conclusion=settings.drift_check_conclusion,
                post_comment=settings.post_pr_comment,
                publish_check_run=settings.publish_check_run,
            ),
            settings=settings,
        )

        # `review` is contractually no-raise; failures arrive as an outcome.
        outcome = await pipeline.review(ctx)
    finally:
        await http.aclose()
        await backend.aclose()

    print(render_outcome(outcome, dry_run=args.dry_run))
    write_step_summary(outcome)

    if outcome.kind is OutcomeKind.FAILED:
        return EXIT_ERROR
    if outcome.verdict_status == "NEEDS_UPDATE" and args.fail_on_drift:
        return EXIT_DRIFT
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_review(args))
    except CliError as exc:
        print(f"\nerror: {exc}\n", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ninterrupted\n", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised via `main()`
    raise SystemExit(main())
