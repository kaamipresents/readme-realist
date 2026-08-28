"""Provider selection: `LLM_PROVIDER` decides the backend, nothing else does."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import LLMProvider, Settings
from app.services.llm.evaluator import DriftEvaluator
from app.services.llm.factory import build_backend
from app.services.llm.gemini import GeminiDriftEvaluator

BASE = {
    "github_app_id": "123456",
    "github_webhook_secret": "a-sufficiently-long-webhook-secret",
}


def _settings(rsa_private_key_pem: str, **overrides: object) -> Settings:
    values = {**BASE, "github_private_key": rsa_private_key_pem, **overrides}
    return Settings(**values, _env_file=None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_gemini_is_the_default_backend(rsa_private_key_pem: str) -> None:
    settings = _settings(rsa_private_key_pem, gemini_api_key="gemini-test")
    assert settings.llm_provider is LLMProvider.GEMINI

    backend = build_backend(settings)
    assert isinstance(backend.evaluator, GeminiDriftEvaluator)
    assert backend.provider is LLMProvider.GEMINI
    assert backend.model == "gemini-2.5-flash"


def test_anthropic_is_selected_by_configuration(rsa_private_key_pem: str) -> None:
    settings = _settings(
        rsa_private_key_pem, llm_provider="anthropic", anthropic_api_key="sk-ant-test"
    )
    backend = build_backend(settings)

    assert isinstance(backend.evaluator, DriftEvaluator)
    assert backend.provider is LLMProvider.ANTHROPIC
    assert backend.model == "claude-opus-5"


def test_the_model_id_is_configurable(rsa_private_key_pem: str) -> None:
    """Gemini model IDs move fast; the default must not be a hard-coded ceiling."""
    settings = _settings(
        rsa_private_key_pem, gemini_api_key="gemini-test", gemini_model="gemini-3.1-pro-preview"
    )
    assert build_backend(settings).model == "gemini-3.1-pro-preview"


@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
def test_both_backends_satisfy_the_pipeline_protocol(
    rsa_private_key_pem: str, provider: str
) -> None:
    """The orchestrator only needs `.evaluate` — it must not care which is live."""
    settings = _settings(
        rsa_private_key_pem,
        llm_provider=provider,
        gemini_api_key="gemini-test",
        anthropic_api_key="sk-ant-test",
    )
    evaluator = build_backend(settings).evaluator
    assert callable(getattr(evaluator, "evaluate", None))


# --------------------------------------------------------------------------- #
# Credential requirements
# --------------------------------------------------------------------------- #


def test_gemini_key_is_required_when_gemini_is_selected(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError, match="GEMINI_API_KEY is required"):
        _settings(rsa_private_key_pem, anthropic_api_key="sk-ant-test")


def test_anthropic_key_is_required_when_anthropic_is_selected(
    rsa_private_key_pem: str,
) -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY is required"):
        _settings(rsa_private_key_pem, llm_provider="anthropic", gemini_api_key="gemini-test")


def test_the_unused_provider_key_is_not_required(rsa_private_key_pem: str) -> None:
    """A Gemini-only deployment must not have to invent an Anthropic key."""
    settings = _settings(rsa_private_key_pem, gemini_api_key="gemini-test")
    assert settings.anthropic_api_key is None
    assert build_backend(settings) is not None


def test_an_unknown_provider_is_rejected(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError):
        _settings(rsa_private_key_pem, llm_provider="llama", gemini_api_key="x")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
async def test_aclose_is_safe_for_either_backend(rsa_private_key_pem: str, provider: str) -> None:
    settings = _settings(
        rsa_private_key_pem,
        llm_provider=provider,
        gemini_api_key="gemini-test",
        anthropic_api_key="sk-ant-test",
    )
    backend = build_backend(settings)
    await backend.aclose()  # must not raise, whether or not a closer exists
