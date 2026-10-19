"""Автоопределение настроек Microsoft Exchange / Office 365."""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree import ElementTree

import httpx

logger = logging.getLogger("mailsorter.exchange")

# Известные пресеты
PRESETS: dict[str, dict[str, Any]] = {
    "microsoft365": {
        "label": "Microsoft 365 / Exchange Online",
        "host": "outlook.office365.com",
        "port": 993,
        "inbox_folder": "INBOX",
    },
    "outlook_com": {
        "label": "Outlook.com / Hotmail / Live",
        "host": "outlook.office365.com",
        "port": 993,
        "inbox_folder": "INBOX",
    },
    "gmail": {
        "label": "Gmail",
        "host": "imap.gmail.com",
        "port": 993,
        "inbox_folder": "INBOX",
    },
    "yandex": {
        "label": "Yandex",
        "host": "imap.yandex.ru",
        "port": 993,
        "inbox_folder": "INBOX",
    },
    "mailru": {
        "label": "Mail.ru",
        "host": "imap.mail.ru",
        "port": 993,
        "inbox_folder": "INBOX",
    },
}

MICROSOFT_DOMAINS = {
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "office365.com",
}


def detect_preset_by_email(email: str) -> dict[str, Any] | None:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    domain = email.split("@", 1)[1]
    if domain in MICROSOFT_DOMAINS:
        return {**PRESETS["outlook_com"], "id": "outlook_com", "detected": True}
    if domain in ("gmail.com", "googlemail.com"):
        return {**PRESETS["gmail"], "id": "gmail", "detected": True}
    if domain in ("yandex.ru", "yandex.com", "ya.ru"):
        return {**PRESETS["yandex"], "id": "yandex", "detected": True}
    if domain in ("mail.ru", "inbox.ru", "bk.ru", "list.ru"):
        return {**PRESETS["mailru"], "id": "mailru", "detected": True}
    # Корпоративные домены часто на Exchange Online
    return None


def autodiscover_microsoft(email: str, timeout: float = 10.0) -> dict[str, Any]:
    """
    Пытается получить IMAP-настройки через Microsoft Autodiscover.
    Для Exchange Online почти всегда возвращает outlook.office365.com.
    """
    email = (email or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "host": "outlook.office365.com",
        "port": 993,
        "inbox_folder": "INBOX",
        "source": "preset",
        "message": "",
    }

    preset = detect_preset_by_email(email)
    if preset:
        result.update(
            {
                "ok": True,
                "host": preset["host"],
                "port": preset["port"],
                "inbox_folder": preset.get("inbox_folder", "INBOX"),
                "source": "domain",
                "label": preset.get("label"),
                "message": f"Определено по домену: {preset.get('label')}",
            }
        )
        return result

    # JSON Autodiscover (Office 365)
    try:
        url = "https://outlook.office365.com/autodiscover/autodiscover.json/v1.0/" + email
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                # Иногда возвращает Url — этого достаточно, чтобы считать M365
                result.update(
                    {
                        "ok": True,
                        "host": "outlook.office365.com",
                        "port": 993,
                        "source": "autodiscover_json",
                        "label": "Microsoft 365 / Exchange Online",
                        "message": "Autodiscover: Microsoft 365",
                        "raw": data if isinstance(data, dict) else None,
                    }
                )
                return result
    except Exception as e:
        logger.debug("JSON autodiscover failed: %s", e)

    # XML Autodiscover (классический Exchange)
    try:
        domain = email.split("@", 1)[1] if "@" in email else ""
        urls = [
            f"https://autodiscover.{domain}/autodiscover/autodiscover.xml",
            f"https://{domain}/autodiscover/autodiscover.xml",
            "https://autodiscover-s.outlook.com/autodiscover/autodiscover.xml",
        ]
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/requestschema/2006">
  <Request>
    <EMailAddress>{email}</EMailAddress>
    <AcceptableResponseSchema>http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a</AcceptableResponseSchema>
  </Request>
</Autodiscover>"""
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.post(
                        url,
                        content=body.encode("utf-8"),
                        headers={"Content-Type": "text/xml; charset=utf-8"},
                    )
                    if resp.status_code != 200:
                        continue
                    # Ищем Server в ответе
                    text = resp.text
                    m = re.search(
                        r"<Server[^>]*>([^<]+)</Server>",
                        text,
                        re.IGNORECASE,
                    )
                    # Для IMAP иногда Type=IMAP
                    imap_host = None
                    try:
                        # namespace-agnostic search
                        for el in ElementTree.fromstring(text).iter():
                            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                            if tag.lower() == "protocol":
                                proto_type = None
                                server = None
                                port = None
                                for child in el:
                                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                                    if ctag.lower() == "type" and child.text:
                                        proto_type = child.text.strip().upper()
                                    if ctag.lower() == "server" and child.text:
                                        server = child.text.strip()
                                    if ctag.lower() == "port" and child.text:
                                        try:
                                            port = int(child.text.strip())
                                        except ValueError:
                                            pass
                                if proto_type == "IMAP" and server:
                                    imap_host = server
                                    result["port"] = port or 993
                                    break
                    except ElementTree.ParseError:
                        if m:
                            imap_host = m.group(1).strip()

                    if imap_host:
                        result.update(
                            {
                                "ok": True,
                                "host": imap_host,
                                "source": "autodiscover_xml",
                                "label": "Microsoft Exchange",
                                "message": f"Autodiscover XML: {imap_host}",
                            }
                        )
                        return result
                    # Если XML ответил, но IMAP не нашли — всё равно M365
                    if "Autodiscover" in text or "Exchange" in text:
                        result.update(
                            {
                                "ok": True,
                                "host": "outlook.office365.com",
                                "source": "autodiscover_xml",
                                "label": "Microsoft Exchange / 365",
                                "message": "Autodiscover ответил (используем outlook.office365.com)",
                            }
                        )
                        return result
                except Exception as e:
                    logger.debug("autodiscover %s: %s", url, e)
    except Exception as e:
        logger.debug("XML autodiscover failed: %s", e)

    # Fallback: если корпоративный email — предложить M365
    if email and "@" in email:
        result.update(
            {
                "ok": True,
                "host": "outlook.office365.com",
                "port": 993,
                "source": "fallback_m365",
                "label": "Microsoft 365 (предположение)",
                "message": (
                    "Автоопределение не нашло точных данных. "
                    "Для корпоративных Microsoft-ящиков обычно подходит outlook.office365.com. "
                    "Нужен пароль приложения или современная аутентификация."
                ),
            }
        )
        return result

    result["message"] = "Не удалось определить настройки. Укажите IMAP вручную."
    return result
