"""Работа с IMAP: подключение, получение писем, перемещение, создание папок."""

from __future__ import annotations

import imaplib
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from imap_tools import AND, Header, MailBox, MailMessage

from .config import ImapConfig

logger = logging.getLogger("mailsorter.imap")


MAX_DNS_ADDRESSES = 8
MAX_ADDRESS_SECONDS = 10


@dataclass(frozen=True)
class MoveResult:
    evidence: str  # server_ok or verified_after_error
    error_type: str | None = None


class MoveNotStartedError(RuntimeError):
    def __init__(self, stage: str, error_type: str):
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"IMAP MOVE не начат: {stage} ({error_type})")


class MoveUncertainError(RuntimeError):
    def __init__(self, stage: str, error_type: str):
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"Результат IMAP MOVE не определён: {stage} ({error_type})")


def _single_header(msg: MailMessage, name: str) -> str | None:
    values = msg.headers.get(name) or msg.headers.get(name.lower()) or ()
    return str(values[0]).strip() if len(values) == 1 and values[0] else None


def _message_identity(msg: MailMessage) -> tuple[str, int, str | None, str | None] | None:
    message_id = _single_header(msg, "Message-ID")
    size = getattr(msg, "size_rfc822", 0)
    if not message_id or not isinstance(size, int) or size <= 0:
        return None
    return message_id, size, _single_header(msg, "Date"), _single_header(msg, "From")


def _make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # Более совместимый набор для старых Exchange/корпоративных серверов
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    return ctx


def _open_tls_socket(
    address: tuple, family: int, socktype: int, proto: int,
    host: str, context: ssl.SSLContext, timeout: float,
) -> ssl.SSLSocket:
    """Connect to one resolved IP, verifying the original DNS name."""
    raw = socket.socket(family, socktype, proto)
    raw.settimeout(timeout)
    try:
        raw.connect(address)
        return context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise


class _PreconnectedIMAP4SSL(imaplib.IMAP4_SSL):
    """Let imaplib consume an existing TLS socket without resolving DNS again."""

    def __init__(self, host: str, port: int, tls_socket: ssl.SSLSocket,
                 context: ssl.SSLContext, timeout: float):
        self._provided_socket = tls_socket
        try:
            super().__init__(host, port, ssl_context=context, timeout=timeout)
        except Exception:
            tls_socket.close()
            raise

    def _create_socket(self, timeout: float) -> ssl.SSLSocket:
        sock = self._provided_socket
        self._provided_socket = None
        return sock


class _PreconnectedMailBox(MailBox):
    """imap-tools adapter preserving its login/folder interface."""

    def __init__(self, host: str, port: int, tls_socket: ssl.SSLSocket,
                 context: ssl.SSLContext, timeout: float):
        self._provided_socket = tls_socket
        super().__init__(host, port=port, timeout=timeout, ssl_context=context)

    def _get_mailbox_client(self) -> imaplib.IMAP4_SSL:
        return _PreconnectedIMAP4SSL(
            self._host, self._port, self._provided_socket,
            self._ssl_context, self._timeout,
        )


class ImapClient:
    def __init__(self, cfg: ImapConfig, protected_folders: list[str] | None = None):
        self.cfg = cfg
        self.protected_folders = protected_folders or []
        self._mailbox: MailBox | None = None
        self._folder_delimiter: str = "/"
        self._selected_folder: str | None = None
        self._selected_uidvalidity: int | None = None

    @property
    def selected_uidvalidity(self) -> int | None:
        return self._selected_uidvalidity

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.disconnect()

    def connect(self) -> None:
        password = self.cfg.resolve_password()
        host = self.cfg.host.strip()
        port = int(self.cfg.port)
        timeout = int(getattr(self.cfg, "timeout_seconds", 30) or 30)
        if not self.cfg.ssl_verify:
            raise RuntimeError("IMAP TLS: проверка сертификата обязательна")
        username = self.cfg.username.strip()

        logger.info(
            "IMAP connect: host=%s port=%s ssl_verify=true timeout=%s",
            host, port, timeout,
        )

        context = _make_ssl_context()
        deadline = time.monotonic() + timeout
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            logger.warning("IMAP DNS %s failed: %s", host, type(exc).__name__)
            raise RuntimeError("Не удалось разрешить DNS-имя IMAP-сервера") from None

        candidates = []
        seen = set()
        for family, socktype, proto, _canonname, address in resolved:
            key = (family, socktype, proto, address)
            if key not in seen:
                seen.add(key)
                candidates.append((family, socktype, proto, address))
            if len(candidates) >= MAX_DNS_ADDRESSES:
                break
        if not candidates:
            raise RuntimeError("DNS не вернул адресов IMAP-сервера")

        last_error = "нет ответа"
        for family, socktype, proto, address in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt_timeout = min(remaining, MAX_ADDRESS_SECONDS)
            ip = address[0]
            logger.info("IMAP TLS attempt: ip=%s port=%s", ip, port)
            try:
                tls_socket = _open_tls_socket(
                    address, family, socktype, proto, host, context, attempt_timeout,
                )
            except (ssl.SSLCertVerificationError, ssl.CertificateError):
                logger.error("IMAP TLS certificate verification failed: ip=%s", ip)
                raise RuntimeError("Ошибка проверки TLS-сертификата IMAP") from None
            except (OSError, ssl.SSLError) as exc:
                last_error = type(exc).__name__
                logger.warning("IMAP TLS attempt failed: ip=%s reason=%s", ip, last_error)
                continue

            try:
                mailbox = _PreconnectedMailBox(
                    host, port, tls_socket, context, attempt_timeout,
                )
            except (ssl.SSLCertVerificationError, ssl.CertificateError):
                tls_socket.close()
                logger.error("IMAP TLS certificate verification failed: ip=%s", ip)
                raise RuntimeError("Ошибка проверки TLS-сертификата IMAP") from None
            except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
                tls_socket.close()
                last_error = type(exc).__name__
                logger.warning("IMAP greeting failed: ip=%s reason=%s", ip, last_error)
                continue
            except Exception:
                tls_socket.close()
                raise

            try:
                mailbox.login(username, password)
            except Exception as exc:
                # Do not retry another IP after credentials have been sent.
                try:
                    mailbox.client.shutdown()
                except Exception:
                    tls_socket.close()
                logger.warning("IMAP login failed: ip=%s reason=%s", ip, type(exc).__name__)
                raise RuntimeError(f"Ошибка IMAP-аутентификации ({type(exc).__name__})") from None

            tls_socket.settimeout(timeout)
            self._mailbox = mailbox
            logger.info("IMAP TLS connected: ip=%s host=%s", ip, host)
            break
        else:
            raise RuntimeError(f"IMAP TLS недоступен по DNS-адресам ({last_error})")
        if self._mailbox is None:
            raise RuntimeError(f"Превышено время подключения к IMAP ({last_error})")

        # --- Этап 3: папки и INBOX ---
        try:
            for f in self._mailbox.folder.list():
                delim = getattr(f, "delim", None) or getattr(f, "delimiter", None)
                if delim:
                    self._folder_delimiter = delim
                    break
        except Exception:
            pass

        folders = self.list_folders()
        logger.info(
            "IMAP OK: %s@%s | папок=%d | delim=%r",
            username, host, len(folders), self._folder_delimiter,
        )

        inbox = self._resolve_folder_name(self.cfg.inbox_folder, folders)
        if not inbox:
            available = ", ".join(folders[:30]) or "(пусто)"
            self.disconnect()
            raise RuntimeError(
                f"Папка входящих «{self.cfg.inbox_folder}» не найдена. Доступные: {available}"
            )
        if inbox != self.cfg.inbox_folder:
            logger.info("INBOX: конфиг=%r → сервер=%r", self.cfg.inbox_folder, inbox)
            self.cfg.inbox_folder = inbox
        try:
            self._mailbox.folder.set(self.cfg.inbox_folder)
            self._remember_selected_folder(self.cfg.inbox_folder)
        except Exception as e:
            available = ", ".join(folders[:30]) or "(пусто)"
            self.disconnect()
            raise RuntimeError(
                f"Не удалось открыть «{self.cfg.inbox_folder}»: {e}. Доступные: {available}"
            ) from e

    def disconnect(self) -> None:
        if self._mailbox:
            try:
                self._mailbox.logout()
            except Exception as exc:
                logger.warning("IMAP stage=logout error_type=%s", type(exc).__name__)
                try:
                    self._mailbox.client.shutdown()
                except Exception:
                    pass
            self._mailbox = None
            self._selected_folder = None
            self._selected_uidvalidity = None

    @property
    def mailbox(self) -> MailBox:
        if not self._mailbox:
            raise RuntimeError("IMAP не подключён")
        return self._mailbox

    def list_folders(self) -> list[str]:
        try:
            return [f.name for f in self.mailbox.folder.list()]
        except Exception as e:
            logger.warning("Не удалось получить список папок: %s", type(e).__name__)
            return []

    def _resolve_folder_name(self, wanted: str, folders: list[str] | None = None) -> str | None:
        if folders is None:
            folders = self.list_folders()
        if not wanted:
            return None
        if wanted in folders:
            return wanted
        lower_map = {f.lower(): f for f in folders}
        if wanted.lower() in lower_map:
            return lower_map[wanted.lower()]
        if wanted.upper() == "INBOX" and "inbox" in lower_map:
            return lower_map["inbox"]
        return None

    def ensure_folder(self, folder: str) -> str:
        existing = self.list_folders()
        resolved = self._resolve_folder_name(folder, existing)
        if resolved:
            return resolved
        norm = folder.replace("\\", "/").replace("/", self._folder_delimiter)
        parts = [p for p in norm.split(self._folder_delimiter) if p]
        current = ""
        for part in parts:
            current = f"{current}{self._folder_delimiter}{part}" if current else part
            found = self._resolve_folder_name(current, existing)
            if found:
                current = found
                continue
            try:
                self.mailbox.folder.create(current)
                logger.info("Создана папка: %s", current)
                existing.append(current)
            except Exception as e:
                existing = self.list_folders()
                found = self._resolve_folder_name(current, existing)
                if found:
                    current = found
                    continue
                logger.warning("Не удалось создать папку %s: %s", current, type(e).__name__)
                raise RuntimeError(
                    f"Не удалось создать папку «{current}» ({type(e).__name__}). "
                    f"Доступные: {', '.join(existing[:20])}"
                ) from e
        return current

    def fetch_inbox_messages(self, limit: int = 50) -> list[MailMessage]:
        return self.fetch_folder_messages(self.cfg.inbox_folder, limit=limit)

    def fetch_folder_messages(self, folder: str, limit: int = 50) -> list[MailMessage]:
        existing = self.list_folders()
        actual = self._resolve_folder_name(folder, existing)
        if not actual:
            raise RuntimeError(
                f"Папка «{folder}» не найдена. Доступные: {', '.join(existing[:25])}"
            )
        self.mailbox.folder.set(actual)
        self._remember_selected_folder(actual)
        return list(self.mailbox.fetch(limit=limit, reverse=True, mark_seen=False))

    def _remember_selected_folder(self, actual: str) -> None:
        self._selected_folder = actual
        try:
            responses = self.mailbox.client.untagged_responses
            selected = responses.get("UIDVALIDITY", [None])[0] if isinstance(responses, dict) else None
            self._selected_uidvalidity = int(selected) if selected is not None else None
            if self._selected_uidvalidity is None:
                self._selected_uidvalidity = self.mailbox.folder.status(
                    actual, options=("UIDVALIDITY",),
                ).get("UIDVALIDITY")
        except Exception as exc:
            self._selected_uidvalidity = None
            logger.warning("IMAP stage=source_uidvalidity error_type=%s", type(exc).__name__)

    def move_message(self, msg: MailMessage, target_folder: str,
                     before_command: Callable[[], None] | None = None) -> MoveResult:
        """Never retry a possibly executed MOVE; verify by identity on a new read-only session."""
        uid = str(msg.uid or "")
        if not uid.isdigit():
            raise MoveNotStartedError("validate_uid", "InvalidUID")
        source = self._selected_folder
        if not source:
            raise MoveNotStartedError("validate_source", "UnknownSource")
        try:
            actual = self.ensure_folder(target_folder)
        except Exception as exc:
            logger.warning("IMAP stage=prepare_destination uid=%s target=%s error_type=%s",
                           uid, target_folder, type(exc).__name__)
            raise MoveNotStartedError("prepare_destination", type(exc).__name__) from exc

        baseline: dict[str, int] = {}
        try:
            baseline = self.mailbox.folder.status(
                actual, options=("UIDVALIDITY", "UIDNEXT"),
            )
        except imaplib.IMAP4.abort as exc:
            logger.warning("IMAP stage=target_baseline uid=%s target=%s error_type=%s",
                           uid, actual, type(exc).__name__)
            raise MoveNotStartedError("target_baseline", type(exc).__name__) from exc
        except Exception as exc:
            logger.warning("IMAP stage=target_baseline uid=%s target=%s error_type=%s",
                           uid, actual, type(exc).__name__)

        method = "MOVE" if "MOVE" in self.mailbox.client.capabilities else "COPY_STORE_EXPUNGE"
        if before_command is not None:
            try:
                before_command()
            except Exception as exc:
                logger.error("IMAP stage=journal_pending uid=%s target=%s error_type=%s",
                             uid, actual, type(exc).__name__)
                raise MoveNotStartedError("journal_pending", type(exc).__name__) from exc

        logger.info("IMAP stage=move_command uid=%s target=%s method=%s", uid, actual, method)
        try:
            self.mailbox.move(uid, actual)
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("IMAP stage=move_command uid=%s target=%s method=%s error_type=%s",
                           uid, actual, method, error_type)
            try:
                verified = self._verify_move(msg, source, actual, self._selected_uidvalidity, baseline)
            except Exception as verify_exc:
                logger.warning("IMAP stage=verify_move uid=%s target=%s error_type=%s",
                               uid, actual, type(verify_exc).__name__)
                verified = False
            if verified:
                logger.warning("IMAP stage=verify_move uid=%s target=%s result=confirmed error_type=%s",
                               uid, actual, error_type)
                return MoveResult("verified_after_error", error_type)
            logger.error("IMAP stage=verify_move uid=%s target=%s result=unknown error_type=%s",
                         uid, actual, error_type)
            raise MoveUncertainError("move_command", error_type) from exc

        logger.info("IMAP stage=move_confirmed uid=%s target=%s evidence=server_ok", uid, actual)
        return MoveResult("server_ok")

    def _verify_move(self, msg: MailMessage, source: str, target: str,
                     source_uidvalidity: int | None, baseline: dict[str, int]) -> bool:
        identity = _message_identity(msg)
        target_validity = baseline.get("UIDVALIDITY")
        target_next = baseline.get("UIDNEXT")
        if (identity is None or not isinstance(source_uidvalidity, int) or source_uidvalidity <= 0
                or not isinstance(target_validity, int) or target_validity <= 0
                or not isinstance(target_next, int) or target_next <= 0):
            return False
        uid = str(msg.uid)
        try:
            with ImapClient(self.cfg) as checker:
                mailbox = checker.mailbox
                source_status = mailbox.folder.status(source, options=("UIDVALIDITY",))
                if source_status.get("UIDVALIDITY") != source_uidvalidity:
                    return False
                mailbox.folder.set(source, readonly=True)
                if uid in mailbox.uids(AND(uid=uid), charset=None):
                    return False

                target_status = mailbox.folder.status(target, options=("UIDVALIDITY",))
                if target_status.get("UIDVALIDITY") != target_validity:
                    return False
                mailbox.folder.set(target, readonly=True)
                candidates = mailbox.uids(
                    AND(header=Header("Message-ID", identity[0])), charset=None,
                )
                new_uids = [candidate for candidate in candidates
                            if candidate.isdigit() and int(candidate) >= target_next]
                if len(new_uids) != 1:
                    return False
                matches = list(mailbox.fetch(
                    uid_list=new_uids, headers_only=True, mark_seen=False,
                ))
                if (len(matches) != 1 or str(matches[0].uid) != new_uids[0]
                        or _message_identity(matches[0]) != identity):
                    return False
                if mailbox.folder.status(target, options=("UIDVALIDITY",)).get("UIDVALIDITY") != target_validity:
                    return False
                mailbox.folder.set(source, readonly=True)
                if mailbox.folder.status(source, options=("UIDVALIDITY",)).get("UIDVALIDITY") != source_uidvalidity:
                    return False
                return uid not in mailbox.uids(AND(uid=uid), charset=None)
        except Exception as exc:
            logger.warning("IMAP stage=verify_move uid=%s target=%s error_type=%s",
                           uid, target, type(exc).__name__)
            return False

    def create_folder(self, folder: str) -> str:
        return self.ensure_folder(folder)

    def delete_folder(self, folder: str) -> None:
        existing = self.list_folders()
        actual = self._resolve_folder_name(folder, existing)
        if not actual:
            raise RuntimeError(f"Папка «{folder}» не найдена")
        if actual.upper() == "INBOX":
            raise RuntimeError("Нельзя удалить папку INBOX")
        self._check_manageable(actual, existing)
        self.mailbox.folder.set(actual)
        if next(iter(self.mailbox.fetch(limit=1, mark_seen=False)), None) is not None:
            raise RuntimeError("Нельзя удалить непустую папку")
        self.mailbox.folder.delete(actual)
        logger.info("Удалена папка: %s", actual)

    def rename_folder(self, old_name: str, new_name: str) -> None:
        existing = self.list_folders()
        actual = self._resolve_folder_name(old_name, existing)
        if not actual:
            raise RuntimeError(f"Папка «{old_name}» не найдена")
        if actual.upper() == "INBOX":
            raise RuntimeError("Нельзя переименовать INBOX")
        self._check_manageable(actual, existing)
        self._check_manageable(new_name, existing)
        parts = new_name.replace("\\", "/").split("/")
        if len(parts) > 1:
            parent = self._folder_delimiter.join(parts[:-1])
            try:
                self.ensure_folder(parent)
            except Exception:
                pass
        self.mailbox.folder.rename(actual, new_name)
        logger.info("Папка переименована: %s → %s", actual, new_name)

    def _check_manageable(self, folder: str, existing: list[str]) -> None:
        protected = {"inbox", "sent", "sent items", "drafts", "trash", "deleted items", "junk", "junk email", "spam", "archive"}
        leaf = re.split(r"[/\\.]", folder.lower())[-1]
        if leaf in protected:
            raise RuntimeError("Операция с системной папкой запрещена")
        for used in self.protected_folders:
            used_lower = used.replace("\\", "/").replace("/", self._folder_delimiter).casefold()
            folder_lower = folder.casefold()
            if used_lower == folder_lower or used_lower.startswith(folder_lower + self._folder_delimiter):
                raise RuntimeError("Папка используется конфигурацией")
        for item in self.mailbox.folder.list():
            if item.name.lower() == folder.lower():
                flags = {str(flag).lower() for flag in getattr(item, "flags", ())}
                if flags.intersection({"\\sent", "\\drafts", "\\trash", "\\junk", "\\archive", "\\all"}):
                    raise RuntimeError("Операция с системной папкой запрещена")
        if any(other != folder and other.startswith(folder + self._folder_delimiter) for other in existing):
            raise RuntimeError("Операция с папкой, содержащей вложенные папки, запрещена")

    def get_message_text(self, msg: MailMessage, max_chars: int = 3000) -> str:
        text = msg.text or ""
        if not text and msg.html:
            text = re.sub(r"<[^>]+>", " ", msg.html)
            text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
