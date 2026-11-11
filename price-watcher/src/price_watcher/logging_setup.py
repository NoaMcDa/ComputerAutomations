from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from price_watcher.paths import log_dir


_configured = False


def configure(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return

    log_path: Path = log_dir() / "price-watcher.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)
    root.addHandler(stream)
    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
