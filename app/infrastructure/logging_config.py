"""Structured application logging configuration with secret redaction."""

from __future__ import annotations

import logging
import re
from pathlib import Path

_SENSITIVE_PATTERN = re.compile(
    r"(password|secret_key|csrf_token|session_token)\s*[=:]\s*([^\s,&]+)",
    re.IGNORECASE,
)


class SecretRedactingFilter(logging.Filter):
    """Ensure credentials, session tokens, or secrets are never written to logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SENSITIVE_PATTERN.sub(r"\1=[REDACTED]", record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: ("[REDACTED]" if "password" in k.lower() or "secret" in k.lower() else v)
                    for k, v in record.args.items()
                }
        return True


def configure_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Configure root/app logger safely without duplicating handlers."""
    logger = logging.getLogger("pustakalaya")
    logger.setLevel(level)
    if logger.handlers:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redactor = SecretRedactingFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)
    logger.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)

    logger.propagate = False
