"""Logging setup: JSON lines for Cloud Logging, readable text for a terminal."""

import json
import logging

import pytest
import structlog

from aml_copilot.logging_config import bind_job, clear_job, configure_logging


@pytest.fixture(autouse=True)
def restore_logging():
    """Each test configures logging itself; put the root logger back afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    clear_job()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def lines(capsys):
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]  # logs go to stderr


def test_json_mode_prints_one_json_object_with_the_fields_cloud_logging_reads(capsys):
    configure_logging("json")
    structlog.get_logger().info("source_retry", source="databricks", attempt=2)

    [line] = lines(capsys)
    record = json.loads(line)
    assert record["severity"] == "INFO" and record["message"] == "source_retry"
    assert record["source"] == "databricks" and record["attempt"] == 2
    assert record["timestamp"].endswith("Z") or "+00:00" in record["timestamp"]
    assert "level" not in record and "event" not in record  # renamed, not duplicated


@pytest.mark.parametrize(("method", "severity"), [("warning", "WARNING"), ("error", "ERROR"), ("debug", "DEBUG")])
def test_each_level_maps_to_the_matching_severity(capsys, method, severity):
    configure_logging("json", level="DEBUG")
    getattr(structlog.get_logger(), method)("something")
    assert json.loads(lines(capsys)[0])["severity"] == severity


def test_lines_from_ordinary_libraries_are_json_too(capsys):
    configure_logging("json")
    logging.getLogger("some.library").warning("connection reset")

    record = json.loads(lines(capsys)[0])
    assert record["severity"] == "WARNING" and record["message"] == "connection reset"
    assert record["logger"] == "some.library"


def test_an_exception_is_included_as_text(capsys):
    configure_logging("json")
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger().error("run_failed", exc_info=True)

    assert "ValueError: boom" in json.loads(lines(capsys)[0])["exception"]


def test_bind_job_adds_the_job_id_to_every_line_until_cleared(capsys):
    configure_logging("json")
    bind_job("job-42", user="supervisor-1")
    structlog.get_logger().info("run_started")
    clear_job()
    structlog.get_logger().info("run_finished")

    first, second = (json.loads(line) for line in lines(capsys))
    assert first["job_id"] == "job-42" and first["user"] == "supervisor-1"
    assert "job_id" not in second


def test_configuring_twice_does_not_print_each_line_twice(capsys):
    configure_logging("json")
    configure_logging("json")
    structlog.get_logger().info("once")
    assert len(lines(capsys)) == 1


def test_the_level_filters_quieter_messages(capsys):
    configure_logging("json", level="WARNING")
    structlog.get_logger().info("chatter")
    structlog.get_logger().warning("important")
    assert [json.loads(line)["message"] for line in lines(capsys)] == ["important"]


def test_console_mode_is_readable_text_not_json(capsys):
    configure_logging("console")
    structlog.get_logger().info("ingestion_complete", account_id="ACC-H-00000")

    [line] = lines(capsys)
    assert "ingestion_complete" in line and "ACC-H-00000" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_an_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="log_format must be one of"):
        configure_logging("xml")


def test_chatty_libraries_are_held_to_warning_but_our_own_events_still_show(capsys):
    configure_logging("json", level="INFO")
    logging.getLogger("databricks.sql.session").info("Successfully opened session 1234")
    logging.getLogger("databricks.sql.session").warning("slow response")
    structlog.get_logger().info("ingestion_complete", account_id="ACC-H-00000")

    messages = [json.loads(line)["message"] for line in lines(capsys)]
    assert messages == ["slow response", "ingestion_complete"]  # the library's INFO line is gone


def test_a_debug_run_still_quietens_the_libraries(capsys):
    configure_logging("json", level="DEBUG")
    logging.getLogger("httpx").info("HTTP Request: POST https://example.invalid")
    structlog.get_logger().debug("detail")
    assert [json.loads(line)["message"] for line in lines(capsys)] == ["detail"]
