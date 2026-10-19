"""Offline IMAP protocol-level MOVE and safe fallback regressions."""

from __future__ import annotations

import imaplib
import ssl
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

from imap_tools.utils import encode_folder

from mailsorter.imap_client import (
    ImapClient, MoveNotStartedError, MoveResult, MoveUncertainError,
    _PreconnectedIMAP4SSL, _PreconnectedMailBox,
)
from tests.test_move_lifecycle import message, prepared_client
from tests.test_folder_preparation import config as folder_config
from tests.test_safety import FakeImap, FakeLlm, FakeMoveJournal
from mailsorter.sorter import MailSorter


class ScriptedTlsSocket:
    """Only synthetic IMAP lines; no DNS, TCP, TLS, credentials or real mailbox."""

    def __init__(self, move_response):
        self.lines = deque([b"* OK synthetic IMAP ready\r\n"])
        self.sent = []
        self.move_response = move_response
        self.closed = False

    def makefile(self, *_args):
        return self

    def readline(self, *_args):
        return self.lines.popleft() if self.lines else b""

    def sendall(self, data):
        self.sent.append(data)
        tag = data.split(b" ", 1)[0]
        if b" CAPABILITY" in data:
            self.lines.extend([b"* CAPABILITY IMAP4rev1 MOVE UIDPLUS\r\n",
                               tag + b" OK CAPABILITY completed\r\n"])
        elif b" UID MOVE " in data:
            self.lines.extend(self.move_response(tag))

    def fileno(self):
        return -1 if self.closed else 1

    def close(self):
        self.closed = True

    def shutdown(self, *_args):
        self.closed = True


def scripted_client(response):
    stream = ScriptedTlsSocket(response)
    client = _PreconnectedIMAP4SSL("mail.example.invalid", 993, stream,
                                   ssl.create_default_context(), 2)
    client.state = "SELECTED"  # synthetic authenticated, selected mailbox
    return client, stream


class TaggedMoveProtocolTests(unittest.TestCase):
    def test_imap_tools_uses_single_uid_move_without_copy_or_expunge(self):
        stream = ScriptedTlsSocket(lambda tag: [tag + b" OK MOVE done\r\n"])
        mailbox = _PreconnectedMailBox("mail.example.invalid", 993, stream,
                                       ssl.create_default_context(), 2)
        mailbox.client.state = "SELECTED"
        mailbox.move("30", "INBOX/Доступы")
        commands = [line for line in stream.sent if b" UID " in line]
        self.assertEqual(len(commands), 1)
        self.assertIn(b" UID MOVE 30 ", commands[0])
        self.assertNotIn("Доступы".encode(), commands[0])

    def test_normal_uid_move_tagged_ok_is_captured(self):
        client, stream = scripted_client(
            lambda tag: [b"* 1 EXPUNGE\r\n", tag + b" OK [COPYUID 11 30 205] MOVE done\r\n"]
        )
        result = client.uid("MOVE", "30", encode_folder("INBOX/Доступы"))
        self.assertEqual(result[0], "OK")
        self.assertEqual(client.last_operation_trace.operation, "MOVE")
        self.assertEqual(client.last_operation_trace.tagged_status, "OK")
        self.assertEqual(client.last_operation_trace.phase, "completed")
        self.assertEqual(len([line for line in stream.sent if b" UID MOVE " in line]), 1)
        self.assertNotIn("Доступы".encode(), b"".join(stream.sent))

    def test_server_applies_move_but_response_is_lost(self):
        client, stream = scripted_client(lambda _tag: [b"* 1 EXPUNGE\r\n"])
        with self.assertRaises(imaplib.IMAP4.abort):
            client.uid("MOVE", "30", encode_folder("INBOX/Доступы"))
        self.assertIsNone(client.last_operation_trace.tagged_status)
        self.assertEqual(client.last_operation_trace.phase, "await_tagged_response")
        self.assertEqual(len([line for line in stream.sent if b" UID MOVE " in line]), 1)

    def test_malformed_response_is_distinguished_from_tagged_ok(self):
        client, _ = scripted_client(lambda _tag: [b"* 1 EXPUNGE\r\n", b"MALFORMED\r\n"])
        with self.assertRaises(imaplib.IMAP4.abort):
            client.uid("MOVE", "30", encode_folder("INBOX/Доступы"))
        self.assertIsNone(client.last_operation_trace.tagged_status)
        self.assertEqual(client.last_operation_trace.phase, "await_tagged_response")

    def test_tagged_no_is_not_success(self):
        client, _ = scripted_client(lambda tag: [tag + b" NO MOVE rejected\r\n"])
        result = client.uid("MOVE", "30", encode_folder("INBOX/Доступы"))
        self.assertEqual(result[0], "NO")
        self.assertEqual(client.last_operation_trace.tagged_status, "NO")

    def test_socket_break_before_send_is_submission_unknown(self):
        client, stream = scripted_client(lambda _tag: [])

        def broken_send(data):
            if b" UID MOVE " in data:
                raise OSError("synthetic disconnect")
            stream.sent.append(data)

        stream.sendall = broken_send
        with self.assertRaises(imaplib.IMAP4.abort):
            client.uid("MOVE", "30", encode_folder("INBOX/Доступы"))
        self.assertEqual(client.last_operation_trace.phase, "submission_unknown")
        self.assertIsNone(client.last_operation_trace.tagged_status)


class MoveEvidenceTests(unittest.TestCase):
    def test_real_imap_tools_path_tagged_ok_confirms_move(self):
        stream = ScriptedTlsSocket(lambda tag: [tag + b" OK MOVE done\r\n"])
        mailbox = _PreconnectedMailBox("mail.example.invalid", 993, stream,
                                       ssl.create_default_context(), 2)
        mailbox.client.state = "SELECTED"
        mailbox.folder.status = MagicMock(return_value={"UIDVALIDITY": 11, "UIDNEXT": 200})
        client = ImapClient(folder_config().imap)
        client._mailbox = mailbox
        client._selected_folder = "INBOX"
        client._selected_uidvalidity = 7
        with patch.object(client, "ensure_folder", return_value="INBOX/Доступы"):
            result = client.move_message(message(), "INBOX/Доступы", before_command=lambda: None)
        self.assertEqual(result.evidence, "server_ok")
        self.assertIsNone(result.error_type)
        self.assertEqual(len([line for line in stream.sent if b" UID MOVE " in line]), 1)

    def test_real_imap_tools_path_tagged_no_never_false_confirms(self):
        stream = ScriptedTlsSocket(lambda tag: [tag + b" NO MOVE rejected\r\n"])
        mailbox = _PreconnectedMailBox("mail.example.invalid", 993, stream,
                                       ssl.create_default_context(), 2)
        mailbox.client.state = "SELECTED"
        mailbox.folder.status = MagicMock(return_value={"UIDVALIDITY": 11, "UIDNEXT": 200})
        client = ImapClient(folder_config().imap)
        client._mailbox = mailbox
        client._selected_folder = "INBOX"
        client._selected_uidvalidity = 7
        with patch.object(client, "ensure_folder", return_value="INBOX/Доступы"), \
             patch.object(client, "_verify_move", return_value=False) as verify:
            with self.assertRaises(MoveUncertainError):
                client.move_message(message(), "INBOX/Доступы", before_command=lambda: None)
        self.assertEqual(mailbox.client.last_operation_trace.tagged_status, "NO")
        verify.assert_called_once()
        self.assertEqual(len([line for line in stream.sent if b" UID MOVE " in line]), 1)

    def test_tagged_ok_then_abort_is_server_confirmed_without_recheck(self):
        client, mailbox = prepared_client()
        # The production trace is emitted by the preconnected imaplib subclass.
        from mailsorter.imap_client import _CommandTrace
        mailbox.client.last_operation_trace = _CommandTrace(
            "MOVE", "after_tagged_response", b"TAG1", "OK",
        )
        mailbox.move.side_effect = imaplib.IMAP4.abort("socket error: EOF")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move") as verify:
            result = client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual(result.evidence, "server_ok")
        self.assertEqual(result.stage, "post_tagged_response")
        self.assertEqual(result.phase, "after_tagged_response")
        self.assertEqual(result.diagnostic_code, "socket_eof")
        verify.assert_not_called()
        mailbox.move.assert_called_once()

    def test_no_tagged_ok_falls_back_to_independent_verification(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("socket error: EOF")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=True) as verify:
            result = client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual(result.evidence, "verified_after_error")
        self.assertEqual(result.phase, "unavailable")
        self.assertEqual(result.diagnostic_code, "socket_eof")
        verify.assert_called_once()

    def test_failed_verification_keeps_unknown_and_never_retries(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("socket error: EOF")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=False):
            with self.assertRaises(MoveUncertainError) as caught:
                client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual(caught.exception.diagnostic_code, "socket_eof")
        mailbox.move.assert_called_once()

    def test_verification_exception_is_not_false_success(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("unexpected response: synthetic")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", side_effect=OSError("offline")):
            with self.assertRaises(MoveUncertainError):
                client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        mailbox.move.assert_called_once()

    def test_abort_diagnostic_never_logs_server_text(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("unexpected response: SECRET-SYNTHETIC")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=False), \
             self.assertLogs("mailsorter.imap", level="WARNING") as logs:
            with self.assertRaises(MoveUncertainError):
                client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertNotIn("SECRET-SYNTHETIC", " ".join(logs.output))
        self.assertIn("diagnostic_code=unexpected_response", " ".join(logs.output))

    def test_connection_failure_before_move_does_not_call_move(self):
        client, mailbox = prepared_client()
        mailbox.folder.status.side_effect = imaplib.IMAP4.abort("socket error: EOF")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"):
            with self.assertRaises(MoveNotStartedError) as caught:
                client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual(caught.exception.stage, "target_baseline")
        mailbox.move.assert_not_called()


class SafeFallbackTests(unittest.TestCase):
    def test_uidplus_fallback_never_issues_global_expunge(self):
        client, mailbox = prepared_client()
        mailbox.client.capabilities = ("IMAP4rev1", "UIDPLUS")
        mailbox.client.uid.return_value = ("OK", [None])
        with patch.object(client, "ensure_folder", return_value="INBOX/Доступы"):
            result = client.move_message(message(), "INBOX/Доступы", before_command=lambda: None)
        self.assertEqual(result, MoveResult("server_ok"))
        self.assertEqual([call.args[0] for call in mailbox.client.uid.call_args_list],
                         ["COPY", "STORE", "EXPUNGE"])
        self.assertEqual([call.args[1] for call in mailbox.client.uid.call_args_list],
                         ["30", "30", "30"])
        self.assertEqual(mailbox.client.uid.call_args_list[0].args[2], encode_folder("INBOX/Доступы"))
        mailbox.expunge.assert_not_called()
        mailbox.move.assert_not_called()

    def test_without_move_or_uidplus_refuses_before_journal_and_mail_changes(self):
        client, mailbox = prepared_client()
        mailbox.client.capabilities = ("IMAP4rev1",)
        before = MagicMock()
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"):
            with self.assertRaises(MoveNotStartedError) as caught:
                client.move_message(message(), "INBOX/Work", before_command=before)
        self.assertEqual(caught.exception.stage, "capability")
        before.assert_not_called()
        mailbox.client.uid.assert_not_called()
        mailbox.move.assert_not_called()
        mailbox.expunge.assert_not_called()

    def test_uidplus_abort_after_copy_remains_unknown_without_recopy(self):
        client, mailbox = prepared_client()
        mailbox.client.capabilities = ("IMAP4rev1", "UIDPLUS")
        mailbox.client.uid.side_effect = [("OK", [None]), imaplib.IMAP4.abort("socket error: EOF")]
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=False):
            with self.assertRaises(MoveUncertainError):
                client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual(mailbox.client.uid.call_count, 2)
        self.assertEqual(mailbox.client.uid.call_args_list[0].args[0], "COPY")
        mailbox.expunge.assert_not_called()


class MoveStatisticsTests(unittest.TestCase):
    def test_confirmed_warning_and_unsorted_are_reported_without_false_error(self):
        class ConfirmedImap(FakeImap):
            def move_message(self, msg, target, before_command=None):
                before_command()
                self.moves.append((msg.uid, target))
                return MoveResult("server_ok", "abort", "post_tagged_response", "socket_eof",
                                  "after_tagged_response")

        with patch("mailsorter.sorter.ImapClient", ConfirmedImap), \
             patch("mailsorter.sorter.LlmClient", FakeLlm), \
             patch("mailsorter.sorter.MoveJournal", FakeMoveJournal), \
             patch.object(FakeLlm, "answer"), patch.object(FakeLlm, "error", None):
            FakeLlm.answer = {"category": "Доступы", "confidence": 0.95}
            moved = MailSorter(folder_config()).run_once("active")
            FakeLlm.answer = {"category": "Доступы", "confidence": 0.01}
            unsorted = MailSorter(folder_config()).run_once("active")
        self.assertEqual((moved.moved, moved.unsorted, moved.errors), (1, 0, 0))
        self.assertEqual((unsorted.moved, unsorted.unsorted, unsorted.errors), (0, 1, 0))
        self.assertEqual(moved.details[0]["folder"], "INBOX/Доступы")
        self.assertEqual(unsorted.details[0]["folder"], "INBOX/Unsorted")
        for stats in (moved, unsorted):
            self.assertEqual(stats.move_unknown, 0)
            self.assertEqual(stats.details[0]["confirmation_method"], "server_tagged_ok")
            self.assertEqual(stats.details[0]["move_warning"], "abort")
            self.assertEqual(stats.details[0]["diagnostic_code"], "socket_eof")
            self.assertEqual(stats.details[0]["phase"], "after_tagged_response")


if __name__ == "__main__":
    unittest.main()
