"""Databricks OAuth machine-to-machine sign-in: a service principal's id and secret are traded for a
short-lived access token, instead of a personal access token that expires on a calendar and belongs to a
person. The token is cached and renewed shortly before it expires.

`post` (the HTTP call) and `clock` are injectable, so tests never touch the network or wait.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx

from aml_copilot.errors import SourceUnavailableError

RENEW_BEFORE_SECONDS = 300  # fetch a fresh token when fewer than 5 minutes remain
TOKEN_PATH = "/oidc/v1/token"

PostFn = Callable[..., Any]


class DatabricksOAuthTokenProvider:
    def __init__(
        self, server_hostname: str, client_id: str, client_secret: str, *,
        post: PostFn | None = None, clock: Callable[[], float] = time.monotonic,
    ):
        self._url = f"https://{server_hostname}{TOKEN_PATH}"
        self._auth = (client_id, client_secret)
        self._post = post or httpx.post
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token is None or self._clock() >= self._expires_at - RENEW_BEFORE_SECONDS:
            self._refresh()
        assert self._token is not None
        return self._token

    def _refresh(self) -> None:
        try:
            response = self._post(
                self._url, auth=self._auth, data={"grant_type": "client_credentials", "scope": "all-apis"}, timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            self._token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        except Exception as exc:  # never include the secret or the response body in the message
            self._token = None
            raise SourceUnavailableError("databricks", f"OAuth sign-in failed ({type(exc).__name__})") from exc

    def credentials_provider(self) -> Callable[[], Callable[[], dict[str, str]]]:
        """The shape `databricks.sql.connect(credentials_provider=...)` expects."""
        return lambda: (lambda: {"Authorization": f"Bearer {self.token()}"})
