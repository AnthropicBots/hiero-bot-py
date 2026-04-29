# app/utils/logger.py — Structured logging

from __future__ import annotations
import logging
import sys
from app.utils.settings import settings


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"hiero.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    return logger
