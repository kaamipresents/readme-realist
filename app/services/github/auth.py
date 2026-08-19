"""GitHub App authentication.

Two-step flow: sign a short-lived RS256 JWT with the App's private key, exchange
it for an installation access token, then use that token as a bearer for all
repository calls. Installation tokens last an hour, so they are cached per
installation and refreshed just before expiry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import jwt

logger = logging.getLogger(__name__)

#: GitHub rejects JWTs with `exp` more than 10 minutes out. Nine is comfortable.
_JWT_LIFETIME_SECONDS = 9 * 60

#: Backdate `iat` to absorb clock drift between us and GitHub.
_JWT_CLOCK_SKEW_SECONDS = 60

#: Refresh an installation token this long before it actually expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 300


class GitHubAuthError(RuntimeError):
    """Could not mint a JWT or exchange it for an installation token."""


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: str
    expires_at: float  # monotonic-comparable POSIX timestamp

    def is_fresh(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current < self.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS


class GitHubAppAuth:
    """Mints and caches installation access tokens."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        http: httpx.AsyncClient,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._http = http
        self._api_base_url = api_base_url.rstrip("/")
        self._cache: dict[int, _CachedToken] = {}
        # One lock per installation so concurrent PRs on the same repo mint a
        # single token instead of racing to replace each other's.
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #

    def create_app_jwt(self) -> str:
        """Sign the App-level JWT used to request installation tokens."""
        now = int(time.time())
        payload = {
            "iat": now - _JWT_CLOCK_SKEW_SECONDS,
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except Exception as exc:  # pragma: no cover - key validated at startup
            raise GitHubAuthError(f"failed to sign App JWT: {exc}") from exc

    async def installation_token(self, installation_id: int) -> str:
        """Return a valid installation token, minting one if needed."""
        cached = self._cache.get(installation_id)
        if cached is not None and cached.is_fresh():
            return cached.token

        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            # Re-check: another coroutine may have refreshed while we waited.
            cached = self._cache.get(installation_id)
            if cached is not None and cached.is_fresh():
                return cached.token

            token = await self._request_installation_token(installation_id)
            self._cache[installation_id] = token
            return token.token

    def invalidate(self, installation_id: int) -> None:
        """Drop a cached token — call this after a 401 so the next try re-mints."""
        self._cache.pop(installation_id, None)

    # ------------------------------------------------------------------ #

    async def _request_installation_token(self, installation_id: int) -> _CachedToken:
        url = f"{self._api_base_url}/app/installations/{installation_id}/access_tokens"
        try:
            response = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.create_app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"network error requesting installation token: {exc}") from exc

        if response.status_code != 201:
            raise GitHubAuthError(
                f"installation token request failed ({response.status_code}): {response.text[:500]}"
            )

        body = response.json()
        token = body.get("token")
        if not token:
            raise GitHubAuthError("installation token response contained no `token`")

        expires_at = _parse_expiry(body.get("expires_at"))
        logger.debug(
            "minted installation token",
            extra={"installation_id": installation_id, "expires_at": body.get("expires_at")},
        )
        return _CachedToken(token=token, expires_at=expires_at)


def _parse_expiry(raw: object) -> float:
    """GitHub returns ISO-8601 with a `Z` suffix; fall back to +1h."""
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.warning("unparseable token expiry %r; assuming one hour", raw)
    return datetime.now(UTC).timestamp() + 3600
