import os
import logging
from typing import Optional
from pathlib import Path

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _create_logger(name: Optional[str] = None) -> logging.Logger:
    logger_name = name or "itsm"
    logger = logging.getLogger(logger_name)

    logger.setLevel(LOG_LEVEL)

    if logger.handlers:
        return logger

    log_format = (
        "%(asctime)s - %(levelname)s - "
        "[%(threadName)s] [%(name)s:%(lineno)d] - %(message)s"
    )
    formatter = logging.Formatter(log_format)

    # Console handler – always emit to terminal
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(LOG_LEVEL)
    logger.addHandler(console_handler)

    # File handler – plain append mode to avoid Windows file-lock issues with rotation
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(
        filename=log_dir / "application.log",
        mode="a",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(LOG_LEVEL)
    logger.addHandler(file_handler)

    logger.propagate = False

    return logger


# Global logger
logger = _create_logger()
