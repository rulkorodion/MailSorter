"""Менеджер всех ящиков: загрузка конфигов, планировщик, статус."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import MailboxConfig, config_path, load_all_configs, load_mailbox_config
from .sorter import MailSorter, RunStats

logger = logging.getLogger("mailsorter.manager")


class SorterManager:
    def __init__(self) -> None:
        self.sorters: dict[str, MailSorter] = {}  # key = config_file name
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._lock = threading.Lock()
        self._job_ids: dict[str, str] = {}
        self.global_stats: dict[str, RunStats] = {}
        self._active_files: set[str] = set()

    def mode_for(self, fname: str) -> str:
        with self._lock:
            return "active" if fname in self._active_files else "dry_run"

    def set_mode(self, fname: str, mode: str, confirmation: str = "") -> str:
        with self._lock:
            if fname not in self.sorters:
                raise ValueError("Ящик не найден")
            if mode == "active":
                if confirmation != f"ACTIVE {fname}":
                    raise ValueError("Требуется точное подтверждение ACTIVE <имя файла>")
                self._active_files.add(fname)
            elif mode == "dry_run":
                self._active_files.discard(fname)
            else:
                raise ValueError("Недопустимый режим")
            return mode

    def start(self) -> None:
        self.reload_all()
        self.scheduler.start()
        logger.info("Планировщик запущен")

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

    def reload_all(self) -> None:
        """Перечитывает все конфиги и обновляет задачи."""
        with self._lock:
            configs = load_all_configs()
            current_files = set()

            for cfg in configs:
                fname = cfg.config_file or cfg.name
                current_files.add(fname)
                if fname in self.sorters:
                    # Обновляем конфиг существующего
                    self.sorters[fname].config = cfg
                    self._active_files.discard(fname)
                    self._reschedule(fname, cfg.imap.poll_interval_seconds)
                else:
                    sorter = MailSorter(cfg)
                    self.sorters[fname] = sorter
                    self._schedule(fname, cfg.imap.poll_interval_seconds)
                    logger.info("Добавлен ящик: %s (%s)", cfg.name, fname)

            # Удаляем исчезнувшие
            for fname in list(self.sorters.keys()):
                if fname not in current_files:
                    self._unschedule(fname)
                    del self.sorters[fname]
                    self._active_files.discard(fname)
                    logger.info("Удалён ящик: %s", fname)

    def reload_one(self, filename: str) -> None:
        path = config_path(filename)
        if not path.exists():
            self._unschedule(filename)
            self.sorters.pop(filename, None)
            self._active_files.discard(filename)
            return
        cfg = load_mailbox_config(path)
        with self._lock:
            if filename in self.sorters:
                self.sorters[filename].config = cfg
                self._active_files.discard(filename)
                self._reschedule(filename, cfg.imap.poll_interval_seconds)
            else:
                self.sorters[filename] = MailSorter(cfg)
                self._schedule(filename, cfg.imap.poll_interval_seconds)

    def _schedule(self, fname: str, interval: int) -> None:
        job_id = f"poll_{fname}"
        self.scheduler.add_job(
            self._run_job,
            trigger=IntervalTrigger(seconds=interval),
            id=job_id,
            args=[fname],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._job_ids[fname] = job_id
        logger.info("Запланирован опрос %s каждые %d сек", fname, interval)

    def _reschedule(self, fname: str, interval: int) -> None:
        self._schedule(fname, interval)

    def _unschedule(self, fname: str) -> None:
        job_id = self._job_ids.pop(fname, None)
        if job_id:
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass

    def _run_job(self, fname: str) -> None:
        sorter = self.sorters.get(fname)
        if not sorter:
            return
        try:
            stats = sorter.run_once(mode=self.mode_for(fname))
            self.global_stats[fname] = stats
        except Exception as e:
            logger.error("Ошибка фонового задания %s: %s", fname, type(e).__name__)

    def run_now(self, fname: str | None = None) -> dict[str, Any]:
        """Принудительный запуск (одного или всех)."""
        results = {}
        targets = [fname] if fname else list(self.sorters.keys())
        for f in targets:
            sorter = self.sorters.get(f)
            if not sorter:
                results[f] = {"error": "not found"}
                continue
            # Запускаем в отдельном потоке, чтобы не блокировать веб
            def _run(s=sorter, name=f):
                stats = s.run_once(mode=self.mode_for(name))
                self.global_stats[name] = stats
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            results[f] = {"status": "started"}
        return results

    def get_status(self) -> list[dict[str, Any]]:
        out = []
        for fname, sorter in self.sorters.items():
            stats = self.global_stats.get(fname) or sorter.stats
            out.append({
                "config_file": fname,
                "name": sorter.name,
                "provider": sorter.config.llm.provider,
                "model": sorter.config.llm.model,
                "status": stats.status,
                "processed": stats.processed,
                "moved": stats.moved,
                "unsorted": stats.unsorted,
                "planned_unsorted": stats.planned_unsorted,
                "errors": stats.errors,
                "planned": stats.planned,
                "mode": self.mode_for(fname),
                "details": stats.details[-100:],
                "started_at": stats.started_at.isoformat() if stats.started_at else None,
                "finished_at": stats.finished_at.isoformat() if stats.finished_at else None,
                "last_error": stats.last_error,
                "poll_interval": sorter.config.imap.poll_interval_seconds,
            })
        return out

    def get_sorter(self, fname: str) -> MailSorter | None:
        return self.sorters.get(fname)


# Глобальный экземпляр
manager = SorterManager()
