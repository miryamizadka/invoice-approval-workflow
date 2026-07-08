"""Structured JSON logging (M14) - stdlib only, no new dependency.

Duplicated from services/intake/logging_config.py / services/decision/service/
logging_config.py rather than imported - generic technical infrastructure,
not a business rule; each service owns its own copy so one can evolve
without coupling to the others.
"""

from __future__ import annotations

import json
import logging


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per log line, with correlation_id when present."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    """Route all logging through the JSON formatter. Call once at startup."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
