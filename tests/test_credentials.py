"""Offline regressions for editor redaction and IMAP credential resolution."""

from __future__ import annotations

import base64
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from fastapi.testclient import TestClient
from imap_tools import MailboxLoginError

from mailsorter.config import ImapConfig, MailboxConfig, MissingImapPassword
from mailsorter.imap_client import ImapAuthenticationError, ImapClient
from mailsorter.sorter import MailSorter
from mailsorter.webapp import app, _redacted_yaml
from tests.test_imap_failover import fake_mailbox


def synthetic_config():
    return {
        "name": "synthetic", "folder_layout": "inbox",
        "imap": {"host": "example.invalid", "username": "offline-user",
                 "password": "ENV:MAIL_PASSWORD"},
        "llm": {"provider": "cloudru", "model": "openai/gpt-oss-120b",
                "api_key": "ENV:CLOUDRU_FOUNDATION_MODELS_API_KEY"},
        "rules": {"destinations": [{"folder": "Доступы", "condition": "offline"}]},
        "web": {"admin_username": "admin", "admin_password": "offline-admin"},
    }


class CredentialResolutionTests(unittest.TestCase):
    def test_env_reference_reads_only_process_environment(self):
        cfg = ImapConfig(host="example.invalid", username="offline",
                         password="ENV:MAIL_PASSWORD")
        with patch.dict(os.environ, {"MAIL_PASSWORD": "synthetic-secret"}):
            self.assertEqual(cfg.resolve_password(), "synthetic-secret")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingImapPassword) as caught:
                cfg.resolve_password()
        self.assertEqual(caught.exception.env_name, "MAIL_PASSWORD")
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_inline_password_does_not_use_mail_password(self):
        cfg = ImapConfig(host="example.invalid", username="offline",
                         password="synthetic-inline")
        with patch.dict(os.environ, {"MAIL_PASSWORD": "synthetic-env"}):
            self.assertEqual(cfg.resolve_password(), "synthetic-inline")

    def test_login_uses_resolved_env_secret_without_logging_it(self):
        cfg = ImapConfig(host="example.invalid", username="offline-user",
                         password="ENV:MAIL_PASSWORD")
        mailbox = fake_mailbox()
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 993))]
        with patch.dict(os.environ, {"MAIL_PASSWORD": "synthetic-secret"}), \
             patch("mailsorter.imap_client.socket.getaddrinfo", return_value=address), \
             patch("mailsorter.imap_client._open_tls_socket", return_value=MagicMock()), \
             patch("mailsorter.imap_client._PreconnectedMailBox", return_value=mailbox):
            client = ImapClient(cfg)
            client.connect()
            client.disconnect()
        mailbox.login.assert_called_once_with("offline-user", "synthetic-secret")

    def test_login_rejection_reports_source_without_secret_or_dns_retry(self):
        cfg = ImapConfig(host="example.invalid", username="offline-user",
                         password="ENV:MAIL_PASSWORD")
        mailbox = fake_mailbox(MailboxLoginError(("NO", [b"offline auth"]), "OK"))
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 993)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.21", 993))]
        with patch.dict(os.environ, {"MAIL_PASSWORD": "synthetic-secret"}), \
             patch("mailsorter.imap_client.socket.getaddrinfo", return_value=address), \
             patch("mailsorter.imap_client._open_tls_socket", return_value=MagicMock()) as open_tls, \
             patch("mailsorter.imap_client._PreconnectedMailBox", return_value=mailbox), \
             self.assertLogs("mailsorter.imap", level="WARNING") as logs:
            with self.assertRaises(ImapAuthenticationError) as caught:
                ImapClient(cfg).connect()
        self.assertEqual(caught.exception.credential_source, "environment")
        self.assertEqual(open_tls.call_count, 1)
        self.assertNotIn("synthetic-secret", " ".join(logs.output))

    def test_connection_diagnostic_distinguishes_missing_env_and_inline_rejection(self):
        cfg = MailboxConfig.model_validate(synthetic_config())
        with patch("mailsorter.sorter.ImapClient", side_effect=MissingImapPassword("MAIL_PASSWORD")):
            missing = MailSorter(cfg).test_connection()
        self.assertFalse(missing["ok"])
        self.assertIn("MAIL_PASSWORD", missing["message"])
        with patch("mailsorter.sorter.ImapClient",
                   side_effect=ImapAuthenticationError("inline_yaml", "MailboxLoginError")):
            rejected = MailSorter(cfg).test_connection()
        self.assertFalse(rejected["ok"])
        self.assertIn("YAML", rejected["message"])
        self.assertIn("не используется", rejected["message"])


class RedactedEditorTests(unittest.TestCase):
    def test_validate_and_save_restore_env_refs_without_network(self):
        auth = base64.b64encode(b"admin:offline-admin").decode("ascii")
        headers = {"Authorization": f"Basic {auth}"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.yaml"
            path.write_text(yaml.safe_dump(synthetic_config(), allow_unicode=True), encoding="utf-8")
            before = yaml.safe_load(path.read_text(encoding="utf-8"))
            with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
                 patch("mailsorter.webapp._load_web_credentials", return_value=True), \
                 patch("mailsorter.webapp._web_user", "admin"), \
                 patch("mailsorter.webapp._web_pass", "offline-admin"), \
                 patch("mailsorter.webapp.config_path", return_value=path), \
                 patch("mailsorter.webapp.manager.reload_one") as reload_one, \
                 patch("mailsorter.webapp.ImapClient") as imap:
                client = TestClient(app)
                config_response = client.get("/api/config/synthetic.yaml", headers=headers).json()
                self.assertEqual(config_response["imap_credential_source"], "environment")
                visible = config_response["content"]
                self.assertIn("<REDACTED>", visible)
                self.assertNotIn("ENV:MAIL_PASSWORD", visible)
                no_file = client.post("/api/validate", headers=headers, json={"content": visible}).json()
                self.assertFalse(no_file["ok"])
                self.assertNotIn("ValidationError", no_file["message"])
                checked = client.post("/api/validate", headers=headers,
                                      json={"content": visible, "file": "synthetic.yaml"}).json()
                self.assertEqual(checked, {"ok": True, "message": "OK"})
                self.assertEqual(yaml.safe_load(path.read_text(encoding="utf-8")), before)
                saved = client.post("/api/config/synthetic.yaml", headers=headers,
                                    json={"content": visible}).json()
                self.assertTrue(saved["ok"])
                imap.assert_not_called()
                reload_one.assert_called_once_with("synthetic.yaml")
            after = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(after["imap"]["password"], "ENV:MAIL_PASSWORD")
            self.assertEqual(after["llm"]["api_key"],
                             "ENV:CLOUDRU_FOUNDATION_MODELS_API_KEY")
            self.assertEqual(after["folder_layout"], "inbox")

    def test_editor_identifies_inline_source_without_exposing_password(self):
        raw = synthetic_config()
        raw["imap"]["password"] = "synthetic-inline"
        auth = base64.b64encode(b"admin:offline-admin").decode("ascii")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
                 patch("mailsorter.webapp._load_web_credentials", return_value=True), \
                 patch("mailsorter.webapp._web_user", "admin"), \
                 patch("mailsorter.webapp._web_pass", "offline-admin"), \
                 patch("mailsorter.webapp.config_path", return_value=path):
                response = TestClient(app).get("/api/config/synthetic.yaml",
                                               headers={"Authorization": f"Basic {auth}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["imap_credential_source"], "inline_yaml")
        self.assertNotIn("synthetic-inline", str(response.json()))

    def test_secret_marker_without_existing_value_is_rejected(self):
        visible = _redacted_yaml(yaml.safe_dump(synthetic_config(), allow_unicode=True))
        auth = base64.b64encode(b"admin:offline-admin").decode("ascii")
        with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
             patch("mailsorter.webapp._load_web_credentials", return_value=True), \
             patch("mailsorter.webapp._web_user", "admin"), \
             patch("mailsorter.webapp._web_pass", "offline-admin"):
            response = TestClient(app).post("/api/validate", headers={"Authorization": f"Basic {auth}"},
                                            json={"content": visible})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
