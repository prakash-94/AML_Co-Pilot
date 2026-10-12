"""Who is calling, and in which role.

The browser signs the user in with Firebase / Identity Platform and sends the resulting ID token
in `Authorization: Bearer ...`. The API checks that token's signature in code (a Firebase Hosting
rewrite cannot present Google credentials, so the Cloud Run API is publicly reachable and this
check is the front door). The user's role comes from a custom claim set by an administrator -
never from anything the browser sends.

Every verifier is injected into the API (`create_app(verifier=...)`), so tests use a fake and the
real Google libraries are imported only when a real token is checked.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from aml_copilot.errors import AMLCopilotError

Role = Literal["investigator", "supervisor"]
ROLES: tuple[str, ...] = ("investigator", "supervisor")


class AuthError(AMLCopilotError):
    """The caller could not be identified (missing, malformed, expired or forged token)."""


class ForbiddenError(AMLCopilotError):
    """The caller is known but their role does not allow this."""


@dataclass(frozen=True)
class Principal:
    uid: str
    email: str
    role: Role


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal:
        """Return who the token belongs to, or raise AuthError."""


def bearer_token(authorization: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header."""
    if not authorization:
        raise AuthError("Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization header must be 'Bearer <token>'")
    return token.strip()


def principal_from_claims(claims: Mapping[str, object]) -> Principal:
    """Build a Principal from verified token claims. A user with no valid role claim is refused."""
    uid = claims.get("user_id") or claims.get("sub")
    role = claims.get("role")
    if not isinstance(uid, str) or not uid:
        raise AuthError("Token has no user id")
    if role not in ROLES:
        raise AuthError("This account has no role assigned")
    return Principal(uid=uid, email=str(claims.get("email", "")), role=role)  # type: ignore[arg-type]


class FirebaseTokenVerifier:
    """Checks a Firebase ID token's signature, expiry, issuer and audience (= the project id)."""

    def __init__(self, project_id: str):
        if not project_id:
            raise AuthError("FIREBASE_PROJECT_ID is not set")
        self._project_id = project_id

    def verify(self, token: str) -> Principal:
        from google.auth.transport import requests as google_requests  # imported only when needed
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_firebase_token(token, google_requests.Request(), audience=self._project_id)
        except Exception as exc:  # google-auth raises ValueError / GoogleAuthError subclasses
            raise AuthError(f"Invalid token: {type(exc).__name__}") from exc
        if not claims:
            raise AuthError("Invalid token")
        return principal_from_claims(claims)


class StaticTokenVerifier:
    """Local development and tests only: fixed tokens map to fixed people. No cryptography."""

    def __init__(self, people: Mapping[str, Principal]):
        self._people = dict(people)

    def verify(self, token: str) -> Principal:
        try:
            return self._people[token]
        except KeyError:
            raise AuthError("Unknown token") from None


def dev_people() -> dict[str, Principal]:
    return {
        "dev-investigator": Principal("dev-investigator", "investigator@dev.local", "investigator"),
        "dev-supervisor": Principal("dev-supervisor", "supervisor@dev.local", "supervisor"),
    }


def build_verifier(mode: str, *, firebase_project_id: str = "", environ: Mapping[str, str] | None = None) -> TokenVerifier:
    """Pick the verifier. Dev auth is refused on Cloud Run (which always sets K_SERVICE), so a
    misconfigured deploy fails to start instead of accepting `dev-supervisor` from anyone."""
    env = os.environ if environ is None else environ
    if mode == "firebase":
        return FirebaseTokenVerifier(firebase_project_id)
    if mode == "dev":
        if env.get("K_SERVICE"):
            raise AuthError("Dev authentication is not allowed on Cloud Run")
        return StaticTokenVerifier(dev_people())
    raise AuthError(f"Unknown AUTH_MODE '{mode}' (use 'firebase' or 'dev')")


def require_role(principal: Principal, *allowed: str) -> None:
    if principal.role not in allowed:
        raise ForbiddenError(f"Role '{principal.role}' may not do this")


# --------------------------------------------------------------- the worker's caller (Cloud Tasks)
class InternalCallerVerifier(Protocol):
    def verify(self, authorization: str | None) -> None:
        """Raise AuthError unless the request really comes from our Cloud Tasks queue."""


class CloudTasksCallerVerifier:
    """Cloud Tasks signs each call with an OIDC token for a service account we chose. Check the
    signature, that it was minted for THIS worker (audience), and that it is that account."""

    def __init__(self, audience: str, allowed_email: str):
        if not audience or not allowed_email:
            raise AuthError("WORKER_URL and TASKS_INVOKER_EMAIL must be set for the worker")
        self._audience = audience
        self._allowed_email = allowed_email

    def verify(self, authorization: str | None) -> None:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        token = bearer_token(authorization)
        try:
            claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=self._audience)
        except Exception as exc:
            raise AuthError(f"Invalid caller token: {type(exc).__name__}") from exc
        if claims.get("email") != self._allowed_email or not claims.get("email_verified"):
            raise AuthError("The caller is not the Cloud Tasks invoker")


class AllowAnyCaller:
    """Local development and tests only."""

    def verify(self, authorization: str | None) -> None:
        return None


__all__ = [
    "ROLES", "AllowAnyCaller", "AuthError", "CloudTasksCallerVerifier", "FirebaseTokenVerifier", "ForbiddenError",
    "InternalCallerVerifier", "Principal", "Role", "StaticTokenVerifier", "TokenVerifier", "bearer_token",
    "build_verifier", "dev_people", "principal_from_claims", "require_role",
]
