"""Account id validation.

Every id reaches a SQL/SOQL query, so anything that is not exactly
ACC-S-nnnnn, ACC-L-nnnnn, ACC-N-nnnnn or ACC-H-nnnnn is rejected up front.
(S = structuring, L = layering, N = normal, H = high-risk: both patterns at once.)
"""

import re

from aml_copilot.errors import InvalidAccountIdError

_ACCOUNT_ID_RE = re.compile(r"ACC-[SLNH]-[0-9]{5}")


def validate_account_id(raw: str) -> str:
    """Return the canonical (stripped, upper-case) id or raise InvalidAccountIdError."""
    candidate = raw.strip().upper() if isinstance(raw, str) else ""
    if not _ACCOUNT_ID_RE.fullmatch(candidate):
        raise InvalidAccountIdError(f"Invalid account id: {raw!r}")
    return candidate


def canonical(value: object) -> str:
    """Canonical form of an id read back from a source, for comparison only."""
    return str(value).strip().upper() if value is not None else ""
