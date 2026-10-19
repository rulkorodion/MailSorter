"""Работа с IMAP: подключение, получение писем, перемещение, создание папок."""

from __future__ import annotations

import imaplib
import logging
import os
import re
import secrets
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from imap_tools import AND, Header, MailBox, MailMessage
from imap_tools.errors import MailboxCopyError, MailboxDeleteError, MailboxExpungeError
from imap_tools.utils import check_command_status, encode_folder

from .config import ImapConfig

logger = logging.getLogger("mailsorter.imap")


MAX_DNS_ADDRESSES = 8
MAX_ADDRESS_SECONDS = 10
_TAG_STATUS_SHAPE = re.compile(rb"^[!-~]+ (?:OK|NO|BAD)(?: |$)", re.ASCII)
_NUMERIC_UNTAGGED_SHAPE = re.compile(rb"^\* [0-9]+ [A-Z-]+(?: |$)", re.ASCII)


def _response_shape(line: bytes, tag_pattern: Any, pending_tag: bytes | None) -> dict[str, str | int | bool]:
    """Classify an IMAP line without retaining/logging its bytes or text."""
    if not line:
        kind = "empty"
    elif line.startswith(b"*"):
        kind = "untagged_candidate"
    elif line.startswith(b"+"):
        kind = "continuation_candidate"
    elif line[:1] in (b" ", b"\t"):
        kind = "leading_whitespace"
    elif 33 <= line[0] <= 126:
        kind = "tag_candidate"
    else:
        kind = "other"
    marker_spaces = (len(line[1:]) - len(line[1:].lstrip(b" "))
                     if line[:1] in (b"*", b"+") else 0)
    return {
        "length": len(line),
        "kind": kind,
        "marker_spaces": min(marker_spaces, 3),
        "parser_tag_match": bool(tag_pattern.match(line)),
        "current_tag_prefix": bool(pending_tag and line.startswith(pending_tag + b" ")),
        "tag_status_shape": bool(_TAG_STATUS_SHAPE.match(line)),
        "tag_status_casefold_shape": bool(_TAG_STATUS_SHAPE.match(line.upper())),
        "parser_untagged_match": bool(imaplib.Untagged_response.match(line)),
        "parser_numeric_match": bool(imaplib.Untagged_status.match(line)),
        "numeric_casefold_shape": bool(_NUMERIC_UNTAGGED_SHAPE.match(line.upper())),
        "parser_continuation_match": bool(imaplib.Continuation.match(line)),
        "non_ascii": any(byte >= 128 for byte in line),
        "nul": b"\x00" in line,
        "embedded_cr": b"\r" in line,
        "tab": b"\t" in line,
        "other_control": any(byte < 32 and byte not in (9, 13) for byte in line),
    }


def destination_path(folder: str, layout: str, inbox: str, delimiter: str) -> str:
    """Resolve a rule's logical name to one server path; explicit paths stay explicit."""
    if not folder or not folder.strip() or not delimiter:
        raise ValueError("Пустой путь папки")
    folder = folder.strip()
    if any(ord(char) < 32 for char in folder):
        raise ValueError("Недопустимый символ в имени папки")
    # Configuration paths use /; imap-tools accepts Unicode and handles IMAP UTF-7.
    explicit = "/" in folder or "\\" in folder or delimiter in folder
    normalized = folder.replace("\\", "/")
    if delimiter != "/":
        normalized = normalized.replace("/", delimiter)
    parts = normalized.split(delimiter)
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("Недопустимый путь папки")
    if parts[0].casefold() == "inbox":
        parts[0] = inbox
    elif layout == "inbox" and not explicit:
        parts.insert(0, inbox)
    return delimiter.join(parts)


@dataclass(frozen=True)
class MoveResult:
    evidence: str  # server_ok or verified_after_error
    error_type: str | None = None
    stage: str | None = field(default=None, compare=False)
    diagnostic_code: str | None = field(default=None, compare=False)
    phase: str | None = field(default=None, compare=False)


@dataclass
class _CommandTrace:
    operation: str
    phase: str = "before_submit"
    tag: bytes | None = None
    tagged_status: str | None = None


class MoveNotStartedError(RuntimeError):
    def __init__(self, stage: str, error_type: str):
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"IMAP MOVE не начат: {stage} ({error_type})")


class MoveUncertainError(RuntimeError):
    def __init__(self, stage: str, error_type: str, phase: str | None = None,
                 diagnostic_code: str | None = None):
        self.stage = stage
        self.error_type = error_type
        self.phase = phase
        self.diagnostic_code = diagnostic_code
        super().__init__(f"Результат IMAP MOVE не определён: {stage} ({error_type})")


def _safe_abort_code(exc: Exception) -> str:
    """Only emit a fixed category; abort text can contain raw server data."""
    if not isinstance(exc, imaplib.IMAP4.abort):
        return "other_error"
    reason = str(exc).casefold()
    if "unexpected tagged response" in reason:
        return "unexpected_tagged_response"
    if "unexpected response" in reason:
        return "unexpected_response"
    if "unterminated line" in reason:
        return "unterminated_line"
    if "socket error: eof" in reason:
        return "socket_eof"
    if "timed out" in reason or "timeout" in reason:
        return "timeout"
    if "socket error" in reason:
        return "socket_error"
    if "bye" in reason:
        return "server_bye"
    return "imap_abort_other"


def _safe_connection_state(client: Any) -> tuple[str, str]:
    state = getattr(client, "state", None)
    if state not in {"NONAUTH", "AUTH", "SELECTED", "LOGOUT"}:
        state = "unknown"
    sock = getattr(client, "sock", None)
    try:
        open_state = "open" if sock is not None and sock.fileno() >= 0 else "closed"
    except Exception:
        open_state = "unknown"
    return state, open_state


class ImapAuthenticationError(RuntimeError):
    def __init__(self, credential_source: str, error_type: str):
        self.credential_source = credential_source
        self.error_type = error_type
        super().__init__(f"Ошибка IMAP-аутентификации ({error_type})")


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
        self.last_operation_trace: _CommandTrace | None = None
        self._move_response_diagnostics = os.environ.get("MAILSORTER_IMAP_MOVE_DIAGNOSTICS") == "1"
        self.last_response_shape: dict[str, str | int | bool] | None = None
        try:
            super().__init__(host, port, ssl_context=context, timeout=timeout)
        except Exception:
            tls_socket.close()
            raise

    def _create_socket(self, timeout: float) -> ssl.SSLSocket:
        sock = self._provided_socket
        self._provided_socket = None
        return sock

    def _get_line(self) -> bytes:
        line = super()._get_line()
        trace = self.last_operation_trace
        if (self._move_response_diagnostics and trace is not None
                and trace.operation == "MOVE" and trace.phase == "await_tagged_response"):
            self.last_response_shape = _response_shape(line, self.tagre, trace.tag)
        return line

    def _command(self, name: str, *args: Any) -> bytes:
        operation = str(args[0]).upper() if name == "UID" and args else ""
        trace = None
        if operation in {"MOVE", "COPY", "STORE", "EXPUNGE"}:
            trace = _CommandTrace(operation)
            self.last_operation_trace = trace
            self.last_response_shape = None
        try:
            tag = super()._command(name, *args)
        except Exception:
            if trace is not None:
                trace.phase = "submission_unknown"
            raise
        if trace is not None:
            trace.tag = tag
            trace.phase = "await_tagged_response"
        return tag

    def _get_tagged_response(self, tag: bytes, expect_bye: bool = False) -> tuple:
        result = super()._get_tagged_response(tag, expect_bye=expect_bye)
        trace = self.last_operation_trace
        if trace is not None and trace.tag == tag:
            trace.tagged_status = result[0]
            trace.phase = "tagged_response_received"
        return result

    def _command_complete(self, name: str, tag: bytes) -> tuple:
        try:
            result = super()._command_complete(name, tag)
        except Exception:
            trace = self.last_operation_trace
            if trace is not None and trace.tag == tag:
                trace.phase = ("after_tagged_response" if trace.tagged_status is not None
                               else "await_tagged_response")
            raise
        trace = self.last_operation_trace
        if trace is not None and trace.tag == tag:
            trace.phase = "completed"
        return result


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
        self._session_id = secrets.token_hex(4)

    @property
    def selected_uidvalidity(self) -> int | None:
        return self._selected_uidvalidity

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.disconnect()

    def connect(self) -> None:
        credential_source = "environment" if self.cfg.password.startswith("ENV:") else "inline_yaml"
        password = self.cfg.resolve_password()
        host = self.cfg.host.strip()
        port = int(self.cfg.port)
        timeout = int(getattr(self.cfg, "timeout_seconds", 30) or 30)
        if not self.cfg.ssl_verify:
            raise RuntimeError("IMAP TLS: проверка сертификата обязательна")
        username = self.cfg.username.strip()

        logger.info(
            "IMAP connect: host=%s port=%s ssl_verify=true timeout=%s session=%s",
            host, port, timeout, self._session_id,
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
                logger.warning("IMAP login failed: ip=%s reason=%s credential_source=%s",
                               ip, type(exc).__name__, credential_source)
                raise ImapAuthenticationError(credential_source, type(exc).__name__) from None

            tls_socket.settimeout(timeout)
            self._mailbox = mailbox
            logger.info("IMAP TLS connected: ip=%s host=%s session=%s", ip, host, self._session_id)
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
                logger.warning("IMAP stage=logout error_type=%s session=%s",
                               type(exc).__name__, self._session_id)
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

    def folder_snapshot(self) -> list[str]:
        """LIST must succeed for preparation and MOVE; never infer absence on failure."""
        return [entry.name for entry in self.mailbox.folder.list()]

    def _resolve_folder_name(self, wanted: str, folders: list[str] | None = None) -> str | None:
        if folders is None:
            folders = self.list_folders()
        if not wanted:
            return None
        if wanted in folders:
            return wanted
        lower_map = {f.casefold(): f for f in folders}
        if wanted.casefold() in lower_map:
            return lower_map[wanted.casefold()]
        if wanted.casefold() == "inbox" and "inbox" in lower_map:
            return lower_map["inbox"]
        return None

    def ensure_folder(self, folder: str, create: bool = False) -> str:
        folder = destination_path(folder, "legacy", self.cfg.inbox_folder, self._folder_delimiter)
        existing = self.folder_snapshot()
        resolved = self._resolve_folder_name(folder, existing)
        if resolved:
            return resolved
        if not create:
            raise FileNotFoundError("Целевая IMAP-папка отсутствует")
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
                existing = self.folder_snapshot()
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
        folder = destination_path(folder, "legacy", self.cfg.inbox_folder, self._folder_delimiter)
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

        capabilities = self.mailbox.client.capabilities
        if "MOVE" in capabilities:
            method = "MOVE"
        elif "UIDPLUS" in capabilities:
            method = "UIDPLUS_FALLBACK"
        else:
            logger.warning("IMAP stage=capability uid=%s source=%s target=%s session=%s reason=safe_move_unavailable",
                           uid, source, actual, self._session_id)
            raise MoveNotStartedError("capability", "SafeMoveUnavailable")
        if before_command is not None:
            try:
                before_command()
            except Exception as exc:
                logger.error("IMAP stage=journal_pending uid=%s target=%s error_type=%s",
                             uid, actual, type(exc).__name__)
                raise MoveNotStartedError("journal_pending", type(exc).__name__) from exc

        logger.info("IMAP stage=move_command uid=%s target=%s source=%s source_uidvalidity=%s method=%s session=%s",
                    uid, actual, source, self._selected_uidvalidity, method, self._session_id)
        if isinstance(self.mailbox.client, _PreconnectedIMAP4SSL):
            self.mailbox.client.last_operation_trace = None
        try:
            if method == "MOVE":
                self.mailbox.move(uid, actual)
            else:
                self._uidplus_move(uid, actual)
        except Exception as exc:
            error_type = type(exc).__name__
            diagnostic_code = _safe_abort_code(exc)
            trace = getattr(self.mailbox.client, "last_operation_trace", None)
            if not isinstance(trace, _CommandTrace):
                trace = None
            phase = trace.phase if trace else "unavailable"
            tagged_status = trace.tagged_status if trace else None
            operation = trace.operation if trace else "unknown"
            state, socket_state = _safe_connection_state(self.mailbox.client)
            logger.warning(
                "IMAP stage=move_command uid=%s target=%s source=%s source_uidvalidity=%s method=%s "
                "operation=%s phase=%s tagged_status=%s connection_state=%s socket_state=%s "
                "session=%s error_type=%s diagnostic_code=%s",
                uid, actual, source, self._selected_uidvalidity, method, operation, phase,
                tagged_status or "none", state, socket_state, self._session_id, error_type, diagnostic_code,
            )
            if (diagnostic_code == "unexpected_response"
                    and isinstance(self.mailbox.client, _PreconnectedIMAP4SSL)
                    and self.mailbox.client.last_response_shape is not None):
                logger.warning("IMAP stage=move_response_shape uid=%s target=%s session=%s shape=%s",
                               uid, actual, self._session_id, self.mailbox.client.last_response_shape)
            # A tagged OK for this exact UID MOVE (or final UID EXPUNGE after
            # confirmed COPY and STORE) is server evidence, even if imaplib
            # raises on a later BYE/connection issue.
            confirmed_operation = "MOVE" if method == "MOVE" else "EXPUNGE"
            if (trace is not None and trace.operation == confirmed_operation
                    and trace.tagged_status == "OK"):
                logger.warning("IMAP stage=post_tagged_response uid=%s target=%s method=%s "
                               "session=%s result=server_confirmed diagnostic_code=%s",
                               uid, actual, method, self._session_id, diagnostic_code)
                return MoveResult("server_ok", error_type, "post_tagged_response", diagnostic_code,
                                  phase)
            try:
                verified = self._verify_move(msg, source, actual, self._selected_uidvalidity, baseline)
            except Exception as verify_exc:
                logger.warning("IMAP stage=verify_move uid=%s target=%s error_type=%s",
                               uid, actual, type(verify_exc).__name__)
                verified = False
            if verified:
                logger.warning("IMAP stage=verify_move uid=%s target=%s session=%s result=confirmed "
                               "evidence=independent_imap error_type=%s diagnostic_code=%s",
                               uid, actual, self._session_id, error_type, diagnostic_code)
                return MoveResult("verified_after_error", error_type, "verify_move", diagnostic_code,
                                  phase)
            logger.error("IMAP stage=verify_move uid=%s target=%s session=%s result=unknown "
                         "error_type=%s diagnostic_code=%s",
                         uid, actual, self._session_id, error_type, diagnostic_code)
            raise MoveUncertainError("move_command", error_type, phase, diagnostic_code) from exc

        logger.info("IMAP stage=move_confirmed uid=%s target=%s session=%s evidence=server_ok",
                    uid, actual, self._session_id)
        return MoveResult("server_ok")

    def _uidplus_move(self, uid: str, target: str) -> None:
        """Safe fallback: UID EXPUNGE affects only this UID, never other Deleted mail."""
        client = self.mailbox.client
        copy_result = client.uid("COPY", uid, encode_folder(target))
        check_command_status(copy_result, MailboxCopyError)
        store_result = client.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
        check_command_status(store_result, MailboxDeleteError)
        expunge_result = client.uid("EXPUNGE", uid)
        check_command_status(expunge_result, MailboxExpungeError)

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
        return self.ensure_folder(folder, create=True)

    def delete_folder(self, folder: str) -> None:
        folder = destination_path(folder, "legacy", self.cfg.inbox_folder, self._folder_delimiter)
        existing = self.folder_snapshot()
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
        old_name = destination_path(old_name, "legacy", self.cfg.inbox_folder, self._folder_delimiter)
        new_name = destination_path(new_name, "legacy", self.cfg.inbox_folder, self._folder_delimiter)
        existing = self.folder_snapshot()
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
