"""Startup configuration validation.

The point of this module is fail-fast: a malformed key should stop the process,
not surface as a 500 on the first webhook at 3am.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Effort, LogFormat, Settings

BASE = {
    "github_app_id": "123456",
    "github_webhook_secret": "a-sufficiently-long-webhook-secret",
    "anthropic_api_key": "sk-ant-test",
    "gemini_api_key": "gemini-test",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides}, _env_file=None)  # type: ignore[arg-type]


def test_accepts_a_valid_inline_key(rsa_private_key_pem: str) -> None:
    settings = _settings(github_private_key=rsa_private_key_pem)
    assert settings.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_accepts_a_key_from_a_file(tmp_path: Path, rsa_private_key_pem: str) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")

    settings = _settings(github_private_key_path=key_file)
    assert "PRIVATE KEY" in settings.private_key_pem


def test_escaped_newlines_from_a_dotenv_are_restored(rsa_private_key_pem: str) -> None:
    """`.env` files cannot hold real newlines, so `\\n` must be unescaped."""
    escaped = rsa_private_key_pem.replace("\n", "\\n")
    settings = _settings(github_private_key=escaped)
    assert settings.private_key_pem.count("\n") > 5


def test_requires_a_private_key() -> None:
    with pytest.raises(ValidationError, match="GITHUB_PRIVATE_KEY"):
        _settings()


def test_rejects_supplying_both_key_forms(tmp_path: Path, rsa_private_key_pem: str) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")
    with pytest.raises(ValidationError, match="only one"):
        _settings(github_private_key=rsa_private_key_pem, github_private_key_path=key_file)


def test_rejects_a_missing_key_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        _settings(github_private_key_path=tmp_path / "nope.pem")


def test_rejects_a_key_that_is_not_a_pem(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError, match="PEM"):
        _settings(github_private_key="just-some-string-that-is-not-a-key")


def test_rejects_a_short_webhook_secret(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        _settings(github_private_key=rsa_private_key_pem, github_webhook_secret="short")


def test_docs_globs_accept_a_comma_separated_env_string(
    rsa_private_key_pem: str,
) -> None:
    settings = _settings(
        github_private_key=rsa_private_key_pem,
        docs_globs="README.md, docs/**/*.md ,CONTRIBUTING.md",
    )
    assert settings.docs_globs == ["README.md", "docs/**/*.md", "CONTRIBUTING.md"]


def test_api_base_url_must_be_absolute(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError, match="absolute"):
        _settings(github_private_key=rsa_private_key_pem, github_api_base_url="api.github.com")


def test_api_base_url_trailing_slash_is_stripped(rsa_private_key_pem: str) -> None:
    settings = _settings(
        github_private_key=rsa_private_key_pem,
        github_api_base_url="https://github.example.com/api/v3/",
    )
    assert settings.github_api_base_url == "https://github.example.com/api/v3"


def test_rejects_an_unknown_log_level(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError, match="log level"):
        _settings(github_private_key=rsa_private_key_pem, log_level="CHATTY")


def test_rejects_an_invalid_check_conclusion(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError):
        _settings(github_private_key=rsa_private_key_pem, drift_check_conclusion="exploded")


def test_secret_values_covers_every_secret(rsa_private_key_pem: str) -> None:
    """Anything listed here gets scrubbed from logs; missing one is a leak.

    Both provider keys are included even though only one backend is active — a
    configured-but-unused key must still never reach a log sink.
    """
    settings = _settings(github_private_key=rsa_private_key_pem)
    secrets = settings.secret_values()

    assert settings.webhook_secret in secrets
    assert "sk-ant-test" in secrets
    assert "gemini-test" in secrets
    assert settings.private_key_pem in secrets


def test_secrets_are_not_exposed_by_repr(rsa_private_key_pem: str) -> None:
    settings = _settings(github_private_key=rsa_private_key_pem)
    rendered = repr(settings)

    assert "sk-ant-test" not in rendered
    assert "gemini-test" not in rendered
    assert "PRIVATE KEY" not in rendered


# --------------------------------------------------------------------------- #
# Loading from the environment
#
# The tests above build Settings from kwargs, which uses pydantic-settings'
# *init* source. Production uses the *env* source, and the two differ: the env
# source pre-parses complex (list-typed) fields as JSON before field validators
# run. These tests exercise the path a container actually takes.
# --------------------------------------------------------------------------- #


@pytest.fixture
def env_key_file(tmp_path: Path, rsa_private_key_pem: str) -> Path:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")
    return key_file


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch, env_key_file: Path) -> None:
    for name in list(os.environ):
        if name.startswith(("GITHUB_", "ANTHROPIC_", "DOCS_", "DRIFT_", "LOG_")):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "a-sufficiently-long-webhook-secret")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(env_key_file))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env")


def _from_env() -> Settings:
    """Construct exactly as the running service does — no kwargs at all."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_loads_cleanly_from_the_environment(base_env: None) -> None:
    settings = _from_env()
    assert settings.github_app_id == "123456"
    assert settings.docs_globs == ["README.md", "docs/**/*.md"]

    # Both provider keys are optional on the model; the active one is enforced
    # by `_require_key_for_selected_provider`.
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "gemini-env"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-env"


def test_comma_separated_docs_globs_from_env(
    base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form documented in .env.example must actually work.

    pydantic-settings JSON-decodes list fields from the env source by default,
    which would reject this outright — the field is marked `NoDecode` so the
    string reaches our own validator.
    """
    monkeypatch.setenv("DOCS_GLOBS", "README.md,docs/**/*.md,CONTRIBUTING.md")
    assert _from_env().docs_globs == ["README.md", "docs/**/*.md", "CONTRIBUTING.md"]


def test_a_single_glob_from_env(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_GLOBS", "README.md")
    assert _from_env().docs_globs == ["README.md"]


def test_json_array_docs_globs_from_env_still_works(
    base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCS_GLOBS", '["README.md", "docs/**/*.md"]')
    assert _from_env().docs_globs == ["README.md", "docs/**/*.md"]


def test_malformed_json_docs_globs_is_reported_clearly(
    base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCS_GLOBS", '["README.md",')
    with pytest.raises(ValidationError, match="will not parse"):
        _from_env()


def test_env_overrides_reach_every_tunable(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_EFFORT", "low")
    monkeypatch.setenv("DRIFT_CHECK_CONCLUSION", "failure")
    monkeypatch.setenv("SKIP_LLM_ON_NOISE_ONLY", "false")
    monkeypatch.setenv("MAX_CONCURRENT_REVIEWS", "8")
    monkeypatch.setenv("LOG_FORMAT", "text")

    settings = _from_env()
    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.anthropic_effort is Effort.LOW
    assert settings.drift_check_conclusion == "failure"
    assert settings.skip_llm_on_noise_only is False
    assert settings.max_concurrent_reviews == 8
    assert settings.log_format is LogFormat.TEXT


def test_an_inline_key_with_escaped_newlines_from_env(
    base_env: None, monkeypatch: pytest.MonkeyPatch, rsa_private_key_pem: str
) -> None:
    """The realistic secret-manager form: one line with `\n` escapes."""
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", rsa_private_key_pem.replace("\n", "\n"))

    assert "-----BEGIN PRIVATE KEY-----\n" in _from_env().private_key_pem


def test_a_missing_required_variable_fails_at_startup(
    base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_APP_ID")
    with pytest.raises(ValidationError, match="github_app_id"):
        _from_env()
