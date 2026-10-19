"""Offline regression tests: every IMAP and model interaction is faked."""

from __future__ import annotations

import ssl
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailsorter.config import ImapConfig, MailboxConfig, config_path
from mailsorter.imap_client import ImapClient
from mailsorter.sorter import MailSorter
from mailsorter.manager import SorterManager
from mailsorter.security import REDACTED, redact_secrets, restore_redacted, sanitize_log
from mailsorter.webapp import app


def mailbox_config() -> MailboxConfig:
    return MailboxConfig.model_validate({
        "name": "fake",
        "imap": {"host": "example.invalid", "username": "fake@example.invalid", "password": "unused"},
        "llm": {"max_retries": 1},
        "rules": {"source_folder": "INBOX", "destinations": [{"folder": "INBOX/Work", "condition": "Работа"}]},
    })


class FakeImap:
    instances = []

    def __init__(self, cfg):
        self.moves = []
        self.creates = []
        self.fetches = []
        self.message = SimpleNamespace(uid="101", subject="Тема", from_="sender@example.invalid")
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def fetch_folder_messages(self, folder, limit=100):
        self.fetches.append((folder, limit))
        return [self.message]

    def get_message_text(self, msg):
        return "Текст письма"

    def move_message(self, msg, target):
        self.moves.append((msg.uid, target))

    def ensure_folder(self, folder):
        self.creates.append(folder)


class FakeLlm:
    answer = {"category": "INBOX/Work", "confidence": 0.95}
    error = None

    def __init__(self, cfg):
        pass

    def classify(self, *args):
        if self.error:
            raise self.error
        return self.answer


class SorterTests(unittest.TestCase):
    def setUp(self):
        FakeImap.instances.clear()
        FakeLlm.answer = {"category": "INBOX/Work", "confidence": 0.95}
        FakeLlm.error = None
        self.imap_patch = patch("mailsorter.sorter.ImapClient", FakeImap)
        self.llm_patch = patch("mailsorter.sorter.LlmClient", FakeLlm)
        self.imap_patch.start()
        self.llm_patch.start()
        self.addCleanup(self.imap_patch.stop)
        self.addCleanup(self.llm_patch.stop)

    def test_dry_run_reads_and_plans_without_mutation(self):
        stats = MailSorter(mailbox_config()).run_once()
        fake = FakeImap.instances[-1]
        self.assertEqual(fake.fetches, [("INBOX", 100)])
        self.assertEqual(fake.moves, [])
        self.assertEqual(fake.creates, [])
        self.assertEqual((stats.mode, stats.planned, stats.moved), ("dry_run", 1, 0))
        self.assertEqual(stats.details[0]["folder"], "INBOX/Work")

    def test_active_moves_only_known_category_with_sufficient_confidence(self):
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [("101", "INBOX/Work")])
        self.assertEqual(stats.moved, 1)
        FakeLlm.answer = {"category": "INBOX/Unknown", "confidence": 0.99}
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.errors, 1)
        FakeLlm.answer = {"category": "", "confidence": 0.99}
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.errors, 1)

    def test_llm_failure_leaves_message_in_source(self):
        FakeLlm.error = TimeoutError("fake timeout")
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.details[0]["action"], "retry_next_run")
        self.assertEqual(stats.errors, 1)


class ImapTlsTests(unittest.TestCase):
    def test_certificate_error_never_reconnects_without_verification(self):
        cfg = ImapConfig(host="example.invalid", username="fake", password="unused")
        fake_socket = SimpleNamespace(close=lambda: None)
        with patch("mailsorter.imap_client.socket.create_connection", return_value=fake_socket), \
             patch("mailsorter.imap_client.MailBox", side_effect=ssl.SSLCertVerificationError("fake certificate")) as mailbox:
            with self.assertRaisesRegex(RuntimeError, "TLS"):
                ImapClient(cfg).connect()
        self.assertEqual(mailbox.call_count, 1)
        self.assertTrue(cfg.ssl_verify)
        self.assertEqual(mailbox.call_args.kwargs["ssl_context"].verify_mode, ssl.CERT_REQUIRED)

    def test_fetch_uses_peek_without_marking_seen(self):
        client = ImapClient(ImapConfig(host="example.invalid", username="fake", password="unused"))
        fake_mailbox = SimpleNamespace(folder=SimpleNamespace(set=lambda folder: None))
        calls = []

        def fetch(**kwargs):
            calls.append(kwargs)
            return iter([])

        fake_mailbox.fetch = fetch
        client._mailbox = fake_mailbox
        with patch.object(client, "list_folders", return_value=["INBOX"]):
            client.fetch_folder_messages("INBOX", limit=5)
        self.assertEqual(calls, [{"limit": 5, "reverse": True, "mark_seen": False}])

    def test_folder_protection_preserves_personal_folder_management(self):
        items = [SimpleNamespace(name=name, flags=()) for name in ("INBOX", "INBOX/Work", "INBOX/Sent", "INBOX/Old")]
        deleted = []
        fake_folder = SimpleNamespace(list=lambda: items, set=lambda name: None, delete=lambda name: deleted.append(name))
        client = ImapClient(ImapConfig(host="example.invalid", username="fake", password="unused"),
                            protected_folders=["INBOX/Work"])
        client._mailbox = SimpleNamespace(folder=fake_folder, fetch=lambda **kwargs: iter([]))
        with self.assertRaises(RuntimeError):
            client.delete_folder("INBOX/Work")
        with self.assertRaises(RuntimeError):
            client.delete_folder("INBOX/Sent")
        client.delete_folder("INBOX/Old")
        self.assertEqual(deleted, ["INBOX/Old"])


class SecurityTests(unittest.TestCase):
    def test_active_requires_exact_confirmation(self):
        manager = SorterManager()
        manager.sorters["fake.yaml"] = MailSorter(mailbox_config())
        self.assertEqual(manager.mode_for("fake.yaml"), "dry_run")
        with self.assertRaises(ValueError):
            manager.set_mode("fake.yaml", "active", "ACTIVE other.yaml")
        self.assertEqual(manager.mode_for("fake.yaml"), "dry_run")
        self.assertEqual(manager.set_mode("fake.yaml", "active", "ACTIVE fake.yaml"), "active")
        self.assertEqual(manager.mode_for("fake.yaml"), "active")
        manager.set_mode("fake.yaml", "dry_run")
        self.assertEqual(manager.mode_for("fake.yaml"), "dry_run")
        with patch.object(manager.sorters["fake.yaml"], "run_once") as run_once:
            manager._run_job("fake.yaml")
            run_once.assert_called_once_with(mode="dry_run")
            run_once.reset_mock()
            with patch("mailsorter.manager.threading.Thread") as thread:
                manager.run_now("fake.yaml")
                thread.call_args.kwargs["target"]()
            run_once.assert_called_once_with(mode="dry_run")

    def test_unauthorized_endpoints_reject_before_network_calls(self):
        with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
             patch("mailsorter.webapp._load_web_credentials", return_value=True), \
             patch("mailsorter.webapp.ImapClient") as imap, \
             patch("mailsorter.webapp.autodiscover_microsoft") as autodiscover:
            client = TestClient(app)
            self.assertEqual(client.post("/api/test-imap", json={"file": "mailbox.yaml"}).status_code, 401)
            self.assertEqual(client.post("/api/autodiscover", json={"email": "fake@example.invalid"}).status_code, 401)
            self.assertEqual(client.post("/api/mode/mailbox.yaml", json={"mode": "active", "confirmation": "ACTIVE mailbox.yaml"}).status_code, 401)
            imap.assert_not_called()
            autodiscover.assert_not_called()

    def test_initial_setup_rejects_nonlocal_client(self):
        with patch("mailsorter.webapp.is_setup_mode", return_value=True):
            client = TestClient(app)
            self.assertEqual(client.get("/setup").status_code, 403)
            self.assertEqual(client.post("/api/setup", json={}).status_code, 403)

    def test_unsafe_config_paths_are_rejected(self):
        for name in ("../README.md", "..\\README.md", "/absolute.yaml", "other.txt", ".hidden.yaml", "x'evil.yaml"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config_path(name)

    def test_unsafe_config_path_is_rejected_by_web_page(self):
        credentials = base64.b64encode(b"admin:fake-password").decode("ascii")
        with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
             patch("mailsorter.webapp._load_web_credentials", return_value=True), \
             patch("mailsorter.webapp._web_user", "admin"), \
             patch("mailsorter.webapp._web_pass", "fake-password"):
            client = TestClient(app)
            response = client.get("/config?file=../README.md", headers={"Authorization": f"Basic {credentials}"})
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/config", headers={"Authorization": f"Basic {credentials}"},
                                   json={"filename": "../unsafe.yaml", "content": ""})
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/mode/fake.yaml", headers={"Authorization": f"Basic {credentials}",
                                                               "Origin": "http://attacker.invalid"},
                                   json={"mode": "active", "confirmation": "ACTIVE fake.yaml"})
            self.assertEqual(response.status_code, 403)

    def test_saved_secrets_are_redacted_and_restored(self):
        original = {"imap": {"password": "ENV:FAKE_TEST_SECRET"}, "web": {"admin_password": "fake-hash"}}
        displayed = redact_secrets(original)
        self.assertEqual(displayed["imap"]["password"], REDACTED)
        self.assertEqual(restore_redacted(displayed, original), original)
        self.assertNotIn("fake-secret", sanitize_log("password=fake-secret Authorization: Bearer fake-secret"))

    def test_legacy_rules_load(self):
        cfg = MailboxConfig.model_validate({
            "name": "legacy", "imap": {"host": "example.invalid", "username": "fake", "password": "unused"},
            "rules": {"items": [{"source_folder": "INBOX", "categories": [{"name": "Работа", "folder": "INBOX/Work"}]}]},
        })
        self.assertEqual(cfg.rules.destinations[0].folder, "INBOX/Work")
        self.assertEqual(cfg.llm.model, "qwen2.5:14b")
        self.assertTrue(cfg.imap.ssl_verify)


if __name__ == "__main__":
    unittest.main()
