"""Retry transient failures when querying Databricks and Salesforce.

A source call can fail for two very different reasons:

* **Transient** - a dropped connection, a timeout, a 5xx, a "try again later". The same call a
  few seconds later usually works, so we retry it (with growing, jittered waits).
* **Permanent** - a bad credential, a malformed query, a missing permission. Repeating it never
  helps, and for a Salesforce login it can even lock the account. These fail immediately.

So the rule is "retry only what is known to be transient"; an unknown error is not retried.
Retries sit *inside* the existing `except -> SourceUnavailableError` boundary: if every attempt
fails, the original driver error is re-raised and wrapped exactly as it was before.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import structlog
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger()

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try, and how long to wait between tries (seconds, doubling each time)."""

    attempts: int = 3
    initial_wait: float = 1.0
    max_wait: float = 10.0


# The Databricks driver already retries 429/503 internally for up to 15 minutes, so our layer is
# only for what it gives up on (e.g. a 400 "Cannot create the resource" while a warehouse wakes).
DATABRICKS_RETRY = RetryPolicy(attempts=4, initial_wait=2.0, max_wait=20.0)
SALESFORCE_RETRY = RetryPolicy(attempts=3, initial_wait=1.0, max_wait=10.0)
NO_RETRY = RetryPolicy(attempts=1, initial_wait=0.0, max_wait=0.0)

_RETRYABLE_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _log_retry(source: str) -> Callable[[RetryCallState], None]:
    def hook(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        log.warning(
            "source_retry",
            source=source,
            attempt=state.attempt_number,
            error_type=type(exc).__name__,  # the type only: messages can carry connection details
            wait_seconds=round(state.next_action.sleep, 1) if state.next_action else 0,
        )

    return hook


def call_with_retry(
    operation: Callable[[], T],
    *,
    source: str,
    policy: RetryPolicy,
    is_transient: Callable[[BaseException], bool],
) -> T:
    """Run `operation`, retrying while it raises an error `is_transient` approves.

    After the last attempt the original exception is re-raised unchanged.
    """
    retrying = Retrying(
        stop=stop_after_attempt(max(1, policy.attempts)),
        wait=wait_exponential_jitter(multiplier=policy.initial_wait, max=policy.max_wait),
        retry=retry_if_exception(is_transient),
        before_sleep=_log_retry(source),
        reraise=True,
    )
    return retrying(operation)


def is_transient_databricks(exc: BaseException) -> bool:
    """True for failures worth retrying against Databricks."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    try:
        from databricks.sql import exc as dbx
    except ImportError:  # driver not installed: nothing of ours can be classified
        return False

    # The driver has already spent its own retry budget, or the failure is expected/permanent.
    if isinstance(
        exc,
        dbx.MaxRetryDurationError
        | dbx.NonRecoverableNetworkError
        | dbx.SessionAlreadyClosedError
        | dbx.CursorAlreadyClosedError,
    ):
        return False
    if isinstance(exc, dbx.RequestError):
        code = exc.context.get("http-code")
        if code is None or code in _RETRYABLE_HTTP_CODES:  # no response at all, or a server hiccup
            return True
        return "try again later" in (exc.message or "").lower()  # e.g. 400 while a warehouse wakes
    return False


def is_transient_salesforce(exc: BaseException) -> bool:
    """True for failures worth retrying against Salesforce.

    Deliberately NOT retried: authentication failures (a repeated bad login can lock the user),
    malformed queries (400) and refused requests (403, which includes the daily API limit).
    """
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    try:
        import requests
        from simple_salesforce import exceptions as sfx
    except ImportError:
        return False

    if isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout):
        return True
    # An expired session is fixed by logging in again (the source drops its cached client);
    # a 404 on the query endpoint was seen once, intermittently, against the live org.
    if isinstance(exc, sfx.SalesforceExpiredSession | sfx.SalesforceResourceNotFound):
        return True
    if isinstance(exc, sfx.SalesforceGeneralError):
        return exc.status in _RETRYABLE_HTTP_CODES
    return False


SUPABASE_RETRY = RetryPolicy(attempts=3, initial_wait=1.0, max_wait=10.0)
_SUPABASE_PERMANENT_HINTS = ("password authentication failed", "does not exist", "permission denied")


def is_transient_supabase(exc: BaseException) -> bool:
    """True for failures worth retrying against Supabase Postgres: dropped or refused connections and
    timeouts. NOT retried: a wrong password, a missing table or a permission error - repeating those
    cannot succeed (and repeated bad logins are noise at best)."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    try:
        import psycopg2
    except ImportError:
        return False
    if isinstance(exc, psycopg2.OperationalError | psycopg2.InterfaceError):
        text = str(exc).lower()
        return not any(hint in text for hint in _SUPABASE_PERMANENT_HINTS)
    return False
