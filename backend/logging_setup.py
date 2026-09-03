"""Console + rotating-file logging, every line tagged with the booth id."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings


class _BoothFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.booth = settings.booth_id
        return True


def setup() -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(booth)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    flt = _BoothFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(flt)

    logfile = RotatingFileHandler(
        settings.log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    logfile.setFormatter(fmt)
    logfile.addFilter(flt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [console, logfile]
    logging.getLogger("httpx").setLevel(logging.WARNING)
