"""Structured JSON Logging Configuration."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Custom standard library logging formatter returning JSON lines.

    Includes standard log attributes along with any dynamic dictionary parameters
    passed via the ``extra`` parameter (e.g. ``logger.info("msg", extra={"symbol": "BTC"})``).
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "filename": record.filename,
            "lineno": record.lineno,
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Map any extra variables passed to the log call
        for key, val in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "msg", "name", "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName"
            }:
                log_data[key] = val

        return json.dumps(log_data)


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Initialize root logger handler with the custom JSON formatter."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clean existing handlers
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)

    # Upstream request-per-call INFO lines drown the causal engine's own
    # structured events during Radar refreshes. Failures still surface at
    # WARNING/ERROR, while successful requests remain observable through the
    # application's latency and source-coverage telemetry.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
