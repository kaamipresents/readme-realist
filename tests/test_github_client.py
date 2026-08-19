"""GitHub REST client: media types, docs retrieval, retries, glob matching."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.models.domain import PullRequestContext
from app.services.github.client import (
    DIFF_MEDIA_TYPE,
    RAW_MEDIA_TYPE,
    GitHubApiError,
    GitHubClient,
    glob_to_regex,
)
from tests.conftest import FakeAuth

API = "https://api.github.com"


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(
        http=httpx.AsyncClient(),
        auth=FakeAuth(),
        api_base_url=API,
        max_retries=2,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff would otherwise make this module slow."""

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.github.client.asyncio.sleep", instant)


# --------------------------------------------------------------------------- #
# Glob translation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("README.md", "README.md", True),
        ("README.md", "docs/README.md", False),
        ("docs/**/*.md", "docs/setup.md", True),
        ("docs/**/*.md", "docs/guides/deep/nested.md", True),
        ("docs/**/*.md", "docs/setup.txt", False),
        ("docs/**/*.md", "other/setup.md", False),
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),
        ("**/*.md", "a/b/c.md", True),
        ("docs/?.md", "docs/a.md", True),
        ("docs/?.md", "docs/ab.md", False),
    ],
)
def test_glob_translation(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected


def test_glob_special_characters_are_escaped() -> None:
    """A dot must match a literal dot, not any character."""
    assert glob_to_regex("READMExmd").match("README.md") is None


# --------------------------------------------------------------------------- #
# Diff retrieval
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fetches_the_diff_with_the_correct_media_type(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        return_value=httpx.Response(200, text="diff --git a/x b/x\n")
    )
    diff = await client.fetch_pull_request_diff(pr_context)

    assert diff.startswith("diff --git")
    assert route.calls.last.request.headers["accept"] == DIFF_MEDIA_TYPE
    assert route.calls.last.request.headers["authorization"] == "Bearer ghs_faketoken"


# --------------------------------------------------------------------------- #
# Documentation retrieval
# --------------------------------------------------------------------------- #


def _mock_tree(paths: list[str], truncated: bool = False) -> None:
    respx.get(f"{API}/repos/acme/widget/git/trees/{'a' * 40}").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [{"path": p, "type": "blob", "size": 100} for p in paths]
                + [{"path": "docs", "type": "tree"}],
                "truncated": truncated,
            },
        )
    )


@respx.mock
async def test_selects_only_matching_documentation(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    _mock_tree(["README.md", "docs/setup.md", "app/main.py", "CHANGELOG.md"])
    for path, body in [("README.md", "# Widget"), ("docs/setup.md", "# Setup")]:
        respx.get(f"{API}/repos/acme/widget/contents/{path}").mock(
            return_value=httpx.Response(200, text=body)
        )

    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md", "docs/**/*.md"),
        max_files=40,
        max_total_chars=200_000,
        max_file_chars=60_000,
    )

    assert bundle.paths == ("README.md", "docs/setup.md")
    assert bundle.is_empty is False


@respx.mock
async def test_readme_survives_truncation_first(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    """Ordering matters: the most important doc must not be the one dropped."""
    _mock_tree(["docs/a.md", "docs/b.md", "README.md"])
    for path in ["README.md", "docs/a.md", "docs/b.md"]:
        respx.get(f"{API}/repos/acme/widget/contents/{path}").mock(
            return_value=httpx.Response(200, text=f"# {path}")
        )

    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md", "docs/**/*.md"),
        max_files=1,
        max_total_chars=200_000,
        max_file_chars=60_000,
    )

    assert bundle.paths == ("README.md",)
    assert bundle.truncated is True


@respx.mock
async def test_oversized_files_are_truncated_and_flagged(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    _mock_tree(["README.md"])
    respx.get(f"{API}/repos/acme/widget/contents/README.md").mock(
        return_value=httpx.Response(200, text="x" * 5000)
    )

    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md",),
        max_files=10,
        max_total_chars=200_000,
        max_file_chars=100,
    )

    assert len(bundle.files[0].content) == 100
    assert bundle.files[0].truncated is True


@respx.mock
async def test_one_unreadable_file_does_not_sink_the_review(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    _mock_tree(["README.md", "docs/broken.md"])
    respx.get(f"{API}/repos/acme/widget/contents/README.md").mock(
        return_value=httpx.Response(200, text="# Widget")
    )
    respx.get(f"{API}/repos/acme/widget/contents/docs/broken.md").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md", "docs/**/*.md"),
        max_files=10,
        max_total_chars=200_000,
        max_file_chars=60_000,
    )

    assert bundle.paths == ("README.md",)


@respx.mock
async def test_documentation_is_read_from_the_head_ref(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    """Docs updated *within* the PR must count as up to date."""
    _mock_tree(["README.md"])
    route = respx.get(f"{API}/repos/acme/widget/contents/README.md").mock(
        return_value=httpx.Response(200, text="# Widget")
    )

    await client.fetch_documentation(
        pr_context,
        globs=("README.md",),
        max_files=10,
        max_total_chars=200_000,
        max_file_chars=60_000,
    )

    assert route.calls.last.request.url.params["ref"] == "a" * 40
    assert route.calls.last.request.headers["accept"] == RAW_MEDIA_TYPE


@respx.mock
async def test_repository_with_no_documentation_returns_an_empty_bundle(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    _mock_tree(["app/main.py", "setup.py"])
    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md", "docs/**/*.md"),
        max_files=10,
        max_total_chars=200_000,
        max_file_chars=60_000,
    )
    assert bundle.is_empty is True


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #


@respx.mock
async def test_retries_a_server_error_then_succeeds(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, text="diff --git a/x b/x\n"),
        ]
    )
    diff = await client.fetch_pull_request_diff(pr_context)

    assert diff.startswith("diff --git")
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_the_retry_budget(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    with pytest.raises(GitHubApiError) as exc:
        await client.fetch_pull_request_diff(pr_context)
    assert exc.value.status_code == 503


@respx.mock
async def test_rate_limiting_is_retried(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}, text="slow down"),
            httpx.Response(200, text="diff --git a/x b/x\n"),
        ]
    )
    await client.fetch_pull_request_diff(pr_context)
    assert route.call_count == 2


@respx.mock
async def test_a_401_invalidates_the_cached_token_and_re_mints(
    pr_context: PullRequestContext,
) -> None:
    auth = FakeAuth()
    client = GitHubClient(http=httpx.AsyncClient(), auth=auth, api_base_url=API, max_retries=2)
    respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Bad credentials"}),
            httpx.Response(200, text="diff --git a/x b/x\n"),
        ]
    )

    await client.fetch_pull_request_diff(pr_context)
    assert auth.invalidations == [99]


@respx.mock
async def test_a_404_is_not_retried(client: GitHubClient, pr_context: PullRequestContext) -> None:
    """Client errors are permanent; retrying them just wastes the budget."""
    route = respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(GitHubApiError) as exc:
        await client.fetch_pull_request_diff(pr_context)

    assert exc.value.status_code == 404
    assert route.call_count == 1


@respx.mock
async def test_network_errors_are_retried_then_wrapped(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    respx.get(f"{API}/repos/acme/widget/pulls/42").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(GitHubApiError, match="network error"):
        await client.fetch_pull_request_diff(pr_context)


# --------------------------------------------------------------------------- #
# Feedback endpoints
# --------------------------------------------------------------------------- #


@respx.mock
async def test_posts_a_pull_request_comment(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.post(f"{API}/repos/acme/widget/issues/42/comments").mock(
        return_value=httpx.Response(201, json={"id": 7, "html_url": "https://x/1"})
    )
    result = await client.create_issue_comment(pr_context, "hello")

    assert result["id"] == 7
    assert route.calls.last.request.read() == b'{"body":"hello"}'


@respx.mock
async def test_creates_a_check_run_against_the_head_sha(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.post(f"{API}/repos/acme/widget/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 555})
    )
    result = await client.create_check_run(
        pr_context,
        name="ReadMe Realist",
        status="completed",
        conclusion="neutral",
        title="Docs need updating",
        summary="REDIS_URL is undocumented.",
    )

    assert result["id"] == 555
    body = route.calls.last.request.read().decode()
    assert '"head_sha":"' + "a" * 40 + '"' in body
    assert '"conclusion":"neutral"' in body


@respx.mock
async def test_check_run_text_is_capped_below_the_github_limit(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.post(f"{API}/repos/acme/widget/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    await client.create_check_run(
        pr_context,
        name="x",
        status="completed",
        conclusion="neutral",
        summary="s",
        text="y" * 100_000,
    )
    sent = route.calls.last.request.read().decode()
    assert '"y' * 1 in sent
    assert len(sent) < 70_000


@respx.mock
async def test_total_documentation_budget_stops_and_trims(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    """The last file in is trimmed to fit; anything after it is dropped."""
    _mock_tree(["README.md", "docs/a.md", "docs/b.md"])
    for path in ["README.md", "docs/a.md", "docs/b.md"]:
        respx.get(f"{API}/repos/acme/widget/contents/{path}").mock(
            return_value=httpx.Response(200, text="x" * 80)
        )

    bundle = await client.fetch_documentation(
        pr_context,
        globs=("README.md", "docs/**/*.md"),
        max_files=10,
        max_total_chars=100,
        max_file_chars=1000,
    )

    assert bundle.total_chars == 100
    assert bundle.truncated is True
    assert bundle.paths == ("README.md", "docs/a.md")
    assert bundle.files[-1].truncated is True


@respx.mock
async def test_lists_pull_request_comments(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    respx.get(f"{API}/repos/acme/widget/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "body": "hi"}])
    )
    comments = await client.list_issue_comments(pr_context)
    assert comments[0]["id"] == 1


@respx.mock
async def test_a_non_list_comments_response_is_tolerated(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    respx.get(f"{API}/repos/acme/widget/issues/42/comments").mock(
        return_value=httpx.Response(200, json={"message": "unexpected"})
    )
    assert await client.list_issue_comments(pr_context) == []


@respx.mock
async def test_updates_an_existing_comment(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.patch(f"{API}/repos/acme/widget/issues/comments/500").mock(
        return_value=httpx.Response(200, json={"id": 500, "body": "revised"})
    )
    result = await client.update_issue_comment(pr_context, 500, "revised")

    assert result["body"] == "revised"
    assert b"revised" in route.calls.last.request.read()


@respx.mock
async def test_completes_an_existing_check_run(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    route = respx.patch(f"{API}/repos/acme/widget/check-runs/777").mock(
        return_value=httpx.Response(200, json={"id": 777})
    )
    await client.update_check_run(
        pr_context,
        777,
        status="completed",
        conclusion="success",
        title="Documentation is up to date",
        summary="No drift found.",
    )

    body = route.calls.last.request.read().decode()
    assert '"status":"completed"' in body
    assert '"conclusion":"success"' in body
    assert '"title":"Documentation is up to date"' in body


@respx.mock
async def test_a_check_run_update_can_carry_status_only(
    client: GitHubClient, pr_context: PullRequestContext
) -> None:
    """No output fields means no `output` key — GitHub rejects an empty one."""
    route = respx.patch(f"{API}/repos/acme/widget/check-runs/777").mock(
        return_value=httpx.Response(200, json={"id": 777})
    )
    await client.update_check_run(pr_context, 777, status="in_progress")

    body = route.calls.last.request.read().decode()
    assert "output" not in body
