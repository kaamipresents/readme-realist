"""Environment configuration, validated once at startup.

Every secret is loaded and checked here so the process fails fast on a
malformed key rather than on the first webhook that arrives at 3am.
"""

from __future__ import annotations

import functools
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PEM_HEADERS = ("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----")


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class LLMProvider(StrEnum):
    """Which model backend performs the semantic verification."""

    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class GitHubAuthMode(StrEnum):
    """How the process proves who it is to GitHub.

    `APP` is the webhook server: sign a JWT with the App private key, exchange
    it for a per-installation token. `TOKEN` is the CLI and the GitHub Action,
    where the runner already holds a usable token and there is no App, no
    installation, and no webhook to verify.
    """

    APP = "app"
    TOKEN = "token"


CheckConclusion = Literal["neutral", "failure", "success"]


class Settings(BaseSettings):
    """Fully validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- GitHub credentials -------------------------------------------------
    # Which set of credentials below is mandatory depends on `github_auth_mode`;
    # see `_require_credentials_for_auth_mode`. Everything is optional at the
    # field level so that neither deployment shape has to invent values it will
    # never use.
    github_auth_mode: GitHubAuthMode = GitHubAuthMode.APP

    # App mode (the webhook server)
    github_app_id: str | None = Field(default=None, min_length=1)
    github_webhook_secret: SecretStr | None = None
    github_private_key: SecretStr | None = None
    github_private_key_path: Path | None = None

    # Token mode (the CLI and the GitHub Action). In a workflow this is the
    # `GITHUB_TOKEN` the runner injects; locally it can be any PAT with
    # `repo` scope.
    github_token: SecretStr | None = None

    github_api_base_url: str = "https://api.github.com"
    github_timeout_seconds: float = Field(default=30.0, gt=0)
    github_max_retries: int = Field(default=3, ge=0, le=10)

    # --- Model backend ------------------------------------------------------
    llm_provider: LLMProvider = LLMProvider.GEMINI

    # --- Anthropic ----------------------------------------------------------
    # Optional: only required when `llm_provider` selects it. See
    # `_require_key_for_selected_provider` below.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_max_tokens: int = Field(default=8000, ge=1024, le=128000)
    anthropic_effort: Effort = Effort.HIGH
    anthropic_timeout_seconds: float = Field(default=300.0, gt=0)
    anthropic_max_retries: int = Field(default=3, ge=0, le=10)

    # --- Gemini -------------------------------------------------------------
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_max_output_tokens: int = Field(default=8000, ge=256, le=65536)
    gemini_timeout_seconds: float = Field(default=300.0, gt=0)
    gemini_max_retries: int = Field(default=4, ge=1, le=10)

    # --- Documentation scope ------------------------------------------------
    # `NoDecode` is load-bearing: without it pydantic-settings JSON-decodes any
    # list-typed field coming from the environment *before* validators run, so
    # the documented `DOCS_GLOBS=README.md,docs/**/*.md` form would raise a
    # JSONDecodeError at startup. NoDecode hands the raw string to
    # `_split_globs` below instead.
    docs_globs: Annotated[list[str], NoDecode, Field(min_length=1)] = [
        "README.md",
        "docs/**/*.md",
    ]
    docs_max_files: int = Field(default=40, ge=1, le=500)
    docs_max_total_chars: int = Field(default=200_000, ge=1_000)
    docs_max_file_chars: int = Field(default=60_000, ge=500)

    # --- Diff handling ------------------------------------------------------
    diff_max_chars: int = Field(default=120_000, ge=1_000)
    skip_llm_on_noise_only: bool = True

    # --- Feedback behaviour -------------------------------------------------
    drift_check_conclusion: CheckConclusion = "neutral"
    post_pr_comment: bool = True
    publish_check_run: bool = True
    skip_draft_pull_requests: bool = True

    # --- Server -------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    max_concurrent_reviews: int = Field(default=4, ge=1, le=64)

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @field_validator("docs_globs", mode="before")
    @classmethod
    def _split_globs(cls, value: object) -> object:
        """Accept a comma-separated env string, a JSON array, or a real list."""
        if not isinstance(value, str):
            return value

        text = value.strip()
        if text.startswith("["):
            # Someone used the JSON form; `NoDecode` means we own parsing it.
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"docs_globs looks like JSON but will not parse: {exc}") from exc

        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("github_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("github_api_base_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"unsupported log level: {value!r}")
        return level

    @field_validator("github_webhook_secret")
    @classmethod
    def _webhook_secret_is_substantial(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if len(value.get_secret_value()) < 16:
            raise ValueError(
                "github_webhook_secret must be at least 16 characters; "
                'generate one with `python -c "import secrets; print(secrets.token_hex(32))"`'
            )
        return value

    @model_validator(mode="after")
    def _require_credentials_for_auth_mode(self) -> Settings:
        """Each mode demands its own credentials and none of the other's.

        Runs before `_resolve_private_key` so that a token-mode process is
        never asked for a PEM it has no way to supply.
        """
        if self.github_auth_mode is GitHubAuthMode.TOKEN:
            if self.github_token is None:
                raise ValueError("GITHUB_TOKEN is required when GITHUB_AUTH_MODE=token")
            return self

        missing = [
            name
            for name, value in (
                ("github_app_id", self.github_app_id),
                ("github_webhook_secret", self.github_webhook_secret),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} is required when GITHUB_AUTH_MODE=app "
                "(the default). Set GITHUB_AUTH_MODE=token for the CLI or "
                "GitHub Action, which needs no App credentials."
            )
        return self

    @model_validator(mode="after")
    def _require_key_for_selected_provider(self) -> Settings:
        """Only the chosen backend's credential is mandatory.

        Demanding both would force a Gemini-only deployment to invent an
        Anthropic key it will never use.
        """
        required = {
            LLMProvider.ANTHROPIC: ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            LLMProvider.GEMINI: ("gemini_api_key", "GEMINI_API_KEY"),
        }[self.llm_provider]

        attribute, env_name = required
        if getattr(self, attribute) is None:
            raise ValueError(f"{env_name} is required when LLM_PROVIDER={self.llm_provider.value}")
        return self

    @model_validator(mode="after")
    def _resolve_private_key(self) -> Settings:
        """Exactly one of the inline key / key path must be supplied and usable."""
        if self.github_auth_mode is not GitHubAuthMode.APP:
            # Token mode never signs a JWT, so there is no key to resolve.
            return self

        inline = self.github_private_key
        path = self.github_private_key_path

        if inline is None and path is None:
            raise ValueError("supply either GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH")
        if inline is not None and path is not None:
            raise ValueError(
                "supply only one of GITHUB_PRIVATE_KEY / GITHUB_PRIVATE_KEY_PATH, not both"
            )

        if path is not None:
            if not path.is_file():
                raise ValueError(f"github_private_key_path does not exist: {path}")
            pem = path.read_text(encoding="utf-8")
        else:
            assert inline is not None  # narrowed by the checks above
            # `.env` files cannot hold real newlines, so accept the escaped form.
            pem = inline.get_secret_value().replace("\\n", "\n")

        pem = pem.strip() + "\n"
        if not pem.startswith(_PEM_HEADERS):
            raise ValueError(
                "GitHub App private key does not look like a PEM document "
                f"(expected one of {_PEM_HEADERS})"
            )

        object.__setattr__(self, "github_private_key", SecretStr(pem))
        return self

    # ------------------------------------------------------------------ #
    # Convenience accessors
    # ------------------------------------------------------------------ #

    @property
    def app_id(self) -> str:
        assert self.github_app_id is not None  # guaranteed in app mode
        return self.github_app_id

    @property
    def private_key_pem(self) -> str:
        assert self.github_private_key is not None  # guaranteed by _resolve_private_key
        return self.github_private_key.get_secret_value()

    @property
    def webhook_secret(self) -> str:
        assert self.github_webhook_secret is not None  # guaranteed in app mode
        return self.github_webhook_secret.get_secret_value()

    @property
    def access_token(self) -> str:
        assert self.github_token is not None  # guaranteed in token mode
        return self.github_token.get_secret_value()

    def secret_values(self) -> tuple[str, ...]:
        """Every literal secret, for the log redaction filter.

        Every credential is listed regardless of which mode or provider is
        active — a secret that is configured but unused must still never reach
        a log sink.
        """
        candidates = (
            self.github_webhook_secret.get_secret_value() if self.github_webhook_secret else None,
            self.github_private_key.get_secret_value() if self.github_private_key else None,
            self.github_token.get_secret_value() if self.github_token else None,
            self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None,
            self.gemini_api_key.get_secret_value() if self.gemini_api_key else None,
        )
        return tuple(value for value in candidates if value)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    # Every field is populated from the environment / .env by pydantic-settings.
    return Settings()
