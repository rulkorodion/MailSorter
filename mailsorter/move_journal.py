"""Durable write-ahead quarantine for MOVE commands with uncertain outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .config import MailboxConfig

_JOURNAL_LOCK = threading.Lock()
_JOURNAL_DIR = Path(__file__).resolve().parent.parent / "logs"


class MoveJournal:
    def __init__(self, config: MailboxConfig):
        mailbox_id = hashlib.sha256(
            f"{config.imap.host}\0{config.imap.username}".encode("utf-8")
        ).hexdigest()[:24]
        self.path = _JOURNAL_DIR / f"move-pending-{mailbox_id}.jsonl"
        self.pending: set[str] = set()
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("event") not in {"pending", "resolved"}:
                        raise ValueError("Некорректный журнал неопределённых MOVE")
                    key = record.get("key")
                    if not isinstance(key, str) or len(key) != 64:
                        raise ValueError("Некорректный ключ журнала MOVE")
                    if record["event"] == "pending":
                        self.pending.add(key)
                    else:
                        self.pending.discard(key)

    @staticmethod
    def key(source: str, uidvalidity: int | None, uid: str) -> str:
        # Quarantine by source UID even if a later connection cannot read
        # UIDVALIDITY. A changed UIDVALIDITY may conservatively block a new
        # message with the same number, but must never permit a duplicate MOVE.
        return hashlib.sha256(
            f"{source.casefold()}\0{uid}".encode("utf-8")
        ).hexdigest()

    def begin(self, key: str, *, source: str | None = None, uidvalidity: int | None = None,
              uid: str | None = None, target: str | None = None) -> None:
        record = {"event": "pending", "key": key}
        if source is not None and uid is not None and target is not None:
            record.update(source=source, uidvalidity=uidvalidity, uid=uid, target=target)
        self._append(record)
        self.pending.add(key)

    def resolve(self, key: str) -> None:
        self._append({"event": "resolved", "key": key})
        self.pending.discard(key)

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with _JOURNAL_LOCK:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if os.write(fd, data) != len(data):
                    raise OSError("Неполная запись журнала MOVE")
                os.fsync(fd)
            finally:
                os.close(fd)
