"""
mail-sorter — точка входа.
Один процесс: веб-интерфейс + фоновый опрос всех ящиков.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

import uvicorn

# Чтобы можно было запускать из любой директории
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mailsorter.config import load_all_configs
from mailsorter.logging_setup import setup_logging
from mailsorter.manager import manager
from mailsorter.webapp import app

logger = logging.getLogger("mailsorter")


def main() -> None:
    setup_logging()
    logger.info("=== Mail Sorter запускается ===")

    configs = load_all_configs()
    if not configs:
        logger.warning(
            "Конфиги не найдены — режим первоначальной настройки. "
            "Откройте веб-интерфейс и пройдите мастер."
        )
        host, port = "127.0.0.1", 8080
    else:
        host = configs[0].web.host
        port = configs[0].web.port

    manager.start()

    def shutdown(signum, frame):
        logger.info("Получен сигнал %s, останавливаемся...", signum)
        manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Веб-интерфейс: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
