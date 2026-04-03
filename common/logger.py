import os
import logging
import queue
from typing import Optional
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener, TimedRotatingFileHandler

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_log_queue: queue.Queue | None = None
_listener: QueueListener | None = None


def _create_logger(name: Optional[str] = None) -> logging.Logger:
    global _log_queue, _listener

    logger_name = name or "itsm"
    logger = logging.getLogger(logger_name)

    logger.setLevel(LOG_LEVEL)

    # IMPORTANT: do NOT clear handlers repeatedly
    if logger.handlers:
        return logger

    log_format = (
        "%(asctime)s - %(levelname)s - "
        "[%(threadName)s] [%(name)s:%(lineno)d] - %(message)s"
    )
    formatter = logging.Formatter(log_format)

    # Create shared queue + listener once
    if _log_queue is None:
        _log_queue = queue.Queue(-1)

        file_handler = TimedRotatingFileHandler(
            filename="logs/application.log",
            when="midnight",
            interval=1,
            backupCount=0,   # ← keep ALL logs
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(LOG_LEVEL)

        _listener = QueueListener(_log_queue, file_handler)
        _listener.start()

    # QueueHandler for worker threads
    queue_handler = QueueHandler(_log_queue)
    queue_handler.setLevel(LOG_LEVEL)

    logger.addHandler(queue_handler)
    logger.propagate = False

    return logger


# Global logger
logger = _create_logger()
