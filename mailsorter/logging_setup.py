"""Настройка логирования с ротацией."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_all_configs
from .security import sanitize_log


class _SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_log(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.stack_info = None
        return True


def setup_logging() -> None:
    configs = load_all_configs()
    if configs:
        cfg = configs[0].logging
        level = getattr(logging, cfg.level.upper(), logging.INFO)
        log_file = Path(cfg.file)
        max_bytes = cfg.max_size_mb * 1024 * 1024
        backup = cfg.backup_count
    else:
        level = logging.INFO
        log_file = Path("logs/mailsorter.log")
        max_bytes = 50 * 1024 * 1024
        backup = 3

    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Убираем старые хендлеры
    for h in root.handlers[:]:
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(_SecretFilter())
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(_SecretFilter())
    root.addHandler(ch)

    # Не спамим сторонние библиотеки
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
