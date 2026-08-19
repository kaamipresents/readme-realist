"""Shared fixtures and test doubles.

Nothing here touches the network. GitHub is faked at either the transport layer
(respx) or the client layer (`FakeGitHubClient`), and Claude is faked with a
recording stub that returns whatever JSON the test asks for.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.models.domain import (
    DocumentationBundle,
    DocumentFile,
    PullRequestContext,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Credentials & settings
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    """A real (throwaway) RSA key so JWT signing can be exercised for real."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture
def webhook_secret() -> str:
    return "test-webhook-secret-that-is-long-enough"


@pytest.fixture
def settings(rsa_private_key_pem: str, webhook_secret: str) -> Settings:
    return Settings(
        github_app_id="123456",
        github_webhook_secret=webhook_secret,
        github_private_key=rsa_private_key_pem,
        # Both keys present so either backend can be selected in a test;
        # `llm_provider` stays at its default.
        anthropic_api_key="sk-ant-test-key",
        gemini_api_key="gemini-test-key",
        log_format="text",
        log_level="DEBUG",
        _env_file=None,  # type: ignore[call-arg]
    )


# --------------------------------------------------------------------------- #
# Domain fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def pr_context() -> PullRequestContext:
    return PullRequestContext(
        repo_owner="acme",
        repo_name="widget",
        pull_number=42,
        head_sha="a" * 40,
        head_ref="feature/add-env-var",
        base_ref="main",
        installation_id=99,
        action="opened",
        title="Add REDIS_URL support",
        html_url="https://github.com/acme/widget/pull/42",
        delivery_id="delivery-1",
    )


@pytest.fixture
def documentation() -> DocumentationBundle:
    return DocumentationBundle(
        files=(
            DocumentFile(
                path="README.md",
                content="# Widget\n\n## Setup\n\nSet `DATABASE_URL` and run `widget serve`.\n",
            ),
        )
    )


@pytest.fixture
def sample_diff() -> str:
    return (FIXTURES / "sample_diff.txt").read_text(encoding="utf-8")


@pytest.fixture
def webhook_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "pull_request_opened.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeAuth:
    """Stands in for GitHubAppAuth without signing or network calls."""

    def __init__(self, token: str = "ghs_faketoken") -> None:
        self.token = token
        self.invalidations: list[int] = []

    async def installation_token(self, installation_id: int) -> str:
        return self.token

    def invalidate(self, installation_id: int) -> None:
        self.invalidations.append(installation_id)


class FakeGitHubClient:
    """Records every call so feedback behaviour can be asserted precisely."""

    def __init__(
        self,
        *,
        diff: str = "",
        documentation: DocumentationBundle | None = None,
        existing_comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.diff = diff
        self.documentation = documentation or DocumentationBundle()
        self.comments: list[dict[str, Any]] = list(existing_comments or [])
        self.created_comments: list[str] = []
        self.updated_comments: list[tuple[int, str]] = []
        self.created_check_runs: list[dict[str, Any]] = []
        self.updated_check_runs: list[tuple[int, dict[str, Any]]] = []
        self._next_id = 1000

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def fetch_pull_request_diff(self, ctx: PullRequestContext) -> str:
        return self.diff

    async def fetch_documentation(self, ctx: PullRequestContext, **_: Any) -> DocumentationBundle:
        return self.documentation

    async def list_issue_comments(self, ctx: PullRequestContext) -> list[dict[str, Any]]:
        return list(self.comments)

    async def create_issue_comment(self, ctx: PullRequestContext, body: str) -> dict[str, Any]:
        comment = {
            "id": self._allocate_id(),
            "body": body,
            "html_url": "https://github.com/acme/widget/pull/42#issuecomment-1",
        }
        self.comments.append(comment)
        self.created_comments.append(body)
        return comment

    async def update_issue_comment(
        self, ctx: PullRequestContext, comment_id: int, body: str
    ) -> dict[str, Any]:
        self.updated_comments.append((comment_id, body))
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = body
                return comment
        return {"id": comment_id, "body": body}

    async def create_check_run(self, ctx: PullRequestContext, **kwargs: Any) -> dict[str, Any]:
        self.created_check_runs.append(kwargs)
        return {"id": self._allocate_id(), **kwargs}

    async def update_check_run(
        self, ctx: PullRequestContext, check_run_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        self.updated_check_runs.append((check_run_id, kwargs))
        return {"id": check_run_id, **kwargs}


def make_anthropic_response(
    payload: dict[str, Any] | str,
    *,
    stop_reason: str = "end_turn",
    include_thinking_block: bool = True,
    model: str = "claude-opus-5",
    usage: dict[str, int] | None = None,
) -> SimpleNamespace:
    """Build a response object shaped like the Anthropic SDK's.

    A thinking block is placed first by default — thinking is on by default on
    Opus 5, and index-0 access to `content` would be a real bug.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)
    blocks: list[SimpleNamespace] = []
    if include_thinking_block:
        blocks.append(SimpleNamespace(type="thinking", thinking=""))
    blocks.append(SimpleNamespace(type="text", text=text))

    usage_values = {
        "input_tokens": 1200,
        "output_tokens": 180,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 900,
    }
    usage_values.update(usage or {})

    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(**usage_values),
        stop_details=None,
    )


class FakeAnthropicClient:
    """Minimal `client.messages.create` stub that records its kwargs."""

    def __init__(self, response: Any = None, *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response

    @property
    def last_call(self) -> dict[str, Any]:
        assert self.calls, "no Anthropic call was made"
        return self.calls[-1]


@pytest.fixture
def fake_github() -> FakeGitHubClient:
    return FakeGitHubClient()
