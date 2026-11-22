from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACT_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r'(["\']?(?:password|passwd|pwd|device_key|bark_key|token|auth_token|authtoken|sessionId|cookie|secret|ticket|code|authorization)["\']?\s*[:=]\s*["\']?)([^"\'\s,&}]+)',
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (re.compile(r"(https?://[^/\s]+/)([A-Za-z0-9_-]{12,})(/|$)"), r"\1***\3"),
]


def redact_text(message: str) -> str:
    result = message
    for pattern, repl in _REDACT_PATTERNS:
        result = pattern.sub(repl, result)
    return result


class SensitiveRedactingFilter(logging.Filter):
    """Redacts passwords, tokens, session IDs, and Bark keys from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_text(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(level: str = "DEBUG", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        target = str(log_file.resolve())
        # A process owns one application log destination, including repeated app construction.
        for existing in list(root.handlers):
            if getattr(existing, "_autobuaa_file", False):
                if getattr(existing, "baseFilename", None) == target:
                    return
                root.removeHandler(existing)
                existing.close()
        file_handler = RotatingFileHandler(
            target, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler._autobuaa_file = True
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(SensitiveRedactingFilter())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.addFilter(SensitiveRedactingFilter())
