"""Ядро: опрос ящика, классификация, перемещение."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import MailboxConfig, build_system_prompt
from .imap_client import ImapClient
from .llm_client import LlmClient

logger = logging.getLogger("mailsorter.sorter")


@dataclass
class RunStats:
    processed: int = 0
    moved: int = 0
    unsorted: int = 0
    errors: int = 0
    planned: int = 0
    mode: str = "dry_run"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = "idle"
    last_error: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)


class MailSorter:
    def __init__(self, config: MailboxConfig):
        self.config = config
        self.stats = RunStats()
        self._running = False
        self._run_lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    def run_once(self, mode: str = "dry_run") -> RunStats:
        if mode not in {"dry_run", "active"}:
            raise ValueError("Недопустимый режим обработки")
        if not self._run_lock.acquire(blocking=False):
            logger.warning("[%s] Уже выполняется, пропускаем", self.name)
            return self.stats

        self._running = True
        self.stats = RunStats(status="running", mode=mode, started_at=datetime.now())
        logger.info("[%s] Начало обработки, mode=%s", self.name, mode)

        try:
            prompt = build_system_prompt(self.config.rules.destinations)
            llm_cfg = self.config.llm.model_copy(update={"system_prompt": prompt})
            with ImapClient(self.config.imap) as imap, LlmClient(llm_cfg) as llm:
                source = self.config.rules.source_folder
                messages = imap.fetch_folder_messages(source, limit=100)
                logger.info("[%s] Писем в %s: %d", self.name, source, len(messages))

                folder_set = {d.folder for d in self.config.rules.destinations}
                folder_lower = {d.folder.lower(): d.folder for d in self.config.rules.destinations}
                threshold = self.config.rules.confidence_threshold
                max_retries = 1 if llm_cfg.provider == "cloudru" else llm_cfg.max_retries
                unsorted = self.config.imap.unsorted_folder

                for msg in messages:
                    self.stats.processed += 1
                    detail: dict[str, Any] = {"uid": str(msg.uid)}
                    try:
                        subject = msg.subject or "(без темы)"
                        sender = msg.from_ or "(unknown)"
                        body = imap.get_message_text(msg)
                        result = None
                        for attempt in range(1, max(1, max_retries) + 1):
                            try:
                                result = llm.classify(subject, sender, body)
                                break
                            except Exception as e:
                                detail["reason"] = f"LLM {type(e).__name__}"
                                logger.warning("[%s] uid=%s LLM attempt=%d/%d error=%s", self.name, msg.uid, attempt, max(1, max_retries), type(e).__name__)
                                if attempt < max(1, max_retries):
                                    time.sleep(2)
                        if result is None:
                            self.stats.errors += 1
                            detail["action"] = "retry_next_run"
                            self.stats.details.append(detail)
                            continue

                        category = (result.get("category") or "").strip()
                        if not category:
                            raise ValueError("Пустая категория модели")
                        confidence = float(result.get("confidence") or 0)
                        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                            raise ValueError("Некорректная уверенность модели")
                        target = category if category in folder_set else folder_lower.get(category.lower())
                        if not target:
                            raise ValueError("Неизвестная категория модели")
                        destination = target if target and confidence >= threshold else unsorted
                        detail.update(category=category, confidence=confidence, folder=destination)
                        if result.get("reason"):
                            detail["reason"] = result["reason"][:500]
                        if "latency_ms" in result:
                            detail["latency_ms"] = result["latency_ms"]
                        if "usage" in result:
                            detail["usage"] = result["usage"]
                        if confidence < threshold:
                            detail["reason"] = "low_confidence"
                        if mode == "dry_run":
                            self.stats.planned += 1
                            detail["action"] = "planned"
                        else:
                            imap.move_message(msg, destination)
                            if destination == unsorted:
                                self.stats.unsorted += 1
                                detail["action"] = "unsorted"
                            else:
                                self.stats.moved += 1
                                detail["action"] = "moved"
                    except Exception as e:
                        self.stats.errors += 1
                        detail.update(action="error", reason=type(e).__name__)
                        logger.warning("[%s] uid=%s processing error=%s", self.name, msg.uid, type(e).__name__)
                    self.stats.details.append(detail)

            self.stats.status = "done"
            self.stats.finished_at = datetime.now()
            logger.info(
                "[%s] Готово: processed=%d planned=%d moved=%d unsorted=%d errors=%d",
                self.name, self.stats.processed, self.stats.planned,
                self.stats.moved, self.stats.unsorted, self.stats.errors,
            )
        except Exception as e:
            self.stats.status = "error"
            self.stats.last_error = type(e).__name__
            self.stats.finished_at = datetime.now()
            logger.error("[%s] Критическая ошибка: %s", self.name, type(e).__name__)
        finally:
            self._running = False
            self._run_lock.release()

        return self.stats

    def test_connection(self) -> dict[str, Any]:
        try:
            with ImapClient(self.config.imap) as imap:
                folders = imap.list_folders()
                return {
                    "ok": True,
                    "message": f"OK: {self.config.imap.username}@{self.config.imap.host}",
                    "folders_count": len(folders),
                    "inbox": self.config.imap.inbox_folder,
                }
        except Exception as e:
            return {"ok": False, "message": type(e).__name__}

    def check_folders(self, create: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "folders": [], "errors": []}
        try:
            with ImapClient(self.config.imap) as imap:
                existing = set(imap.list_folders())
                needed = [self.config.imap.unsorted_folder, self.config.rules.source_folder]
                needed += [d.folder for d in self.config.rules.destinations]
                seen: set[str] = set()
                unique = []
                for f in needed:
                    if f and f not in seen:
                        seen.add(f)
                        unique.append(f)
                for folder in unique:
                    found = imap._resolve_folder_name(folder, list(existing))
                    if found:
                        result["folders"].append({"name": folder, "status": "exists", "actual": found})
                    elif create:
                        try:
                            actual = imap.ensure_folder(folder)
                            existing.add(actual)
                            result["folders"].append({"name": folder, "status": "created", "actual": actual})
                        except Exception as e:
                            result["ok"] = False
                            result["folders"].append({"name": folder, "status": "error", "error": type(e).__name__})
                            result["errors"].append(type(e).__name__)
                    else:
                        result["ok"] = False
                        result["folders"].append({"name": folder, "status": "missing"})
        except Exception as e:
            result["ok"] = False
            result["errors"].append(type(e).__name__)
        return result

    def folder_tree(self) -> dict[str, Any]:
        try:
            with ImapClient(self.config.imap) as imap:
                folders = imap.list_folders()
                delim = imap._folder_delimiter
                return {
                    "ok": True,
                    "folders": folders,
                    "delimiter": delim,
                    "tree": _build_tree(folders, delim),
                }
        except Exception as e:
            return {"ok": False, "error": type(e).__name__, "folders": [], "tree": []}

    def list_messages(self, folder: str | None = None, limit: int = 30) -> dict[str, Any]:
        folder = folder or self.config.rules.source_folder
        try:
            with ImapClient(self.config.imap) as imap:
                messages = imap.fetch_folder_messages(folder, limit=limit)
                items = []
                for msg in messages:
                    items.append({
                        "uid": msg.uid,
                        "from": msg.from_ or "",
                        "subject": (msg.subject or "")[:200],
                        "date": str(msg.date) if msg.date else "",
                        "text_preview": imap.get_message_text(msg, max_chars=500),
                        "text": imap.get_message_text(msg, max_chars=3000),
                    })
                return {"ok": True, "folder": folder, "messages": items}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__, "messages": []}


def _build_tree(folders: list[str], delim: str = "/") -> list[dict]:
    root: dict[str, Any] = {"name": "", "path": "", "children": {}}
    for full in sorted(folders):
        if delim != "/" and delim in full:
            parts = [p for p in full.split(delim) if p]
        else:
            parts = [p for p in full.replace("\\", "/").split("/") if p]
        node = root
        path_so_far = ""
        for part in parts:
            path_so_far = f"{path_so_far}{delim}{part}" if path_so_far else part
            if part not in node["children"]:
                node["children"][part] = {"name": part, "path": path_so_far, "children": {}}
            node = node["children"][part]

    def to_list(n: dict) -> list[dict]:
        return [
            {"name": c["name"], "path": c["path"], "children": to_list(c)}
            for c in n["children"].values()
        ]

    return to_list(root)
