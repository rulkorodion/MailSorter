"""Автоматическое формирование правила (условия) для папки назначения."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import LlmConfig, MailboxConfig, load_mailbox_config, config_path
from .imap_client import ImapClient
from .llm_client import LlmClient

logger = logging.getLogger("mailsorter.autorule")

SUMMARY_SYSTEM = (
    "Ты помощник по анализу почты. "
    "По тексту письма дай краткое содержание в 1–2 предложениях: "
    "о чём письмо и зачем оно получателю. "
    "Без приветствий, подписей и цитирования исходного текста. "
    "Ответь только текстом саммари на русском языке."
)

SYNTH_SYSTEM = (
    "Ты эксперт по классификации почты. "
    "По набору кратких описаний писем, которые уже лежат в одной папке, "
    "сформулируй ОДНО чёткое правило (условие) на русском языке: "
    "какие письма должны попадать в эту папку. "
    "1–3 предложения, без списков, примеров и кавычек вокруг всего текста. "
    "Не упоминай конкретные имена людей и разовые события — обобщи. "
    "Ответь только текстом условия."
)

MIN_EMAILS = 3


@dataclass
class AutoRuleJob:
    id: str
    config_file: str
    folder: str
    limit: int
    status: str = "pending"  # pending|running|done|error|cancelled
    phase: str = ""  # summary|synthesize
    total: int = 0
    current: int = 0
    summaries: list[dict[str, Any]] = field(default_factory=list)
    draft_condition: str = ""
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "config_file": self.config_file,
            "folder": self.folder,
            "limit": self.limit,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "current": self.current,
            "summaries_count": len(self.summaries),
            "summaries": self.summaries[-5:],  # последние для UI
            "draft_condition": self.draft_condition,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class AutoRuleManager:
    def __init__(self) -> None:
        self._jobs: dict[str, AutoRuleJob] = {}
        self._lock = threading.Lock()

    def start(self, config_file: str, folder: str, limit: int = 25) -> AutoRuleJob:
        limit = max(MIN_EMAILS, min(int(limit or 25), 100))
        job = AutoRuleJob(
            id=uuid.uuid4().hex[:12],
            config_file=config_file,
            folder=folder.strip(),
            limit=limit,
            status="pending",
            started_at=datetime.now(),
        )
        with self._lock:
            self._jobs[job.id] = job
        t = threading.Thread(target=self._run, args=(job.id,), daemon=True)
        t.start()
        return job

    def get(self, job_id: str) -> AutoRuleJob | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancel_requested = True
        return True

    def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job.status = "running"
        job.phase = "summary"
        try:
            cfg = load_mailbox_config(config_path(job.config_file))
            llm = LlmClient(
                LlmConfig(
                    endpoint=cfg.llm.endpoint,
                    model=cfg.llm.model,
                    temperature=0.2,
                    max_tokens=200,
                    timeout_seconds=cfg.llm.timeout_seconds,
                    max_retries=1,
                    system_prompt=SUMMARY_SYSTEM,
                )
            )

            with ImapClient(cfg.imap) as imap:
                messages = imap.fetch_folder_messages(job.folder, limit=job.limit)
                if len(messages) < MIN_EMAILS:
                    raise RuntimeError(
                        f"В папке «{job.folder}» слишком мало писем "
                        f"({len(messages)}). Нужно минимум {MIN_EMAILS}."
                    )
                job.total = len(messages)
                logger.info(
                    "Автоправило %s: папка=%s писем=%d",
                    job.id,
                    job.folder,
                    job.total,
                )

                for i, msg in enumerate(messages):
                    if job.cancel_requested:
                        job.status = "cancelled"
                        job.finished_at = datetime.now()
                        return
                    job.current = i + 1
                    subject = msg.subject or "(без темы)"
                    sender = msg.from_ or ""
                    body = imap.get_message_text(msg, max_chars=2000)
                    user = (
                        f"Отправитель: {sender}\n"
                        f"Тема: {subject}\n"
                        f"Текст:\n{body}"
                    )
                    try:
                        summary = llm.chat(SUMMARY_SYSTEM, user, temperature=0.2, max_tokens=150)
                    except Exception as e:
                        logger.warning("Саммари uid=%s: %s", msg.uid, type(e).__name__)
                        summary = "(не удалось получить саммари)"
                    job.summaries.append(
                        {
                            "uid": str(msg.uid),
                            "subject": subject[:120],
                            "from": sender[:80],
                            "summary": summary,
                        }
                    )
                    time.sleep(0.15)

            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = datetime.now()
                return

            # Синтез правила
            job.phase = "synthesize"
            job.current = job.total
            lines = []
            for i, s in enumerate(job.summaries, 1):
                lines.append(f"{i}. [{s['subject']}] {s['summary']}")
            user_synth = (
                f"Папка: {job.folder}\n"
                f"Число писем: {len(job.summaries)}\n\n"
                f"Саммари писем:\n" + "\n".join(lines)
            )
            synth_llm = LlmClient(
                LlmConfig(
                    endpoint=cfg.llm.endpoint,
                    model=cfg.llm.model,
                    temperature=0.3,
                    max_tokens=300,
                    timeout_seconds=cfg.llm.timeout_seconds,
                    max_retries=1,
                    system_prompt=SYNTH_SYSTEM,
                )
            )
            draft = synth_llm.chat(SYNTH_SYSTEM, user_synth, temperature=0.3, max_tokens=300)
            # убрать обёртки в кавычки если модель добавила
            draft = draft.strip().strip('"').strip("«»").strip()
            job.draft_condition = draft
            job.status = "done"
            job.finished_at = datetime.now()
            logger.info("Автоправило %s готово", job.id)
        except Exception as e:
            logger.error("Автоправило %s ошибка: %s", job_id, type(e).__name__)
            job.status = "error"
            job.error = type(e).__name__
            job.finished_at = datetime.now()


autorule_manager = AutoRuleManager()
