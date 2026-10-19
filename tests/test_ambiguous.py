"""Offline tests for model abstention versus technical classification errors."""

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailsorter.config import LlmConfig, MailboxConfig, build_system_prompt
from mailsorter.llm_client import LlmClient, LlmError
from mailsorter.sorter import MailSorter, resolve_destination
from mailsorter.webapp import app
from tests.test_safety import FakeImap


REFUSAL = json.dumps({
    "category": "", "confidence": 0.0,
    "reason": "Недостаточно сведений, чтобы выбрать папку по правилам",
}, ensure_ascii=False)


def mailbox_config(provider="ollama") -> MailboxConfig:
    llm = {"provider": provider, "max_retries": 1}
    if provider == "cloudru":
        llm["api_key"] = "ENV:CLOUDRU_FOUNDATION_MODELS_API_KEY"
    return MailboxConfig.model_validate({
        "name": "synthetic",
        "imap": {"host": "example.invalid", "username": "fake", "password": "unused",
                 "unsorted_folder": "INBOX/NeedsReview"},
        "llm": llm,
        "rules": {"source_folder": "INBOX", "destinations": [
            {"folder": "INBOX/Work", "condition": "Конкретные рабочие заявки"},
        ]},
    })


class ParsedFakeLlm:
    raw = REFUSAL
    failure = None

    def __init__(self, cfg):
        self.cfg = cfg

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def classify(self, subject, sender, body):
        if self.failure:
            raise self.failure
        with LlmClient(self.cfg) as parser:
            return parser._parse_json(self.raw)


class AmbiguousDecisionTests(unittest.TestCase):
    def setUp(self):
        FakeImap.instances.clear()
        ParsedFakeLlm.raw = REFUSAL
        ParsedFakeLlm.failure = None
        self.imap_patch = patch("mailsorter.sorter.ImapClient", FakeImap)
        self.llm_patch = patch("mailsorter.sorter.LlmClient", ParsedFakeLlm)
        self.imap_patch.start()
        self.llm_patch.start()
        self.addCleanup(self.imap_patch.stop)
        self.addCleanup(self.llm_patch.stop)

    def test_ambiguous_letter_plans_configured_unsorted_without_mutation(self):
        for provider in ("ollama", "cloudru"):
            with self.subTest(provider=provider):
                cfg = mailbox_config(provider)
                FakeImap.instances.clear()
                with patch.object(FakeImap, "get_message_text", return_value="Здравствуйте! У меня возникла проблема. Помогите, пожалуйста, разобраться"):
                    stats = MailSorter(cfg).run_once()
                fake = FakeImap.instances[-1]
                self.assertEqual((stats.mode, stats.planned, stats.errors), ("dry_run", 1, 0))
                self.assertEqual(stats.details[0]["action"], "planned")
                self.assertEqual(stats.details[0]["folder"], "INBOX/NeedsReview")
                self.assertEqual(stats.details[0]["decision"], "no_matching_category")
                self.assertIn("Недостаточно сведений", stats.details[0]["reason"])
                self.assertEqual(fake.moves, [])
                self.assertEqual(fake.creates, [])

    def test_valid_refusal_routes_to_configured_unsorted_in_fake_active_mode(self):
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [("101", "INBOX/NeedsReview")])
        self.assertEqual((stats.unsorted, stats.moved, stats.errors), (1, 0, 0))

    def test_technical_llm_failure_retries_next_run_without_move(self):
        ParsedFakeLlm.failure = LlmError("synthetic timeout")
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.details[0]["action"], "retry_next_run")
        self.assertEqual(stats.errors, 1)

    def test_unknown_category_stays_in_source(self):
        ParsedFakeLlm.raw = json.dumps({"category": "INBOX/NotAllowed", "confidence": 0.99,
                                        "reason": "synthetic"})
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.details[0]["action"], "retry_next_run")
        self.assertEqual(stats.errors, 1)

    def test_invalid_json_stays_in_source(self):
        ParsedFakeLlm.raw = '{"category": '
        stats = MailSorter(mailbox_config()).run_once(mode="active")
        self.assertEqual(FakeImap.instances[-1].moves, [])
        self.assertEqual(stats.details[0]["action"], "retry_next_run")
        self.assertEqual(stats.errors, 1)


class AbstentionParsingTests(unittest.TestCase):
    def test_valid_refusal_is_distinct_from_llm_error_for_both_providers(self):
        for provider in ("ollama", "cloudru"):
            with self.subTest(provider=provider):
                cfg = mailbox_config(provider)
                with LlmClient(cfg.llm) as llm:
                    result = llm._parse_json(REFUSAL)
                self.assertEqual(result["category"], "")
                self.assertTrue(result["abstained"])
                self.assertIn("Недостаточно", result["reason"])

    def test_missing_null_and_unexplained_category_are_not_valid_refusals(self):
        cfg = mailbox_config()
        cases = [
            {"confidence": 0.0, "reason": "Нет данных"},
            {"category": None, "confidence": 0.0, "reason": "Нет данных"},
            {"category": "", "confidence": 0.0, "reason": ""},
            {"category": "   ", "confidence": 0.0, "reason": "Нет данных"},
            {"category": "", "confidence": 0.0, "reason": "Нет данных"},
        ]
        with LlmClient(cfg.llm) as llm:
            for case in cases[:-1]:
                with self.subTest(case=case), self.assertRaises(LlmError):
                    llm._parse_json(json.dumps(case))
            self.assertTrue(llm._parse_json(json.dumps(cases[-1]))["abstained"])

    def test_prompt_explicitly_allows_explained_empty_category(self):
        prompt = build_system_prompt(mailbox_config().rules.destinations)
        self.assertIn("category пустой строкой", prompt)
        self.assertIn("Непустая category", prompt)

    def test_valid_refusal_without_optional_marker_still_uses_config(self):
        result = {"category": "", "confidence": 0.0, "reason": "Нет достаточных сведений"}
        self.assertEqual(resolve_destination(result, mailbox_config())[2], "INBOX/NeedsReview")


class AmbiguousWebTests(unittest.TestCase):
    def test_manual_test_shows_planned_configured_folder_without_imap(self):
        cfg = mailbox_config()
        auth = base64.b64encode(b"admin:offline-password").decode()
        with LlmClient(cfg.llm) as parser:
            result = parser._parse_json(REFUSAL)

        class FakeWebLlm:
            def __init__(self, _cfg):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def classify(self, *_args):
                return result.copy()

        with patch("mailsorter.webapp.is_setup_mode", return_value=False), \
             patch("mailsorter.webapp._load_web_credentials", return_value=True), \
             patch("mailsorter.webapp._web_user", "admin"), \
             patch("mailsorter.webapp._web_pass", "offline-password"), \
             patch("mailsorter.webapp.load_mailbox_config", return_value=cfg), \
             patch("mailsorter.webapp.LlmClient", FakeWebLlm), \
             patch("mailsorter.webapp.ImapClient") as imap:
            response = TestClient(app).post("/api/test", headers={"Authorization": f"Basic {auth}"},
                                            json={"file": "synthetic.yaml", "subject": "Нужна помощь",
                                                  "text": "Помогите разобраться"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["folder"], "INBOX/NeedsReview")
        self.assertEqual(response.json()["result"]["action"], "planned")
        imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
