"""Entry points for the Databricks job tasks. The files in `databricks/jobs/` are two-line wrappers around these.

`spark` and `dbutils` come from the job's runtime (`databricks.sdk.runtime`); tests pass fakes instead.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from aml_copilot.dbx.investigate import investigate
from aml_copilot.dbx.runtime import (
    DEFAULT_SCHEMA,
    DEFAULT_SCOPE,
    apply_config,
    load_secrets,
    spark_services,
)
from aml_copilot.service.delta import create_tables_sql, spark_sql


def _runtime(spark: Any, dbutils: Any) -> tuple[Any, Any]:
    if spark is None or dbutils is None:
        from databricks.sdk.runtime import dbutils as real_dbutils
        from databricks.sdk.runtime import spark as real_spark

        return spark or real_spark, dbutils or real_dbutils
    return spark, dbutils


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="Databricks secret scope holding the credentials")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="catalog.schema holding the service tables")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="non-secret setting, e.g. LLM_PROVIDER=anthropic (repeatable)")


def _prepare(args: argparse.Namespace, spark: Any, dbutils: Any) -> tuple[Any, Any]:
    """Settings first (they decide where the typology documents live), then secrets, then logging."""
    apply_config(args.set)
    spark, dbutils = _runtime(spark, dbutils)
    loaded = load_secrets(dbutils, args.scope)
    from aml_copilot.config import Settings
    from aml_copilot.logging_config import configure_logging

    settings = Settings(_env_file=None)
    configure_logging(settings.log_format, settings.log_level)
    print(f"secrets loaded: {', '.join(loaded) or 'none'}", flush=True)
    return spark, settings


def investigate_main(argv: Sequence[str], spark: Any = None, dbutils: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="investigate")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--daily-limit", type=int, default=30)
    _common(parser)
    args = parser.parse_args(list(argv))

    spark, settings = _prepare(args, spark, dbutils)
    sql = spark_sql(spark)
    for statement in create_tables_sql(args.schema):
        sql(statement, {})
    store, audit, _ = spark_services(spark, args.schema)
    requested_by = args.requested_by or sql("SELECT current_user() AS u", {})[0]["u"]

    from aml_copilot.retrieval.store import build_index
    from aml_copilot.service.pipeline import make_provenance, make_run_fn

    print(f"vector index built: {build_index()} chunks", flush=True)
    report = investigate(
        args.account_id, requested_by, store=store, audit=audit, run=make_run_fn(settings),
        clock=lambda: datetime.now(UTC), daily_limit=args.daily_limit, provenance=make_provenance(settings),
    )
    print(f"RESULT {report.outcome}: job {report.job_id} - {report.detail}", flush=True)
    return 0 if report.ok else 1


def canary_main(argv: Sequence[str], spark: Any = None, dbutils: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="canary")
    parser.add_argument("--cap", default="0.6")
    parser.add_argument("--per-family", default="N=2,S=2,L=2,H=1")
    _common(parser)
    args = parser.parse_args(list(argv))

    spark, _ = _prepare(args, spark, dbutils)
    from aml_copilot.evaluation.__main__ import main as evaluation_main
    from aml_copilot.retrieval.store import build_index

    print(f"vector index built: {build_index()} chunks", flush=True)
    return evaluation_main(["canary", "canary", "--yes", "--sink", "spark", "--cap", args.cap,
                            "--per-family", args.per_family, "--table", f"{args.schema}.eval_runs"])


if __name__ == "__main__":
    sys.exit(investigate_main(sys.argv[1:]))
