"""Small, dependency-free helpers for web credentials and YAML redaction."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from copy import deepcopy
from typing import Any

REDACTED = "<REDACTED>"
_SECRET_NAMES = {"password", "admin_password", "api_key", "token", "secret", "key_secret"}
_LOG_SECRET = re.compile(r"(?i)\b([\w.-]*(?:password|passwd|api[_-]?key|token|secret|authorization)[\w.-]*\s*[:=]\s*)([^\s,;]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(password, stored)
    try:
        _, count, salt, expected = stored.split("$", 3)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(count))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: (REDACTED if key.lower() in _SECRET_NAMES else redact_secrets(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return deepcopy(value)


def restore_redacted(value: Any, original: Any) -> Any:
    if isinstance(value, dict):
        old = original if isinstance(original, dict) else {}
        return {key: (old[key] if key.lower() in _SECRET_NAMES and item == REDACTED and key in old
                      else restore_redacted(item, old.get(key))) for key, item in value.items()}
    if isinstance(value, list):
        old = original if isinstance(original, list) else []
        return [restore_redacted(item, old[i] if i < len(old) else None) for i, item in enumerate(value)]
    return value


def has_unresolved_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any((key.lower() in _SECRET_NAMES and item == REDACTED)
                   or has_unresolved_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(has_unresolved_secret(item) for item in value)
    return False


def sanitize_log(text: str) -> str:
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _LOG_SECRET.sub(lambda match: match.group(1) + REDACTED, text)
    return _API_TOKEN.sub(REDACTED, text)
