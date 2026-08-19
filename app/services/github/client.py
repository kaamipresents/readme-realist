"""Authenticated GitHub REST client.

Covers exactly what the pipeline needs: fetch a PR's diff, retrieve raw markdown
from the head ref, and publish comments and check runs. Retries are centralised
here so no caller has to think about rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Final

import httpx

from app.models.domain import DocumentationBundle, DocumentFile, PullRequestContext

logger = logging.getLogger(__name__)

DIFF_MEDIA_TYPE: Final = "application/vnd.github.v3.diff"
RAW_MEDIA_TYPE: Final = "application/vnd.github.raw"
JSON_MEDIA_TYPE: Final = "application/vnd.github+json"
API_VERSION: Final = "2022-11-28"

_RETRYABLE_STATUSES: Final = frozenset({408, 429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS: Final = 30.0


class GitHubApiError(RuntimeError):
    """A GitHub request failed in a way the caller cannot recover from."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Glob matching (supports `**` across separators, unlike PurePath.match)
# --------------------------------------------------------------------------- #


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX-ish glob into an anchored regex."""
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")  # zero or more directory segments
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile(f"^{''.join(out)}$")


def matches_any_glob(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.match(path) for pattern in patterns)


# --------------------------------------------------------------------------- #


class GitHubClient:
    """Thin, retrying wrapper over the endpoints this app actually uses."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        auth: Any,  # GitHubAppAuth — typed loosely so tests can inject a fake
        api_base_url: str = "https://api.github.com",
        max_retries: int = 3,
    ) -> None:
        self._http = http
        self._auth = auth
        self._base = api_base_url.rstrip("/")
        self._max_retries = max_retries

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        installation_id: int,
        accept: str = JSON_MEDIA_TYPE,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base}{path}"
        last_error: str = "no attempt was made"

        for attempt in range(self._max_retries + 1):
            token = await self._auth.installation_token(installation_id)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": accept,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "readme-realist",
            }

            try:
                response = await self._http.request(
                    method, url, headers=headers, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                if attempt >= self._max_retries:
                    raise GitHubApiError(f"{method} {url} failed — {last_error}") from exc
                await self._sleep_before_retry(attempt, None)
                continue

            if response.status_code == 401:
                # Token may have been revoked mid-flight; force a re-mint once.
                self._auth.invalidate(installation_id)
                last_error = "401 unauthorized"
                if attempt >= self._max_retries:
                    raise GitHubApiError(f"{method} {url} failed — {last_error}", status_code=401)
                continue

            if response.status_code in _RETRYABLE_STATUSES:
                last_error = f"{response.status_code} {response.text[:200]}"
                if attempt >= self._max_retries:
                    raise GitHubApiError(
                        f"{method} {url} failed after {attempt + 1} attempts — {last_error}",
                        status_code=response.status_code,
                    )
                logger.warning(
                    "retrying GitHub request",
                    extra={
                        "method": method,
                        "url": url,
                        "status": response.status_code,
                        "attempt": attempt + 1,
                    },
                )
                await self._sleep_before_retry(attempt, response)
                continue

            if response.status_code >= 400:
                raise GitHubApiError(
                    f"{method} {url} failed ({response.status_code}): {response.text[:500]}",
                    status_code=response.status_code,
                )

            return response

        raise GitHubApiError(f"{method} {url} exhausted retries — {last_error}")

    @staticmethod
    async def _sleep_before_retry(attempt: int, response: httpx.Response | None) -> None:
        """Honour `Retry-After` / rate-limit reset when present, else back off."""
        delay: float | None = None
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = None
            if delay is None and response.headers.get("x-ratelimit-remaining") == "0":
                delay = 60.0

        if delay is None:
            delay = min(2.0**attempt, _MAX_BACKOFF_SECONDS)

        # Jitter so a burst of webhooks does not retry in lockstep.
        await asyncio.sleep(min(delay, _MAX_BACKOFF_SECONDS) * (0.5 + random.random() * 0.5))

    # ------------------------------------------------------------------ #
    # Context retrieval
    # ------------------------------------------------------------------ #

    async def fetch_pull_request_diff(self, ctx: PullRequestContext) -> str:
        """The PR's unified diff, via the `v3.diff` media type."""
        response = await self._request(
            "GET",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/pulls/{ctx.pull_number}",
            installation_id=ctx.installation_id,
            accept=DIFF_MEDIA_TYPE,
        )
        return response.text

    async def list_tree_paths(self, ctx: PullRequestContext) -> tuple[list[dict[str, Any]], bool]:
        """Every blob on the PR head ref, plus GitHub's own truncation flag."""
        response = await self._request(
            "GET",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/git/trees/{ctx.head_sha}",
            installation_id=ctx.installation_id,
            params={"recursive": "1"},
        )
        body = response.json()
        entries = [item for item in body.get("tree", []) if item.get("type") == "blob"]
        return entries, bool(body.get("truncated", False))

    async def fetch_file_content(self, ctx: PullRequestContext, path: str) -> str:
        """Raw file bytes from the PR head ref, decoded as UTF-8."""
        response = await self._request(
            "GET",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/contents/{path}",
            installation_id=ctx.installation_id,
            accept=RAW_MEDIA_TYPE,
            params={"ref": ctx.head_sha},
        )
        return response.text

    async def fetch_documentation(
        self,
        ctx: PullRequestContext,
        *,
        globs: tuple[str, ...],
        max_files: int,
        max_total_chars: int,
        max_file_chars: int,
    ) -> DocumentationBundle:
        """Retrieve the markdown that the diff will be checked against.

        Files are ordered README-first, then shallowest path, then
        alphabetically — so the most important document survives truncation.
        """
        patterns = tuple(glob_to_regex(pattern) for pattern in globs)
        entries, tree_truncated = await self.list_tree_paths(ctx)

        candidates = [
            entry for entry in entries if matches_any_glob(str(entry.get("path", "")), patterns)
        ]
        candidates.sort(key=lambda entry: _doc_sort_key(str(entry.get("path", ""))))

        truncated = tree_truncated or len(candidates) > max_files
        selected = candidates[:max_files]

        documents: list[DocumentFile] = []
        used = 0
        for entry in selected:
            path = str(entry["path"])
            if used >= max_total_chars:
                truncated = True
                break
            try:
                content = await self.fetch_file_content(ctx, path)
            except GitHubApiError as exc:
                # One unreadable doc must not sink the whole review.
                logger.warning(
                    "skipping unreadable documentation file",
                    extra={"path": path, "error": str(exc), **ctx.log_context()},
                )
                continue

            file_truncated = False
            if len(content) > max_file_chars:
                content = content[:max_file_chars]
                file_truncated = True
            remaining = max_total_chars - used
            if len(content) > remaining:
                content = content[:remaining]
                file_truncated = True
                truncated = True

            documents.append(DocumentFile(path=path, content=content, truncated=file_truncated))
            used += len(content)

        return DocumentationBundle(files=tuple(documents), truncated=truncated)

    # ------------------------------------------------------------------ #
    # Feedback publishing
    # ------------------------------------------------------------------ #

    async def list_issue_comments(self, ctx: PullRequestContext) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/issues/{ctx.pull_number}/comments",
            installation_id=ctx.installation_id,
            params={"per_page": 100},
        )
        body = response.json()
        return body if isinstance(body, list) else []

    async def create_issue_comment(self, ctx: PullRequestContext, body: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/issues/{ctx.pull_number}/comments",
            installation_id=ctx.installation_id,
            json_body={"body": body},
        )
        return dict(response.json())

    async def update_issue_comment(
        self, ctx: PullRequestContext, comment_id: int, body: str
    ) -> dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/issues/comments/{comment_id}",
            installation_id=ctx.installation_id,
            json_body={"body": body},
        )
        return dict(response.json())

    async def create_check_run(
        self,
        ctx: PullRequestContext,
        *,
        name: str,
        status: str,
        conclusion: str | None = None,
        title: str = "",
        summary: str = "",
        text: str | None = None,
        details_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": ctx.head_sha,
            "status": status,
        }
        if conclusion is not None:
            payload["conclusion"] = conclusion
        if details_url:
            payload["details_url"] = details_url
        if title or summary or text:
            output: dict[str, Any] = {"title": title or name, "summary": summary}
            if text:
                # GitHub caps check-run `text` at 65535 characters.
                output["text"] = text[:65000]
            payload["output"] = output

        response = await self._request(
            "POST",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/check-runs",
            installation_id=ctx.installation_id,
            json_body=payload,
        )
        return dict(response.json())

    async def update_check_run(
        self,
        ctx: PullRequestContext,
        check_run_id: int,
        *,
        status: str | None = None,
        conclusion: str | None = None,
        title: str = "",
        summary: str = "",
        text: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status
        if conclusion is not None:
            payload["conclusion"] = conclusion
        if title or summary or text:
            output: dict[str, Any] = {"title": title, "summary": summary}
            if text:
                output["text"] = text[:65000]
            payload["output"] = output

        response = await self._request(
            "PATCH",
            f"/repos/{ctx.repo_owner}/{ctx.repo_name}/check-runs/{check_run_id}",
            installation_id=ctx.installation_id,
            json_body=payload,
        )
        return dict(response.json())


def _doc_sort_key(path: str) -> tuple[int, int, str]:
    """README first, then shallow paths, then alphabetical."""
    lowered = path.lower()
    is_root_readme = 0 if lowered in {"readme.md", "readme.mdx"} else 1
    return (is_root_readme, path.count("/"), lowered)
