"""Offline folder layout, preparation and administrative safety regressions."""

from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from imap_tools.utils import encode_folder
import yaml

from mailsorter.config import MailboxConfig
from mailsorter.imap_client import ImapClient, MoveNotStartedError, destination_path
from mailsorter.sorter import MailSorter
from mailsorter.webapp import app, _build_config_yaml, _folder_csrf_token, _protect_configured_folders
from tests.test_safety import FakeImap, FakeLlm, FakeMoveJournal, mailbox_config
from tests.test_move_lifecycle import message, prepared_client


def config(*, layout="inbox", destinations=None, unsorted="Unsorted"):
    return MailboxConfig.model_validate({
        "name": "synthetic", "folder_layout": layout,
        "imap": {"host": "example.invalid", "username": "synthetic", "password": "unused",
                 "unsorted_folder": unsorted},
        "rules": {"source_folder": "INBOX", "destinations": destinations or [
            {"folder": "Доступы", "condition": "Заявки на доступ"},
            {"folder": "1С", "condition": "Вопросы 1С"},
        ]},
    })


class FakePreparationImap:
    state = None

    def __init__(self, cfg):
        self.cfg = SimpleNamespace(inbox_folder=self.state["inbox"])
        self._folder_delimiter = self.state["delimiter"]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def folder_snapshot(self):
        self.state["lists"] += 1
        if self.state.get("list_error"):
            raise OSError("synthetic LIST failure")
        return list(self.state["folders"])

    def _resolve_folder_name(self, wanted, existing):
        return next((name for name in existing if name.casefold() == wanted.casefold()), None)

    def create_folder(self, name):
        self.state["creates"].append(name)
        if name in self.state.get("fail", set()):
            raise PermissionError("synthetic CREATE denied")
        self.state["folders"].add(name)
        return name


class FolderPathTests(unittest.TestCase):
    def test_nested_paths_delimiters_cyrillic_and_inbox_case(self):
        self.assertEqual(destination_path("Доступы", "inbox", "Inbox", "/"), "Inbox/Доступы")
        self.assertEqual(destination_path("1С", "inbox", "INBOX", "."), "INBOX.1С")
        self.assertEqual(destination_path("inbox/Сеть и VPN", "inbox", "Inbox", "."),
                         "Inbox.Сеть и VPN")
        self.assertEqual(destination_path("Other/Archive", "inbox", "INBOX", "."),
                         "Other.Archive")

    def test_legacy_config_retains_explicit_and_root_destinations(self):
        old = mailbox_config()
        self.assertEqual(old.folder_layout, "legacy")
        self.assertEqual(destination_path(old.rules.destinations[0].folder, old.folder_layout,
                                          "Inbox", "."), "Inbox.Work")
        self.assertEqual(destination_path("Work", old.folder_layout, "Inbox", "."), "Work")

    def test_invalid_path_is_rejected(self):
        for name in ("", "INBOX//Bad", "../Bad", "Bad\nName"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                destination_path(name, "inbox", "INBOX", "/")

    def test_imap_tools_encodes_cyrillic_folder_names(self):
        encoded = encode_folder("INBOX/Доступы")
        self.assertIsInstance(encoded, bytes)
        self.assertNotIn("Доступы".encode("utf-8"), encoded)


class FolderPreparationTests(unittest.TestCase):
    def setUp(self):
        FakePreparationImap.state = {"inbox": "Inbox", "delimiter": ".",
                                     "folders": {"Inbox", "Inbox.Доступы"},
                                     "creates": [], "lists": 0, "fail": set()}
        self.patcher = patch("mailsorter.sorter.ImapClient", FakePreparationImap)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_check_reports_full_paths_and_does_not_mutate(self):
        result = MailSorter(config()).check_folders()
        self.assertTrue(result["ok"])
        self.assertFalse(result["ready"])
        self.assertEqual(set(result["missing"]), {"Inbox.1С", "Inbox.Unsorted"})
        self.assertIn("Inbox.Доступы", result["existing"])
        self.assertIn("Inbox", result["server_folders"])
        self.assertEqual(FakePreparationImap.state["creates"], [])

    def test_create_missing_is_idempotent_and_does_not_touch_messages(self):
        sorter = MailSorter(config())
        first = sorter.check_folders(create=True)
        self.assertTrue(first["ready"])
        self.assertEqual(set(first["created"]), {"Inbox.1С", "Inbox.Unsorted"})
        self.assertEqual(set(FakePreparationImap.state["creates"]), set(first["created"]))
        second = sorter.check_folders(create=True)
        self.assertEqual(second["created"], [])
        self.assertEqual(set(FakePreparationImap.state["creates"]), set(first["created"]))

    def test_new_category_is_picked_up_without_code_change(self):
        cfg = config()
        cfg.rules.destinations.append(type(cfg.rules.destinations[0])(
            folder="Оборудование", condition="Поломки"))
        result = MailSorter(cfg).check_folders()
        self.assertIn("Inbox.Оборудование", result["missing"])

    def test_removed_category_does_not_delete_existing_folder(self):
        FakePreparationImap.state["folders"].add("Inbox.Старая категория")
        result = MailSorter(config()).check_folders(create=True)
        self.assertTrue(result["ready"])
        self.assertIn("Inbox.Старая категория", FakePreparationImap.state["folders"])
        self.assertNotIn("Inbox.Старая категория", FakePreparationImap.state["creates"])

    def test_legacy_root_destinations_are_not_migrated(self):
        legacy = config(layout="legacy", unsorted="Unsorted",
                        destinations=[{"folder": "Work", "condition": "Работа"}])
        result = MailSorter(legacy).check_folders()
        self.assertEqual(set(result["missing"]), {"Work", "Unsorted"})
        self.assertNotIn("Inbox.Work", result["missing"])

    def test_partial_creation_and_create_error_are_reported(self):
        FakePreparationImap.state["fail"] = {"Inbox.1С"}
        result = MailSorter(config()).check_folders(create=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["ready"])
        self.assertIn("Inbox.Unsorted", result["created"])
        self.assertIn("Inbox.1С", result["failed"])
        self.assertIn("PermissionError", result["errors"])

    def test_missing_source_prevents_all_creation(self):
        FakePreparationImap.state["folders"] = set()
        result = MailSorter(config()).check_folders(create=True)
        self.assertFalse(result["ready"])
        self.assertEqual(FakePreparationImap.state["creates"], [])

    def test_list_error_is_not_interpreted_as_empty_structure(self):
        FakePreparationImap.state["list_error"] = True
        result = MailSorter(config()).check_folders(create=True)
        self.assertFalse(result["ok"])
        self.assertEqual(FakePreparationImap.state["creates"], [])


class ImapFolderSafetyTests(unittest.TestCase):
    def test_move_to_missing_folder_never_sends_move_or_create(self):
        client, mailbox = prepared_client()
        mailbox.folder.list.return_value = [SimpleNamespace(name="INBOX", delim="/")]
        with self.assertRaises(MoveNotStartedError) as caught:
            client.move_message(message(), "INBOX/Missing")
        self.assertEqual(caught.exception.stage, "prepare_destination")
        mailbox.move.assert_not_called()
        mailbox.folder.create.assert_not_called()

    def test_existing_folder_is_reused_without_create(self):
        client, mailbox = prepared_client()
        mailbox.folder.list.return_value = [SimpleNamespace(name="INBOX", delim="/"),
                                             SimpleNamespace(name="Inbox/Доступы", delim="/")]
        self.assertEqual(client.ensure_folder("INBOX/Доступы"), "Inbox/Доступы")
        mailbox.folder.create.assert_not_called()

    def test_unicode_create_is_delegated_to_imap_tools(self):
        client, mailbox = prepared_client()
        folders = [SimpleNamespace(name="INBOX", delim="/")]
        mailbox.folder.list.side_effect = lambda: list(folders)
        mailbox.folder.create.side_effect = lambda name: folders.append(SimpleNamespace(name=name, delim="/"))
        self.assertEqual(client.create_folder("INBOX/Доступы"), "INBOX/Доступы")
        mailbox.folder.create.assert_called_once_with("INBOX/Доступы")

    def test_configured_inbox_categories_are_protected(self):
        cfg = config()
        imap = ImapClient(cfg.imap)
        imap._folder_delimiter = "."
        imap.cfg.inbox_folder = "Inbox"
        _protect_configured_folders(cfg, imap)
        self.assertIn("Inbox.Доступы", imap.protected_folders)
        self.assertIn("Inbox.Unsorted", imap.protected_folders)

    def test_dry_run_and_active_share_resolved_target(self):
        class PathFakeImap(FakeImap):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.cfg = SimpleNamespace(inbox_folder="Inbox")
                self._folder_delimiter = "."
        FakeLlm.answer = {"category": "Доступы", "confidence": 0.95}
        with patch("mailsorter.sorter.ImapClient", PathFakeImap), \
             patch("mailsorter.sorter.LlmClient", FakeLlm), \
             patch("mailsorter.sorter.MoveJournal", FakeMoveJournal):
            dry = MailSorter(config()).run_once()
            active = MailSorter(config()).run_once("active")
        self.assertEqual(dry.details[0]["folder"], "Inbox.Доступы")
        self.assertEqual(active.details[0]["folder"], "Inbox.Доступы")
        self.assertEqual(active.moved, 1)
        self.assertEqual(dry.moved, 0)
        self.assertEqual(PathFakeImap.instances[-1].moves, [("101", "Inbox.Доступы")])


class FolderWebTests(unittest.TestCase):
    def test_new_setup_uses_inbox_layout(self):
        text = _build_config_yaml({
            "name": "synthetic", "imap": {"host": "example.invalid", "username": "user@example.invalid",
                                         "password": "ENV:TEST_MAIL_PASSWORD"},
            "destinations": [{"folder": "Доступы", "condition": "Заявки"}],
            "web": {"admin_password": "synthetic-password"},
        })
        raw = yaml.safe_load(text)
        self.assertEqual(raw["folder_layout"], "inbox")
        self.assertEqual(raw["rules"]["destinations"][0]["folder"], "Доступы")

    def test_create_requires_auth_csrf_and_explicit_confirmation(self):
        credentials = base64.b64encode(b"admin:fake-password").decode("ascii")
        auth = {"Authorization": f"Basic {credentials}"}
        with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
             patch("mailsorter.webapp._load_web_credentials", return_value=True), \
             patch("mailsorter.webapp._web_user", "admin"), \
             patch("mailsorter.webapp._web_pass", "fake-password"), \
             patch("mailsorter.webapp._get_sorter") as get_sorter:
            get_sorter.return_value.check_folders.return_value = {"ok": True, "ready": True}
            client = TestClient(app)
            page = client.get("/folders", headers=auth)
            self.assertEqual(page.status_code, 200)
            self.assertIn(_folder_csrf_token, page.text)
            url = "/api/check-folders?file=synthetic.yaml&create=true"
            self.assertEqual(client.post(url, json={}).status_code, 401)
            self.assertEqual(client.post(url, headers=auth, json={}).status_code, 403)
            csrf = {**auth, "X-CSRF-Token": _folder_csrf_token}
            self.assertEqual(client.post(url, headers=csrf, json={}).status_code, 400)
            self.assertEqual(client.post(url, headers={**csrf, "Origin": "http://evil.invalid"},
                                         json={"confirmation": "CREATE synthetic.yaml"}).status_code, 403)
            self.assertEqual(client.post(url, headers=csrf,
                                         json={"confirmation": "CREATE synthetic.yaml"}).status_code, 200)
            get_sorter.return_value.check_folders.assert_called_once_with(create=True)
            self.assertEqual(client.post("/api/folders/synthetic.yaml/create", headers=auth,
                                         json={"name": "INBOX/Test"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
