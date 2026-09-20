"""Application logging setup."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import traceback

from sync_box.box_errors import safe_error_detail


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not log_file.exists():
            descriptor = os.open(
                log_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
        log_file.chmod(0o600)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=logging.INFO, format=LOG_FORMAT, handlers=handlers, force=True
    )


def log_failure(
    logger: logging.Logger, message: str, error: BaseException
) -> None:
    """Log a useful traceback without exposing chained exception details."""
    frames = traceback.extract_tb(error.__traceback__)
    locations = "\n".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
        for frame in frames
    )
    summary = (
        f"{message}: {type(error).__name__}: "
        f"{safe_error_detail(error)}"
    )
    if locations:
        logger.error("%s\nTraceback (most recent call last):\n%s", summary, locations)
    else:
        logger.error("%s", summary)
