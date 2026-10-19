"""Offline IMAP MOVE lifecycle and uncertainty-quarantine regression tests."""

from __future__ import annotations

import imaplib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mailsorter.imap_client import (
    ImapClient, MoveNotStartedError, MoveResult, MoveUncertainError,
)
from mailsorter.move_journal import MoveJournal
from mailsorter.sorter import MailSorter
from tests.test_safety import FakeImap, FakeLlm, FakeMoveJournal, mailbox_config


def message(uid="30"):
    return SimpleNamespace(
        uid=uid,
        headers={"message-id": ("<synthetic-30@example.invalid>",),
                 "date": ("Mon, 19 Oct 2026 10:00:00 +0000",),
                 "from": ("sender@example.invalid",)},
        size_rfc822=128,
    )


def prepared_client():
    cfg = mailbox_config().imap
    client = ImapClient(cfg)
    mailbox = MagicMock()
    mailbox.client.capabilities = ("IMAP4rev1", "MOVE")
    mailbox.folder.status.return_value = {"UIDVALIDITY": 11, "UIDNEXT": 200}
    client._mailbox = mailbox
    client._selected_folder = "INBOX"
    client._selected_uidvalidity = 7
    return client, mailbox


class MoveCommandTests(unittest.TestCase):
    def test_successful_move_has_server_confirmation(self):
        client, mailbox = prepared_client()
        before = MagicMock()
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"):
            result = client.move_message(message(), "INBOX/Work", before_command=before)
        self.assertEqual(result, MoveResult("server_ok"))
        before.assert_called_once()
        mailbox.move.assert_called_once_with("30", "INBOX/Work")

    def test_abort_before_move_never_sends_command(self):
        client, mailbox = prepared_client()
        before = MagicMock()
        with patch.object(client, "ensure_folder", side_effect=imaplib.IMAP4.abort("offline")):
            with self.assertRaises(MoveNotStartedError) as caught:
                client.move_message(message(), "INBOX/Work", before_command=before)
        self.assertEqual(caught.exception.stage, "prepare_destination")
        before.assert_not_called()
        mailbox.move.assert_not_called()

    def test_journal_write_failure_prevents_move(self):
        client, mailbox = prepared_client()
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"):
            with self.assertRaises(MoveNotStartedError) as caught:
                client.move_message(message(), "INBOX/Work",
                                    before_command=MagicMock(side_effect=OSError("offline disk")))
        self.assertEqual(caught.exception.stage, "journal_pending")
        mailbox.move.assert_not_called()

    def test_abort_after_server_applied_move_can_be_verified_without_repeating(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("offline")
        before = MagicMock()
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=True) as verify:
            result = client.move_message(message(), "INBOX/Work", before_command=before)
        self.assertEqual(result, MoveResult("verified_after_error", "abort"))
        mailbox.move.assert_called_once()
        before.assert_called_once()
        verify.assert_called_once()

    def test_unknown_result_raises_and_never_retries_move(self):
        client, mailbox = prepared_client()
        mailbox.move.side_effect = imaplib.IMAP4.abort("offline")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"), \
             patch.object(client, "_verify_move", return_value=False):
            with self.assertLogs("mailsorter.imap", level="WARNING") as logs:
                with self.assertRaises(MoveUncertainError) as caught:
                    client.move_message(message(), "INBOX/Work", before_command=lambda: None)
        self.assertEqual((caught.exception.stage, caught.exception.error_type), ("move_command", "abort"))
        self.assertTrue(any("stage=move_command uid=30 target=INBOX/Work" in line
                            and "error_type=abort" in line for line in logs.output))
        self.assertNotIn("unused", " ".join(logs.output))
        mailbox.move.assert_called_once()

    def test_logout_error_does_not_change_confirmed_move(self):
        client, mailbox = prepared_client()
        mailbox.logout.side_effect = imaplib.IMAP4.abort("offline")
        with patch.object(client, "ensure_folder", return_value="INBOX/Work"):
            result = client.move_message(message(), "INBOX/Work")
        with self.assertLogs("mailsorter.imap", level="WARNING") as logs:
            client.disconnect()
        self.assertEqual(result.evidence, "server_ok")
        self.assertIsNone(client._mailbox)
        self.assertIn("stage=logout error_type=abort", logs.output[0])
        mailbox.client.shutdown.assert_called_once()


class VerificationTests(unittest.TestCase):
    def _verify(self, *, source_validity=7, target_validity=11,
                source_uids=None, target_uids=None, target_message=None):
        client, _ = prepared_client()
        checker = MagicMock()
        checker.__enter__.return_value = checker
        selected = {"folder": "INBOX"}

        def set_folder(name, readonly=False):
            self.assertTrue(readonly)
            selected["folder"] = name

        def status(name, options):
            return {"UIDVALIDITY": source_validity if name == "INBOX" else target_validity}

        def uids(*_args, **_kwargs):
            return (source_uids if source_uids is not None else []) if selected["folder"] == "INBOX" \
                else (target_uids if target_uids is not None else ["205"])

        checker.mailbox.folder.set.side_effect = set_folder
        checker.mailbox.folder.status.side_effect = status
        checker.mailbox.uids.side_effect = uids
        checker.mailbox.fetch.return_value = [target_message or message(uid="205")]
        with patch("mailsorter.imap_client.ImapClient", return_value=checker):
            return client._verify_move(message(), "INBOX", "INBOX/Work", 7,
                                       {"UIDVALIDITY": 11, "UIDNEXT": 200}), checker

    def test_verifies_new_destination_uid_with_stable_uidvalidity(self):
        verified, checker = self._verify()
        self.assertTrue(verified)
        checker.mailbox.fetch.assert_called_once_with(
            uid_list=["205"], headers_only=True, mark_seen=False,
        )

    def test_uidvalidity_change_or_old_destination_uid_is_not_proof(self):
        for options in ({"source_validity": 8}, {"target_validity": 12},
                        {"target_uids": ["30"]}, {"source_uids": ["30"]},
                        {"target_uids": ["205", "206"]}):
            with self.subTest(options=options):
                verified, _ = self._verify(**options)
                self.assertFalse(verified)


class QuarantineTests(unittest.TestCase):
    def test_pending_move_survives_reload_and_resolve_is_durable(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("mailsorter.move_journal._JOURNAL_DIR", Path(directory)):
            cfg = mailbox_config()
            journal = MoveJournal(cfg)
            key = journal.key("INBOX", 7, "30")
            self.assertEqual(key, journal.key("INBOX", None, "30"))
            journal.begin(key, source="INBOX", uidvalidity=7, uid="30", target="INBOX/Work")
            self.assertIn(key, MoveJournal(cfg).pending)
            recorded = journal.path.read_text(encoding="utf-8")
            self.assertIn('"uid":"30"', recorded)
            self.assertIn('"target":"INBOX/Work"', recorded)
            self.assertNotIn("unused", recorded)
            journal.resolve(key)
            self.assertNotIn(key, MoveJournal(cfg).pending)

    def test_uncertain_move_is_skipped_on_next_run(self):
        journal = FakeMoveJournal(mailbox_config())

        class UnknownImap(FakeImap):
            attempts = 0

            def move_message(self, msg, target, before_command=None):
                type(self).attempts += 1
                before_command()
                raise MoveUncertainError("move_command", "abort")

        UnknownImap.attempts = 0
        FakeLlm.answer = {"category": "INBOX/Work", "confidence": 0.95}
        FakeLlm.error = None
        with patch("mailsorter.sorter.ImapClient", UnknownImap), \
             patch("mailsorter.sorter.LlmClient", FakeLlm), \
             patch("mailsorter.sorter.MoveJournal", return_value=journal):
            sorter = MailSorter(mailbox_config())
            first = sorter.run_once(mode="active")
            second = sorter.run_once(mode="active")
        self.assertEqual(UnknownImap.attempts, 1)
        self.assertEqual((first.moved, first.move_unknown, first.errors), (0, 1, 1))
        self.assertEqual((first.pending_review, second.pending_review), (1, 1))
        self.assertEqual(first.details[0]["action"], "move_unknown")
        self.assertEqual(first.details[0]["reason"], "abort")
        self.assertEqual(second.details[0]["action"], "move_pending_review")
        self.assertEqual(second.move_unknown, 1)
        self.assertEqual(UnknownImap.instances[-1].moves, [])

    def test_pending_move_is_skipped_after_sorter_restart(self):
        class UnknownImap(FakeImap):
            attempts = 0

            def move_message(self, msg, target, before_command=None):
                type(self).attempts += 1
                before_command()
                raise MoveUncertainError("move_command", "abort")

        UnknownImap.attempts = 0
        FakeLlm.answer = {"category": "INBOX/Work", "confidence": 0.95}
        FakeLlm.error = None
        with tempfile.TemporaryDirectory() as directory, \
             patch("mailsorter.move_journal._JOURNAL_DIR", Path(directory)), \
             patch("mailsorter.sorter.ImapClient", UnknownImap), \
             patch("mailsorter.sorter.LlmClient", FakeLlm):
            first = MailSorter(mailbox_config()).run_once(mode="active")
            second = MailSorter(mailbox_config()).run_once(mode="active")
            with patch.object(UnknownImap, "fetch_folder_messages", return_value=[]):
                third = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(first.details[0]["action"], "move_unknown")
        self.assertEqual(second.details[0]["action"], "move_pending_review")
        self.assertEqual(UnknownImap.attempts, 1)
        self.assertEqual(third.pending_review, 1)

    def test_verified_after_abort_counts_as_confirmed_move(self):
        class VerifiedImap(FakeImap):
            def move_message(self, msg, target, before_command=None):
                before_command()
                self.moves.append((msg.uid, target))
                return MoveResult("verified_after_error", "abort")

        FakeLlm.answer = {"category": "INBOX/Work", "confidence": 0.95}
        FakeLlm.error = None
        with patch("mailsorter.sorter.ImapClient", VerifiedImap), \
             patch("mailsorter.sorter.LlmClient", FakeLlm), \
             patch("mailsorter.sorter.MoveJournal", FakeMoveJournal):
            stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual((stats.moved, stats.move_unknown, stats.errors), (1, 0, 0))
        self.assertEqual(stats.pending_review, 0)
        self.assertEqual(stats.details[0]["move_evidence"], "verified_after_error")
        self.assertEqual(stats.details[0]["stage"], "verify_move")
        self.assertEqual(stats.details[0]["move_warning"], "abort")


if __name__ == "__main__":
    unittest.main()
