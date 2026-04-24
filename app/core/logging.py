"""Structured logging setup using loguru with a JSON-friendly format."""

from __future__ import annotations

import logging
import sys

from loguru import logger

from app.core.settings import get_settings


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        serialize=settings.env != "development",
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers = [_InterceptHandler()]
        logging.getLogger(noisy).propagate = False
