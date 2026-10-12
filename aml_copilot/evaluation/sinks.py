"""Where scheduled evaluation results go: one row per account per run, appended to a Databricks Delta table
so quality over time can be queried (and charted) next to the data it was measured on.

The table is written with plain parameterised INSERTs through the same SQL connection the pipeline reads
with; the service principal that does it holds MODIFY on this one table and nothing else.
"""

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Protocol

from aml_copilot.evaluation.metrics import CaseResult

COLUMNS: tuple[str, ...] = (
    "run_id", "run_at", "kind", "account_id", "family", "expected_tier", "tier", "tier_ok", "status", "drafted",
    "sentences", "supported", "unsupported", "contradicted", "unverified", "faithfulness_passed",
    "cost_usd", "seconds", "git_sha", "llm_provider",
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  run_id STRING, run_at TIMESTAMP, kind STRING, account_id STRING, family STRING, expected_tier STRING,
  tier STRING, tier_ok BOOLEAN, status STRING, drafted BOOLEAN, sentences INT, supported INT,
  unsupported INT, contradicted INT, unverified INT, faithfulness_passed BOOLEAN,
  cost_usd DOUBLE, seconds DOUBLE, git_sha STRING, llm_provider STRING
)"""


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    run_at: str  # ISO timestamp
    kind: str  # "canary" or "full"
    git_sha: str
    llm_provider: str


def result_row(result: CaseResult, info: RunInfo) -> dict[str, Any]:
    return {
        "run_id": info.run_id, "run_at": info.run_at, "kind": info.kind,
        "account_id": result.case.account_id, "family": result.case.family,
        "expected_tier": result.case.expected_tier, "tier": result.tier, "tier_ok": result.tier_ok,
        "status": result.status, "drafted": result.drafted, "sentences": result.sentences,
        "supported": result.supported, "unsupported": result.unsupported, "contradicted": result.contradicted,
        "unverified": result.unverified, "faithfulness_passed": result.faithfulness_passed,
        "cost_usd": round(result.cost, 4), "seconds": round(result.seconds, 1),
        "git_sha": info.git_sha, "llm_provider": info.llm_provider,
    }


class ResultSink(Protocol):
    def write(self, rows: Sequence[dict[str, Any]]) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        self.rows.extend(dict(r) for r in rows)


def _safe_table(name: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 3 or not all(p.replace("_", "").isalnum() for p in parts):
        raise ValueError(f"Not a plain table name: {name!r}")
    return ".".join(f"`{p}`" for p in parts)


class DeltaSink:
    """Appends rows to a Delta table over a Databricks SQL connection (`open_connection` is injectable)."""

    def __init__(self, open_connection: Callable[[], Any], table: str):
        self._open = open_connection
        self._table = _safe_table(table)

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        names = ", ".join(COLUMNS)
        placeholders = ", ".join("CAST(:run_at AS TIMESTAMP)" if c == "run_at" else f":{c}" for c in COLUMNS)
        statement = f"INSERT INTO {self._table} ({names}) VALUES ({placeholders})"
        with closing(self._open()) as connection, connection.cursor() as cursor:
            for row in rows:
                cursor.execute(statement, {c: row[c] for c in COLUMNS})


__all__ = ["COLUMNS", "CREATE_TABLE_SQL", "DeltaSink", "InMemorySink", "ResultSink", "RunInfo", "result_row"]
