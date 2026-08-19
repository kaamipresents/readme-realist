"""GitHub App authentication: JWT signing and installation-token caching."""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization

from app.services.github.auth import GitHubAppAuth, GitHubAuthError

API = "https://api.github.com"
TOKEN_URL = f"{API}/app/installations/99/access_tokens"


@pytest.fixture
def public_key_pem(rsa_private_key_pem: str) -> str:
    private_key = serialization.load_pem_private_key(rsa_private_key_pem.encode(), password=None)
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture
def auth(rsa_private_key_pem: str) -> GitHubAppAuth:
    return GitHubAppAuth(
        app_id="123456",
        private_key_pem=rsa_private_key_pem,
        http=httpx.AsyncClient(),
        api_base_url=API,
    )


def _token_response(token: str = "ghs_token_1", *, expires_in: int = 3600) -> httpx.Response:
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))
    return httpx.Response(201, json={"token": token, "expires_at": expires_at})


# --------------------------------------------------------------------------- #
# JWT signing
# --------------------------------------------------------------------------- #


def test_the_jwt_verifies_against_the_public_key(auth: GitHubAppAuth, public_key_pem: str) -> None:
    token = auth.create_app_jwt()
    claims = jwt.decode(token, public_key_pem, algorithms=["RS256"])
    assert claims["iss"] == "123456"


def test_the_jwt_is_signed_with_rs256(auth: GitHubAppAuth) -> None:
    assert jwt.get_unverified_header(auth.create_app_jwt())["alg"] == "RS256"


def test_iat_is_backdated_to_absorb_clock_drift(auth: GitHubAppAuth, public_key_pem: str) -> None:
    """GitHub rejects a JWT whose `iat` is in its future."""
    claims = jwt.decode(auth.create_app_jwt(), public_key_pem, algorithms=["RS256"])
    assert claims["iat"] < time.time()


def test_expiry_stays_inside_githubs_ten_minute_ceiling(
    auth: GitHubAppAuth, public_key_pem: str
) -> None:
    """GitHub caps `exp` at 10 minutes *from now* — the backdated `iat` is not
    part of that budget, so measure the forward span."""
    issued_at = time.time()
    claims = jwt.decode(auth.create_app_jwt(), public_key_pem, algorithms=["RS256"])

    seconds_ahead = claims["exp"] - issued_at
    assert 0 < seconds_ahead <= 600


def test_an_unusable_private_key_raises_a_domain_error() -> None:
    broken = GitHubAppAuth(app_id="1", private_key_pem="not-a-key", http=httpx.AsyncClient())
    with pytest.raises(GitHubAuthError, match="failed to sign"):
        broken.create_app_jwt()


# --------------------------------------------------------------------------- #
# Token exchange
# --------------------------------------------------------------------------- #


@respx.mock
async def test_exchanges_the_jwt_for_an_installation_token(
    auth: GitHubAppAuth, public_key_pem: str
) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    token = await auth.installation_token(99)

    assert token == "ghs_token_1"

    sent = route.calls.last.request.headers["authorization"]
    assert sent.startswith("Bearer ")
    # The bearer must be the App JWT, not an installation token.
    jwt.decode(sent.removeprefix("Bearer "), public_key_pem, algorithms=["RS256"])


@respx.mock
async def test_a_valid_token_is_reused_not_re_minted(auth: GitHubAppAuth) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())

    assert await auth.installation_token(99) == "ghs_token_1"
    assert await auth.installation_token(99) == "ghs_token_1"

    assert route.call_count == 1


@respx.mock
async def test_a_token_near_expiry_is_refreshed_early(auth: GitHubAppAuth) -> None:
    """Refreshing at expiry would race a request already in flight."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            _token_response("ghs_nearly_expired", expires_in=120),
            _token_response("ghs_fresh"),
        ]
    )

    assert await auth.installation_token(99) == "ghs_nearly_expired"
    assert await auth.installation_token(99) == "ghs_fresh"
    assert route.call_count == 2


@respx.mock
async def test_concurrent_callers_mint_a_single_token(auth: GitHubAppAuth) -> None:
    """Two PRs on one repo must not race to replace each other's token."""
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())

    tokens = await asyncio.gather(*(auth.installation_token(99) for _ in range(5)))

    assert set(tokens) == {"ghs_token_1"}
    assert route.call_count == 1


@respx.mock
async def test_separate_installations_get_separate_tokens(
    auth: GitHubAppAuth,
) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response("ghs_for_99"))
    respx.post(f"{API}/app/installations/100/access_tokens").mock(
        return_value=_token_response("ghs_for_100")
    )

    assert await auth.installation_token(99) == "ghs_for_99"
    assert await auth.installation_token(100) == "ghs_for_100"


@respx.mock
async def test_invalidate_forces_a_re_mint(auth: GitHubAppAuth) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[_token_response("ghs_old"), _token_response("ghs_new")]
    )

    assert await auth.installation_token(99) == "ghs_old"
    auth.invalidate(99)
    assert await auth.installation_token(99) == "ghs_new"
    assert route.call_count == 2


def test_invalidating_an_unknown_installation_is_harmless(auth: GitHubAppAuth) -> None:
    auth.invalidate(12345)  # must not raise


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_rejected_exchange_raises(auth: GitHubAppAuth) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    with pytest.raises(GitHubAuthError, match="401"):
        await auth.installation_token(99)


@respx.mock
async def test_a_response_without_a_token_raises(auth: GitHubAppAuth) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(201, json={"expires_at": "x"}))
    with pytest.raises(GitHubAuthError, match="no `token`"):
        await auth.installation_token(99)


@respx.mock
async def test_a_network_error_raises_a_domain_error(auth: GitHubAppAuth) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(GitHubAuthError, match="network error"):
        await auth.installation_token(99)


@respx.mock
async def test_an_unparseable_expiry_falls_back_to_an_hour(auth: GitHubAppAuth) -> None:
    """A malformed timestamp must not make the token look permanently valid."""
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_weird", "expires_at": "not-a-timestamp"}
        )
    )

    assert await auth.installation_token(99) == "ghs_weird"
    assert await auth.installation_token(99) == "ghs_weird"
    assert route.call_count == 1, "the fallback expiry should still permit caching"


@respx.mock
async def test_a_failed_exchange_is_not_cached(auth: GitHubAppAuth) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(500, text="boom"), _token_response("ghs_recovered")]
    )

    with pytest.raises(GitHubAuthError):
        await auth.installation_token(99)

    assert await auth.installation_token(99) == "ghs_recovered"
    assert route.call_count == 2
