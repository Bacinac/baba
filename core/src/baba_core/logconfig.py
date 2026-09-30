"""Central logging configuration for BABA services.

Every service calls ``setup_logging("detector")`` once at startup instead of
repeating its own ``logging.basicConfig(...)``. The level is env-driven so an
operator can crank verbosity to diagnose a problem WITHOUT rebuilding an image:

    BABA_LOG_LEVEL=DEBUG              # every service
    BABA_LOG_LEVEL_DETECTOR=DEBUG    # just the detector (overrides the global)

Default is INFO. An unrecognised level string falls back to INFO with a loud
warning rather than silently picking a wrong level.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _JsonFormatter(logging.Formatter):
    """One JSON object per log line, for structured log ingestion (Loki, ES,
    `jq`). Enabled with BABA_LOG_FORMAT=json. Exceptions become an `exc` field."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


def _resolve_level(service: str) -> int:
    """Per-service override (`BABA_LOG_LEVEL_<SERVICE>`) wins over the global
    `BABA_LOG_LEVEL`; both default to INFO."""
    svc_key = "BABA_LOG_LEVEL_" + service.upper().replace("-", "_")
    raw = os.environ.get(svc_key) or os.environ.get("BABA_LOG_LEVEL") or "INFO"
    level = logging.getLevelName(raw.strip().upper())
    if not isinstance(level, int):
        # getLevelName returns a "Level <n>" string for unknown names.
        logging.getLogger("baba").warning(
            "Unknown log level %r (BABA_LOG_LEVEL[_%s]) — using INFO",
            raw, service.upper(),
        )
        return logging.INFO
    return level


def setup_logging(service: str) -> logging.Logger:
    """Configure root logging once and return the service's named logger.

    Returns ``logging.getLogger(f"baba.{service}")`` for convenience; services
    that already hold a module-level logger can ignore the return value.
    """
    level = _resolve_level(service)
    logging.basicConfig(level=level, format=_FORMAT)
    root = logging.getLogger()
    # basicConfig is a no-op if the root already has handlers (a library
    # configuring logging on import, or one of ours listening for the last
    # error), and a no-op here means no console output at all. Ask for what is
    # actually needed — somewhere for a record to be written — rather than for
    # an empty handler list.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream)
    # The env knob has to take effect in that case too.
    root.setLevel(level)
    # Opt-in structured logging: BABA_LOG_FORMAT=json swaps every handler's
    # formatter for one-JSON-object-per-line. Central here so all 7 services
    # get it from their single setup_logging() call, no per-entrypoint edits.
    if os.environ.get("BABA_LOG_FORMAT", "text").strip().lower() == "json":
        json_fmt = _JsonFormatter(service)
        for h in root.handlers:
            h.setFormatter(json_fmt)
    return logging.getLogger(f"baba.{service}")
