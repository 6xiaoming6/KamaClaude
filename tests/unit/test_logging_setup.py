from __future__ import annotations

import io
import json
import logging

import pytest

from kama_claude.core.config import KamaConfig
from kama_claude.core.logging_setup import setup_logging


@pytest.fixture
def configured_logs(tmp_path, monkeypatch):
    root = logging.getLogger()
    old_handlers, old_level = list(root.handlers), root.level
    stream = io.StringIO()
    monkeypatch.setattr("kama_claude.core.logging_setup.sys.stderr", stream)
    config = KamaConfig()
    config.logging.format = "json"
    config.logging.file = str(tmp_path / "core.log")
    setup_logging(config)
    configured_handlers = list(root.handlers)
    try:
        yield stream, tmp_path / "core.log"
    finally:
        for handler in configured_handlers:
            handler.close()
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_json_messages_round_trip_in_console_and_file(configured_logs):
    stream, path = configured_logs
    message = '中文 "quoted" C:\\temp\\file\nnew line\ttab\rreturn'
    logging.getLogger("test.json").info("message=%s", message)
    for output in (stream.getvalue(), path.read_text(encoding="utf-8")):
        assert len(output.splitlines()) == 1
        record = json.loads(output)
        assert record["msg"] == "message=" + message
        assert record["level"] == "INFO" and record["source"] == "test.json"


def test_json_exception_remains_one_parseable_record(configured_logs):
    stream, path = configured_logs
    try:
        raise ValueError('bad "value"\nsecond line')
    except ValueError:
        logging.getLogger("test.json").exception("operation failed")
    for output in (stream.getvalue(), path.read_text(encoding="utf-8")):
        assert len(output.splitlines()) == 1
        record = json.loads(output)
        assert "operation failed" in record["msg"]
        assert 'ValueError: bad "value"' in record["msg"]
        assert "Traceback" in record["msg"]
