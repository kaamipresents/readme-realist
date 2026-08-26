"""The CLI / GitHub Action entry point.

Nothing here reaches the network: the pipeline is stubbed and the model backend
is never constructed, so these tests exercise argument handling, context
assembly, exit codes, and the dry-run guarantee.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app.config import GitHubAuthMode, Settings
from app.models.domain import (
    ChangeSide,
    OutcomeKind,
    PullRequestContext,
    ReviewOutcome,
    SignalKind,
    StructuralSignal,
)

# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_from_ambient_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test outside the repository, with GitHub credentials unset.

    `Settings` reads `.env` from the working directory — deliberate, and useful
    for local CLI runs — but it would otherwise make these tests depend on
    whatever the developer happens to have configured. A GitHub Actions runner
    has neither a `.env` nor App credentials, which is what we want to model.
    """
    monkeypatch.chdir(tmp_path)
    for name in (
        "GITHUB_APP_ID",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_PRIVATE_KEY_PATH",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_PROVIDER",
        "DOCS_GLOBS",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "command": "review",
        "repository": "acme/widget",
        "pull_number": 42,
        "dry_run": False,
        "fail_on_drift": False,
        "token": "ghp_test",
        "docs": None,
        "provider": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _outcome(
    ctx: PullRequestContext,
    *,
    kind: OutcomeKind = OutcomeKind.EVALUATED,
    verdict_status: str | None = "UP_TO_DATE",
    **extra: Any,
) -> ReviewOutcome:
    return ReviewOutcome(
        kind=kind,
        context=ctx,
        summary="a summary",
        verdict_status=verdict_status,
        **extra,
    )


API_PAYLOAD: dict[str, Any] = {
    "head": {"sha": "b" * 40, "ref": "feature/x"},
    "base": {"ref": "main"},
    "draft": False,
    "title": "Add a flag",
    "html_url": "https://github.com/acme/widget/pull/42",
}


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def test_parser_reads_a_review_invocation() -> None:
    parsed = cli.build_parser().parse_args(["review", "acme/widget", "42", "--dry-run"])
    assert parsed.repository == "acme/widget"
    assert parsed.pull_number == 42
    assert parsed.dry_run is True
    assert parsed.fail_on_drift is False


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


@pytest.mark.parametrize("value", ["widget", "/widget", "acme/", "acme/widget/extra", ""])
def test_malformed_repository_is_rejected(value: str) -> None:
    with pytest.raises(cli.CliError, match="OWNER/REPO"):
        cli.split_repository(value)


def test_repository_splits_into_owner_and_name() -> None:
    assert cli.split_repository("acme/widget") == ("acme", "widget")


# --------------------------------------------------------------------------- #
# Token resolution
# --------------------------------------------------------------------------- #


def test_explicit_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert cli.resolve_token("explicit") == "explicit"


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_token_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for candidate in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(candidate, raising=False)
    monkeypatch.setenv(name, "from-env")
    assert cli.resolve_token(None) == "from-env"


def test_a_missing_token_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for candidate in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(candidate, raising=False)
    with pytest.raises(cli.CliError, match="no GitHub token"):
        cli.resolve_token(None)


# --------------------------------------------------------------------------- #
# Settings construction
# --------------------------------------------------------------------------- #


def test_settings_use_token_mode_and_need_no_app_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the Action path: no App ID, no PEM, no webhook secret."""
    for name in ("GITHUB_APP_ID", "GITHUB_WEBHOOK_SECRET", "GITHUB_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")

    settings = cli.build_settings(_args(), "ghp_test")

    assert settings.github_auth_mode is GitHubAuthMode.TOKEN
    assert settings.access_token == "ghp_test"
    assert settings.github_app_id is None
    assert settings.github_private_key is None


def test_dry_run_disables_every_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    settings = cli.build_settings(_args(dry_run=True), "ghp_test")
    assert settings.post_pr_comment is False
    assert settings.publish_check_run is False


def test_overrides_reach_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    settings = cli.build_settings(
        _args(docs="README.md,CONTRIBUTING.md", provider="anthropic"), "ghp_test"
    )
    assert settings.docs_globs == ["README.md", "CONTRIBUTING.md"]
    assert settings.llm_provider.value == "anthropic"


def test_a_missing_provider_key_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(cli.CliError, match="configuration error"):
        cli.build_settings(_args(), "ghp_test")


# --------------------------------------------------------------------------- #
# Context assembly
# --------------------------------------------------------------------------- #


def test_context_is_rebuilt_from_the_rest_payload() -> None:
    ctx = cli.context_from_api(API_PAYLOAD, "acme", "widget", 42)

    assert ctx.repo_owner == "acme"
    assert ctx.repo_name == "widget"
    assert ctx.pull_number == 42
    assert ctx.head_sha == "b" * 40
    assert ctx.base_ref == "main"
    assert ctx.action == "cli"
    # There is no installation in token mode.
    assert ctx.installation_id == 0


def test_a_payload_without_a_head_sha_is_rejected() -> None:
    with pytest.raises(cli.CliError, match="no head SHA"):
        cli.context_from_api({"head": {}}, "acme", "widget", 42)


def test_draft_state_survives_the_round_trip() -> None:
    ctx = cli.context_from_api({**API_PAYLOAD, "draft": True}, "acme", "widget", 42)
    assert ctx.is_draft is True


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_rendered_outcome_reports_the_essentials(pr_context: PullRequestContext) -> None:
    signal = StructuralSignal(
        kind=SignalKind.ENV_VAR,
        detail="REDIS_URL",
        file_path="app/worker.py",
        side=ChangeSide.ADDED,
    )
    rendered = cli.render_outcome(
        _outcome(pr_context, verdict_status="NEEDS_UPDATE", signals=(signal,)),
        dry_run=False,
    )

    assert "acme/widget#42" in rendered
    assert "NEEDS_UPDATE" in rendered
    assert "1 structural change" in rendered
    assert "dry run" not in rendered


def test_dry_run_is_stated_in_the_output(pr_context: PullRequestContext) -> None:
    rendered = cli.render_outcome(_outcome(pr_context), dry_run=True)
    assert "nothing was posted" in rendered


def test_step_summary_is_written_when_running_in_actions(
    pr_context: PullRequestContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    cli.write_step_summary(_outcome(pr_context, verdict_status="NEEDS_UPDATE"))

    body = summary.read_text(encoding="utf-8")
    assert "ReadMe Realist" in body
    assert "NEEDS_UPDATE" in body


def test_step_summary_is_skipped_outside_actions(
    pr_context: PullRequestContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Must not raise despite there being nowhere to write.
    cli.write_step_summary(_outcome(pr_context))


# --------------------------------------------------------------------------- #
# End-to-end through main(), with the pipeline stubbed
# --------------------------------------------------------------------------- #


class _StubPipeline:
    def __init__(self, outcome: ReviewOutcome) -> None:
        self._outcome = outcome
        self.reviewed: list[PullRequestContext] = []

    async def review(self, ctx: PullRequestContext) -> ReviewOutcome:
        self.reviewed.append(ctx)
        return self._outcome


@pytest.fixture
def stubbed_run(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch out everything that would touch the network."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class _FakeBackend:
        evaluator = object()

        async def aclose(self) -> None:
            return None

    captured: dict[str, Any] = {}

    def _install(outcome: ReviewOutcome, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        async def _fetch_pull_request(
            self: Any, owner: str, repo: str, number: int, **_: Any
        ) -> dict[str, Any]:
            return payload if payload is not None else API_PAYLOAD

        pipeline = _StubPipeline(outcome)
        captured["pipeline"] = pipeline

        monkeypatch.setattr(cli, "build_backend", lambda settings: _FakeBackend())
        monkeypatch.setattr(
            "app.services.github.client.GitHubClient.fetch_pull_request", _fetch_pull_request
        )
        monkeypatch.setattr(cli, "ReviewPipeline", lambda **kwargs: pipeline)
        return captured

    return _install


def test_a_clean_review_exits_zero(stubbed_run: Any, pr_context: PullRequestContext) -> None:
    stubbed_run(_outcome(pr_context, verdict_status="UP_TO_DATE"))
    assert cli.main(["review", "acme/widget", "42"]) == 0


def test_drift_alone_does_not_fail_the_build(
    stubbed_run: Any, pr_context: PullRequestContext
) -> None:
    """Advisory by default — the same stance the Check Run takes."""
    stubbed_run(_outcome(pr_context, verdict_status="NEEDS_UPDATE"))
    assert cli.main(["review", "acme/widget", "42"]) == 0


def test_drift_fails_the_build_when_asked(stubbed_run: Any, pr_context: PullRequestContext) -> None:
    stubbed_run(_outcome(pr_context, verdict_status="NEEDS_UPDATE"))
    assert cli.main(["review", "acme/widget", "42", "--fail-on-drift"]) == cli.EXIT_DRIFT


def test_a_failed_review_exits_with_the_error_code(
    stubbed_run: Any, pr_context: PullRequestContext
) -> None:
    stubbed_run(_outcome(pr_context, kind=OutcomeKind.FAILED, verdict_status=None, error="boom"))
    assert cli.main(["review", "acme/widget", "42"]) == cli.EXIT_ERROR


def test_a_draft_is_skipped_without_reviewing(
    stubbed_run: Any, pr_context: PullRequestContext
) -> None:
    captured = stubbed_run(_outcome(pr_context), {**API_PAYLOAD, "draft": True})
    assert cli.main(["review", "acme/widget", "42"]) == 0
    assert captured["pipeline"].reviewed == []


def test_a_bad_repository_argument_reports_cleanly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    assert cli.main(["review", "not-a-repo", "42"]) == cli.EXIT_ERROR
    assert "OWNER/REPO" in capsys.readouterr().err


def test_dry_run_reaches_the_pipeline_with_writes_disabled(
    stubbed_run: Any, pr_context: PullRequestContext, capsys: pytest.CaptureFixture[str]
) -> None:
    stubbed_run(_outcome(pr_context, verdict_status="NEEDS_UPDATE"))
    assert cli.main(["review", "acme/widget", "42", "--dry-run"]) == 0
    assert "nothing was posted" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Token-mode settings, independent of the CLI
# --------------------------------------------------------------------------- #


def test_token_mode_settings_reject_a_missing_token() -> None:
    with pytest.raises(Exception, match="GITHUB_TOKEN"):
        Settings(
            github_auth_mode="token",
            gemini_api_key="gemini-test",
            _env_file=None,  # type: ignore[call-arg]
        )


def test_token_mode_redacts_the_token_from_logs() -> None:
    settings = Settings(
        github_auth_mode="token",
        github_token="ghp_supersecret",
        gemini_api_key="gemini-test",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert "ghp_supersecret" in settings.secret_values()
    assert "ghp_supersecret" not in repr(settings)
