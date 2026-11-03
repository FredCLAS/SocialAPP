"""Logging to a local file with a redaction filter for token-like strings."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "SocialOps" / "socialops.log"

_REDACT = [
    re.compile(r"(access_token|refresh_token|client_secret|api_key|password)=([^&\s]+)", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
]


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _REDACT:
            msg = pat.sub(lambda m: f"{m.group(1)}=***" if m.lastindex == 2 else f"{m.group(1)}***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(path: Path | None = None, level: int = logging.INFO) -> Path:
    path = Path(path) if path else DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger("socialops")
    root.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    return path
