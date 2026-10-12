"""Logging setup: the one place that decides how every log line looks.

The ingestion code already logs through structlog (`log.warning("source_retry", source=...)`) but
nothing ever configured it, so lines came out as coloured console text. In the cloud that is useless:
Cloud Logging wants one JSON object per line, with `severity` and `message` fields it understands, so
it can filter by level and by `job_id`.

`configure_logging("json")` makes BOTH structlog and the standard `logging` module (which every
third-party library uses) print the same JSON. `configure_logging("console")` is the readable
version for a person at a terminal. Call it once, at start-up.
"""

import logging
import sys
from typing import Any

import structlog

LOG_FORMATS = ("console", "json")

# Libraries that log every HTTP call at INFO. In the cloud that is volume we pay for and it buries
# our own events, so they are held to WARNING and above regardless of the app's level.
NOISY_LIBRARIES = ("databricks", "urllib3", "httpx", "httpcore", "chromadb", "sentence_transformers")


def _cloud_logging_fields(_logger: Any, _method: str, event: dict) -> dict:
    """Rename structlog's keys to the ones Cloud Logging reads: level -> severity, event -> message."""
    event["severity"] = str(event.pop("level", "info")).upper()
    event["message"] = event.pop("event", "")
    return event


def configure_logging(log_format: str = "console", level: str = "INFO") -> None:
    """Set up structlog and stdlib logging. Safe to call more than once (it replaces, never stacks)."""
    if log_format not in LOG_FORMATS:
        raise ValueError(f"log_format must be one of {LOG_FORMATS}, not {log_format!r}")

    # Steps applied to every record, whether it came from structlog or from a library.
    shared = [
        structlog.contextvars.merge_contextvars,  # fields bound with bind_job(), e.g. job_id
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
        final = [_cloud_logging_fields, structlog.processors.format_exc_info, renderer]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        final = [renderer]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # so a later reconfigure (and the tests) takes effect
    )

    # stderr, so a command's report on stdout stays clean when redirected; Cloud Run collects both
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,  # lines from non-structlog libraries get the same fields
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]  # replace, so calling this twice never doubles the output
    root.setLevel(level.upper())
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(max(root.level, logging.WARNING))


def configure_logging_from_settings() -> None:
    """Read LOG_FORMAT / LOG_LEVEL from the environment (or .env) and apply them."""
    from aml_copilot.config import Settings

    settings = Settings()
    configure_logging(settings.log_format, settings.log_level)


def bind_job(job_id: str, **fields: Any) -> None:
    """Attach `job_id` (and anything else) to every log line from now on, in this request/thread."""
    structlog.contextvars.bind_contextvars(job_id=job_id, **fields)


def clear_job() -> None:
    """Forget everything attached by `bind_job`. Call it when a request ends."""
    structlog.contextvars.clear_contextvars()
