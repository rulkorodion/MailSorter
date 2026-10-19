"""Клиент локального Ollama API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import LlmConfig

logger = logging.getLogger("mailsorter.llm")


class LlmClient:
    def __init__(self, cfg: LlmConfig):
        self.cfg = cfg

    def classify(self, subject: str, sender: str, body: str) -> dict[str, Any]:
        """
        Отправляет письмо в модель.
        Возвращает {"category": str, "confidence": float}.
        Бросает ValueError при невозможности получить валидный JSON.
        """
        user_content = (
            f"Отправитель: {sender}\n"
            f"Тема: {subject}\n"
            f"Текст письма:\n{body}"
        )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": self.cfg.system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_predict": self.cfg.max_tokens,
            },
        }
        url = f"{self.cfg.endpoint.rstrip('/')}/api/chat"
        try:
            with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error("Ошибка запроса к Ollama: %s", type(e).__name__)
            raise

        raw = data.get("message", {}).get("content", "") or data.get("response", "")
        return self._parse_json(raw)

    def _parse_json(self, text: str) -> dict[str, Any]:
        """Извлекает JSON из ответа модели (даже если есть markdown)."""
        text = text.strip()
        # Попытка 1: весь текст — JSON
        try:
            obj = json.loads(text)
            return self._validate(obj)
        except json.JSONDecodeError:
            pass

        # Попытка 2: найти блок ```json ... ```
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                obj = json.loads(match.group(1))
                return self._validate(obj)
            except json.JSONDecodeError:
                pass

        # Попытка 3: первый { ... }
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                obj = json.loads(match.group(0))
                return self._validate(obj)
            except json.JSONDecodeError:
                pass

        raise ValueError("Модель не вернула валидный JSON")

    def _validate(self, obj: dict) -> dict[str, Any]:
        if "category" not in obj or "confidence" not in obj:
            raise ValueError("В JSON нет category/confidence")
        conf = float(obj["confidence"])
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence вне диапазона 0-1: {conf}")
        return {"category": str(obj["category"]).strip(), "confidence": conf}

    def chat(self, system: str, user: str, temperature: float | None = None, max_tokens: int | None = None) -> str:
        """Свободный ответ модели (текст, не JSON)."""
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature if temperature is None else temperature,
                "num_predict": self.cfg.max_tokens if max_tokens is None else max_tokens,
            },
        }
        url = f"{self.cfg.endpoint.rstrip('/')}/api/chat"
        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return (
            data.get("message", {}).get("content", "")
            or data.get("response", "")
            or ""
        ).strip()
