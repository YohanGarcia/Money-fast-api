"""OpenID Connect ID-token validation, decoupled from any provider.

The backend never exchanges authorization codes and never holds a client secret: the client obtains an
ID token (carrying the server-issued nonce) and the backend only *verifies* it — signature against the
provider's JWKS, algorithm allow-list, issuer, audience, expiry and nonce. Provider tokens are not stored.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import ServiceUnavailable

GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ALLOWED_ALGORITHMS = ("RS256",)


class OidcError(Exception):
    """The ID token is not acceptable (never carries the token or provider details)."""


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    nonce: str | None
    email: str | None
    email_verified: bool


class OidcVerifier:
    def __init__(
        self,
        *,
        issuers: frozenset[str],
        audience: str,
        jwks_provider: Callable[[], dict],
        leeway_seconds: int = 60,
    ) -> None:
        if not audience:
            raise ValueError("audience is required")
        self.issuers = issuers
        self.audience = audience
        self.jwks_provider = jwks_provider
        self.leeway = leeway_seconds

    def verify(self, id_token: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(id_token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:  # blocks "none" and HMAC-with-public-key tricks
                raise OidcError("alg")
            keys = self.jwks_provider().get("keys", [])
            key = next((k for k in keys if k.get("kid") == header.get("kid")), None)
            if key is None:
                raise OidcError("kid")
            claims = jwt.decode(
                id_token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.audience,
                options={"verify_iss": False, "leeway": self.leeway, "require_exp": True, "require_sub": True},
            )
        except OidcError:
            raise
        except (JWTError, ValueError, KeyError, TypeError):
            raise OidcError("token") from None
        if claims.get("iss") not in self.issuers:
            raise OidcError("iss")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OidcError("sub")
        return VerifiedIdentity(
            issuer=claims["iss"],
            subject=subject,
            nonce=claims.get("nonce"),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
        )


class CachedJwks:
    """Fetches the provider's JWKS over HTTPS and caches it (default one hour)."""

    def __init__(self, url: str, ttl_seconds: int = 3600, timeout: float = 5.0) -> None:
        self.url, self.ttl, self.timeout = url, ttl_seconds, timeout
        self._keys: dict | None = None
        self._fetched_at = 0.0

    def __call__(self) -> dict:
        now = time.monotonic()
        if self._keys is None or now - self._fetched_at > self.ttl:
            try:
                response = httpx.get(self.url, timeout=self.timeout)
                response.raise_for_status()
                self._keys = response.json()
            except (httpx.HTTPError, ValueError):
                raise OidcError("jwks_unavailable") from None
            self._fetched_at = now
        return self._keys


_google_jwks = CachedJwks(GOOGLE_JWKS_URL)


def build_google_verifier() -> OidcVerifier:
    """FastAPI dependency. BLOCKED_BY_EVIDENCE for the real provider: needs a Google OAuth client id
    (``GOOGLE_CLIENT_ID``) and outbound network access, neither available in this environment."""
    if not settings.google_client_id:
        raise ServiceUnavailable("El inicio de sesion con Google no esta configurado.")
    return OidcVerifier(
        issuers=GOOGLE_ISSUERS,
        audience=settings.google_client_id,
        jwks_provider=_google_jwks,
        leeway_seconds=settings.oidc_clock_skew_seconds,
    )
