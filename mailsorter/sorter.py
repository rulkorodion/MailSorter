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
from .imap_client import ImapClient, MoveNotStartedError, MoveUncertainError
from .llm_client import LlmClient
from .move_journal import MoveJournal

logger = logging.getLogger("mailsorter.sorter")


@dataclass
class RunStats:
    processed: int = 0
    moved: int = 0
    unsorted: int = 0
    planned_unsorted: int = 0
    move_unknown: int = 0
    pending_review: int = 0
    errors: int = 0
    planned: int = 0
    mode: str = "dry_run"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = "idle"
    last_error: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)


def resolve_destination(result: dict[str, Any], config: MailboxConfig) -> tuple[str, float, str]:
    """Map a validated model decision to an allowed folder, including explicit abstention."""
    category = result.get("category")
    confidence = result.get("confidence")
    if not isinstance(category, str):
        raise ValueError("Некорректная категория модели")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Некорректная уверенность модели")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Некорректная уверенность модели")
    abstained = category == ""
    category = category.strip()
    if abstained:
        reason = result.get("reason")
        if result.get("abstained") is False or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Неподтверждённый отказ от классификации")
        return category, float(confidence), config.imap.unsorted_folder
    if not category or result.get("abstained") is True:
        raise ValueError("Некорректная категория модели")
    allowed = {d.folder.casefold(): d.folder for d in config.rules.destinations}
    target = allowed.get(category.casefold())
    if not target:
        raise ValueError("Неизвестная категория модели")
    destination = target if confidence >= config.rules.confidence_threshold else config.imap.unsorted_folder
    return target, float(confidence), destination


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

        journal: MoveJournal | None = None
        try:
            journal = MoveJournal(self.config) if mode == "active" else None
            prompt = build_system_prompt(self.config.rules.destinations)
            llm_cfg = self.config.llm.model_copy(update={"system_prompt": prompt})
            with ImapClient(self.config.imap) as imap, LlmClient(llm_cfg) as llm:
                source = self.config.rules.source_folder
                messages = imap.fetch_folder_messages(source, limit=100)
                logger.info("[%s] Писем в %s: %d", self.name, source, len(messages))

                threshold = self.config.rules.confidence_threshold
                max_retries = 1 if llm_cfg.provider == "cloudru" else llm_cfg.max_retries
                unsorted = self.config.imap.unsorted_folder

                for msg in messages:
                    self.stats.processed += 1
                    detail: dict[str, Any] = {"uid": str(msg.uid)}
                    move_key = None
                    if journal is not None:
                        move_key = journal.key(source, imap.selected_uidvalidity, str(msg.uid))
                        if move_key in journal.pending:
                            self.stats.move_unknown += 1
                            detail.update(action="move_pending_review", stage="journal_pending")
                            self.stats.details.append(detail)
                            continue
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

                        category, confidence, destination = resolve_destination(result, self.config)
                        detail.update(category=category, confidence=confidence, folder=destination)
                        if category == "":
                            detail["decision"] = "no_matching_category"
                        if result.get("reason"):
                            detail["reason"] = result["reason"][:500]
                        if "latency_ms" in result:
                            detail["latency_ms"] = result["latency_ms"]
                        if "usage" in result:
                            detail["usage"] = result["usage"]
                        if confidence < threshold and category != "":
                            detail["reason"] = "low_confidence"
                        if mode == "dry_run":
                            self.stats.planned += 1
                            if destination == unsorted:
                                self.stats.planned_unsorted += 1
                            detail["action"] = "planned"
                        else:
                            move_result = imap.move_message(
                                msg, destination,
                                before_command=lambda key=move_key: journal.begin(
                                    key, source=source, uidvalidity=imap.selected_uidvalidity,
                                    uid=str(msg.uid), target=destination,
                                ),
                            )
                            try:
                                journal.resolve(move_key)
                            except Exception as exc:
                                detail["journal_warning"] = type(exc).__name__
                                logger.error("[%s] stage=journal_resolve uid=%s target=%s error_type=%s",
                                             self.name, msg.uid, destination, type(exc).__name__)
                            detail["move_evidence"] = move_result.evidence
                            if move_result.error_type:
                                detail["stage"] = "verify_move"
                                detail["move_warning"] = move_result.error_type
                            if destination == unsorted:
                                self.stats.unsorted += 1
                                detail["action"] = "unsorted"
                            else:
                                self.stats.moved += 1
                                detail["action"] = "moved"
                    except MoveUncertainError as exc:
                        self.stats.errors += 1
                        self.stats.move_unknown += 1
                        detail.update(action="move_unknown", stage=exc.stage,
                                      reason=exc.error_type)
                        logger.error("[%s] stage=%s uid=%s target=%s error_type=%s outcome=unknown",
                                     self.name, exc.stage, msg.uid, detail.get("folder"), exc.error_type)
                    except MoveNotStartedError as exc:
                        self.stats.errors += 1
                        detail.update(action="move_not_started", stage=exc.stage,
                                      reason=exc.error_type)
                        logger.warning("[%s] stage=%s uid=%s target=%s error_type=%s outcome=not_started",
                                       self.name, exc.stage, msg.uid, detail.get("folder"), exc.error_type)
                    except Exception as e:
                        self.stats.errors += 1
                        detail.update(action="error", reason=type(e).__name__)
                        logger.warning("[%s] stage=processing uid=%s target=%s error_type=%s",
                                       self.name, msg.uid, detail.get("folder"), type(e).__name__)
                    self.stats.details.append(detail)

            if journal is not None:
                self.stats.pending_review = len(journal.pending)
            self.stats.status = "done"
            self.stats.finished_at = datetime.now()
            logger.info(
                "[%s] Готово: processed=%d planned=%d planned_unsorted=%d moved=%d unsorted=%d move_unknown=%d pending_review=%d errors=%d",
                self.name, self.stats.processed, self.stats.planned,
                self.stats.planned_unsorted, self.stats.moved, self.stats.unsorted,
                self.stats.move_unknown, self.stats.pending_review, self.stats.errors,
            )
        except Exception as e:
            self.stats.status = "error"
            self.stats.last_error = type(e).__name__
            self.stats.finished_at = datetime.now()
            logger.error("[%s] Критическая ошибка: %s", self.name, type(e).__name__)
        finally:
            if journal is not None:
                self.stats.pending_review = len(journal.pending)
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
