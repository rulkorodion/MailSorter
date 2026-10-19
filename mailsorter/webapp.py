"""Веб-интерфейс FastAPI."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .config import (
    CONFIGS_DIR,
    config_path,
    ImapConfig,
    build_system_prompt,
    delete_config,
    destinations_to_dict,
    list_config_files,
    load_mailbox_config,
    load_raw_yaml,
    validate_yaml_text,
    write_raw_yaml,
)
from .exchange import PRESETS, autodiscover_microsoft
from .imap_client import ImapClient
from .llm_client import LlmClient, LlmError
from .manager import manager
from .sorter import MailSorter
from .autorule import autorule_manager, MIN_EMAILS
from .security import REDACTED, has_unresolved_secret, hash_password, redact_secrets, restore_redacted, sanitize_log, verify_password

logger = logging.getLogger("mailsorter.web")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR = BASE_DIR / "web" / "static"

app = FastAPI(title="Mail Sorter", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

security = HTTPBasic(auto_error=False)

_web_user = "admin"
_web_pass = "CHANGE_ME"


def is_setup_mode() -> bool:
    return len(list_config_files()) == 0


def _require_local_setup(request: Request) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Первоначальная настройка доступна только локально")


def _redacted_yaml(text: str) -> str:
    raw = yaml.safe_load(text)
    return yaml.safe_dump(redact_secrets(raw), allow_unicode=True, sort_keys=False)


def _restore_yaml(text: str, filename: str) -> str:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("Корень YAML должен быть словарём")
    path = config_path(filename)
    if path.exists():
        old = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw = restore_redacted(raw, old)
    if has_unresolved_secret(raw):
        raise ValueError("Не указан секрет")
    _hash_web_password(raw)
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)


def _hash_web_password(raw: dict[str, Any]) -> None:
    web = raw.get("web") or {}
    if not isinstance(web, dict):
        return
    password = web.get("admin_password")
    if isinstance(password, str) and password and not password.startswith("pbkdf2_sha256$"):
        web["admin_password"] = hash_password(password)


def _public_error(error: Exception) -> str:
    """Avoid returning exception text that may contain credentials."""
    return f"Операция не выполнена ({type(error).__name__})"


def _load_web_credentials() -> bool:
    global _web_user, _web_pass
    for path in list_config_files():
        try:
            cfg = load_mailbox_config(path)
            if not cfg.web.admin_password or cfg.web.admin_password == "CHANGE_ME":
                continue
            _web_user = cfg.web.admin_username
            _web_pass = cfg.web.admin_password
            return True
        except Exception:
            continue
    return False


def verify_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    if is_setup_mode():
        _require_local_setup(request)
        return "setup"
    if not _load_web_credentials():
        raise HTTPException(status_code=503, detail="Нет корректной веб-конфигурации")
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, _web_user)
    ok_pass = verify_password(credentials.password, _web_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _get_sorter(filename: str) -> MailSorter:
    sorter = manager.get_sorter(filename)
    if sorter:
        return sorter
    cfg = load_mailbox_config(config_path(filename))
    return MailSorter(cfg)


def _build_config_yaml(data: dict[str, Any]) -> str:
    cats = data.get("destinations") or data.get("categories") or []
    destinations = []
    for c in cats:
        if "condition" in c:
            destinations.append({"folder": c["folder"], "condition": c["condition"]})
        else:
            destinations.append(
                {
                    "folder": c.get("folder") or "",
                    "condition": f'Письма категории «{c.get("name", "")}»',
                }
            )
    prompt = build_system_prompt(destinations)
    imap = data["imap"]
    llm = data.get("llm") or {}
    provider = llm.get("provider") or "ollama"
    web = data.get("web") or {}
    threshold = float(data.get("confidence_threshold", 0.75))

    doc = {
        "name": data.get("name") or "Основной ящик",
        "imap": {
            "host": imap["host"],
            "port": int(imap.get("port", 993)),
            "username": imap["username"],
            "password": imap["password"],
            "inbox_folder": imap.get("inbox_folder") or "INBOX",
            "unsorted_folder": imap.get("unsorted_folder") or "INBOX/Unsorted",
            "poll_interval_seconds": int(imap.get("poll_interval_seconds") or 60),
            "ssl_verify": True,
        },
        "llm": {
            "provider": provider,
            "endpoint": llm.get("endpoint") or "http://localhost:11434",
            "base_url": "https://foundation-models.api.cloud.ru/v1",
            "model": llm.get("model") or ("openai/gpt-oss-120b" if provider == "cloudru" else "qwen2.5:14b"),
            "temperature": (float(llm["temperature"]) if llm.get("temperature") not in (None, "") else
                            (None if provider == "cloudru" else 0.1)),
            "max_tokens": 2048 if provider == "cloudru" else 300,
            "timeout_seconds": 120,
            "max_retries": 3,
            "system_prompt": prompt,
        },
        "rules": {
            "source_folder": imap.get("inbox_folder") or "INBOX",
            "confidence_threshold": threshold,
            "destinations": destinations,
        },
        "web": {
            "host": "127.0.0.1",
            "port": int(web.get("port") or 8080),
            "admin_username": web.get("admin_username") or "admin",
            "admin_password": hash_password(web.get("admin_password") or "CHANGE_ME"),
        },
        "logging": {
            "level": "INFO",
            "file": "logs/mailsorter.log",
            "max_size_mb": 50,
            "backup_count": 3,
        },
    }
    if provider == "cloudru":
        doc["llm"]["api_key"] = "ENV:CLOUDRU_FOUNDATION_MODELS_API_KEY"
    return yaml.dump(doc, allow_unicode=True, default_flow_style=False, sort_keys=False)


# -------------------- Pages --------------------

@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(
        request, "index.html",
        {"status_list": manager.get_status(), "configs": [p.name for p in list_config_files()], "user": user},
    )


@app.get("/setup", response_class=HTMLResponse)
async def page_setup(request: Request):
    _require_local_setup(request)
    if not is_setup_mode():
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {"presets": PRESETS})


@app.get("/config", response_class=HTMLResponse)
async def page_config(request: Request, file: str | None = None, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    files = [p.name for p in list_config_files()]
    content, current = "", file or (files[0] if files else "")
    if current:
        try:
            path = config_path(current)
            if path.exists():
                content = _redacted_yaml(path.read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(400, "Недопустимое имя конфигурации")
    return templates.TemplateResponse(
        request, "config.html",
        {"files": files, "current": current, "content": content, "user": user},
    )


@app.get("/folders", response_class=HTMLResponse)
async def page_folders(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(
        request, "folders.html",
        {"status_list": manager.get_status(), "user": user},
    )


@app.get("/rules", response_class=HTMLResponse)
async def page_rules(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(
        request, "rules.html",
        {"files": [p.name for p in list_config_files()], "user": user},
    )


@app.get("/run", response_class=HTMLResponse)
async def page_run(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(
        request, "run.html",
        {"status_list": manager.get_status(), "user": user},
    )


@app.get("/test", response_class=HTMLResponse)
async def page_test(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(
        request, "test.html",
        {"files": [p.name for p in list_config_files()], "user": user},
    )


@app.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request, user: str = Depends(verify_auth)):
    if is_setup_mode():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse(request, "logs.html", {"user": user})


# -------------------- Setup / IMAP test / Autodiscover --------------------

@app.post("/api/setup")
async def api_setup(request: Request):
    _require_local_setup(request)
    if not is_setup_mode():
        return JSONResponse({"ok": False, "error": "Настройка уже выполнена"}, status_code=400)
    body = await request.json()
    filename = (body.get("filename") or "mailbox.yaml").strip()
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    try:
        path = config_path(filename)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Недопустимое имя конфигурации"}, status_code=400)
    if not body.get("imap", {}).get("host") or not body.get("imap", {}).get("username"):
        return JSONResponse({"ok": False, "error": "Укажите IMAP-сервер и логин"}, status_code=400)
    destinations = body.get("destinations") or []
    if not destinations:
        # legacy categories from wizard
        for c in body.get("categories") or []:
            destinations.append({
                "folder": c.get("folder", ""),
                "condition": f'Письма категории «{c.get("name", "")}»',
            })
    if not destinations:
        return JSONResponse({"ok": False, "error": "Добавьте хотя бы одну папку с условием"}, status_code=400)
    body["destinations"] = destinations
    web_pass = (body.get("web") or {}).get("admin_password") or ""
    if len(web_pass) < 12:
        return JSONResponse({"ok": False, "error": "Пароль веб должен содержать минимум 12 символов"}, status_code=400)
    try:
        text = _build_config_yaml(body)
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)
    ok, msg, _ = validate_yaml_text(text)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return JSONResponse({"ok": False, "error": "Файл уже существует"}, status_code=400)
    path.write_text(text, encoding="utf-8")
    manager.reload_all()
    return {"ok": True, "filename": filename}


@app.get("/api/setup-status")
async def api_setup_status(request: Request):
    if is_setup_mode():
        _require_local_setup(request)
    return {"setup_required": is_setup_mode()}


@app.post("/api/test-imap")
async def api_test_imap(request: Request, user: str = Depends(verify_auth)):
    """Проверка авторизации IMAP. В setup — без auth; иначе нужна auth или file."""
    body = await request.json()
    # Если передан file — берём из конфига (нужна auth)
    filename = body.get("file")
    if filename:
        try:
            sorter = _get_sorter(filename)
            return sorter.test_connection()
        except Exception as e:
            return {"ok": False, "message": _public_error(e)}

    host = body.get("host") or ""
    port = int(body.get("port") or 993)
    username = body.get("username") or ""
    password = body.get("password") or ""
    inbox = body.get("inbox_folder") or "INBOX"
    if not host or not username or not password:
        return {"ok": False, "message": "Укажите host, username и password"}
    try:
        cfg = ImapConfig(
            host=host, port=port, username=username, password=password, inbox_folder=inbox
        )
        with ImapClient(cfg) as imap:
            folders = imap.list_folders()
        return {
            "ok": True,
            "message": f"Подключение успешно: {username}@{host}",
            "folders_count": len(folders),
            "sample_folders": folders[:15],
        }
    except Exception as e:
        return {"ok": False, "message": _public_error(e)}


@app.post("/api/autodiscover")
async def api_autodiscover(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email:
        return {"ok": False, "message": "Укажите email"}
    return autodiscover_microsoft(email)


@app.get("/api/presets")
async def api_presets():
    return PRESETS


# -------------------- Status / run --------------------

@app.get("/api/status")
async def api_status(user: str = Depends(verify_auth)):
    return manager.get_status()


@app.post("/api/run")
async def api_run(file: str | None = None, user: str = Depends(verify_auth)):
    return manager.run_now(file)


@app.post("/api/mode/{filename}")
async def api_mode(filename: str, request: Request, user: str = Depends(verify_auth)):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.casefold() != request.headers.get("host", "").casefold():
        raise HTTPException(403, "Недопустимый источник запроса")
    body = await request.json()
    try:
        mode = manager.set_mode(filename, body.get("mode", ""), body.get("confirmation", ""))
        logger.warning("Режим ящика %s переключён в %s пользователем %s", filename, mode, user)
        return {"ok": True, "mode": mode}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)


@app.post("/api/reload")
async def api_reload(user: str = Depends(verify_auth)):
    manager.reload_all()
    return {"ok": True, "message": "Конфиги перечитаны"}


# -------------------- Config YAML --------------------

@app.get("/api/config/{filename}")
async def api_get_config(filename: str, user: str = Depends(verify_auth)):
    try:
        path = config_path(filename)
    except ValueError:
        raise HTTPException(400, "Недопустимое имя конфигурации")
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return {"filename": filename, "content": _redacted_yaml(path.read_text(encoding="utf-8"))}


@app.post("/api/config/{filename}")
async def api_save_config(filename: str, request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    try:
        path = config_path(filename)
        text = _restore_yaml(body.get("content", ""), filename)
    except (ValueError, yaml.YAMLError):
        return JSONResponse({"ok": False, "error": "Недопустимое имя или YAML"}, status_code=400)
    ok, msg, _ = validate_yaml_text(text)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    path.write_text(text, encoding="utf-8")
    manager.reload_one(filename)
    return {"ok": True, "message": "Сохранено и применено"}


@app.post("/api/config")
async def api_create_config(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    filename = (body.get("filename") or "").strip()
    if not filename:
        return JSONResponse({"ok": False, "error": "Укажите имя файла"}, status_code=400)
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    try:
        path = config_path(filename)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Недопустимое имя конфигурации"}, status_code=400)
    text = body.get("content", "")
    if REDACTED in text:
        return JSONResponse({"ok": False, "error": "Укажите собственные секреты для нового ящика"}, status_code=400)
    try:
        raw = yaml.safe_load(text)
        if isinstance(raw, dict):
            _hash_web_password(raw)
            text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError:
        pass
    ok, msg, _ = validate_yaml_text(text)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    if path.exists():
        return JSONResponse({"ok": False, "error": "Файл уже существует"}, status_code=400)
    path.write_text(text, encoding="utf-8")
    manager.reload_one(filename)
    return {"ok": True, "filename": filename}


@app.delete("/api/config/{filename}")
async def api_delete_config(filename: str, user: str = Depends(verify_auth)):
    try:
        delete_config(filename)
    except ValueError:
        raise HTTPException(400, "Недопустимое имя конфигурации")
    manager.reload_one(filename)
    return {"ok": True}


@app.post("/api/validate")
async def api_validate(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    ok, msg, _ = validate_yaml_text(body.get("content", ""))
    return {"ok": ok, "message": msg}


# -------------------- Folders --------------------

@app.post("/api/check-folders")
async def api_check_folders(file: str, create: bool = False, user: str = Depends(verify_auth)):
    return _get_sorter(file).check_folders(create=create)


@app.get("/api/folders/{filename}")
async def api_list_folders(filename: str, user: str = Depends(verify_auth)):
    return _get_sorter(filename).folder_tree()


@app.post("/api/folders/{filename}/create")
async def api_folder_create(filename: str, request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Укажите имя папки"}, status_code=400)
    try:
        with ImapClient(_get_sorter(filename).config.imap) as imap:
            actual = imap.create_folder(name)
        return {"ok": True, "name": actual}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)


@app.post("/api/folders/{filename}/delete")
async def api_folder_delete(filename: str, request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    try:
        cfg = _get_sorter(filename).config
        protected = [cfg.imap.inbox_folder, cfg.imap.unsorted_folder, cfg.rules.source_folder]
        protected.extend(d.folder for d in cfg.rules.destinations)
        with ImapClient(cfg.imap, protected_folders=protected) as imap:
            imap.delete_folder(name)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)


@app.post("/api/folders/{filename}/rename")
async def api_folder_rename(filename: str, request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    old_name = (body.get("old_name") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    try:
        cfg = _get_sorter(filename).config
        protected = [cfg.imap.inbox_folder, cfg.imap.unsorted_folder, cfg.rules.source_folder]
        protected.extend(d.folder for d in cfg.rules.destinations)
        with ImapClient(cfg.imap, protected_folders=protected) as imap:
            imap.rename_folder(old_name, new_name)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)


# -------------------- Rules (folder + condition) --------------------

@app.get("/api/rules/{filename}")
async def api_get_rules(filename: str, user: str = Depends(verify_auth)):
    try:
        cfg = load_mailbox_config(config_path(filename))
        data = destinations_to_dict(cfg)
        data["ok"] = True
        data["filename"] = filename
        return data
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)


@app.put("/api/rules/{filename}")
async def api_save_rules(filename: str, request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    destinations = body.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        return JSONResponse({"ok": False, "error": "Нужна хотя бы одна папка с условием"}, status_code=400)
    for d in destinations:
        if not d.get("folder") or not d.get("condition"):
            return JSONResponse(
                {"ok": False, "error": "У каждой записи должны быть folder и condition"},
                status_code=400,
            )
    try:
        raw = load_raw_yaml(filename)
        prompt = build_system_prompt(destinations)
        raw["rules"] = {
            "source_folder": body.get("source_folder") or raw.get("imap", {}).get("inbox_folder") or "INBOX",
            "confidence_threshold": float(body.get("confidence_threshold") or 0.75),
            "destinations": destinations,
        }
        # убрать legacy
        raw["rules"].pop("items", None)
        raw["rules"].pop("categories", None)
        if "llm" not in raw:
            raw["llm"] = {}
        raw["llm"]["system_prompt"] = prompt
        text = yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=False)
        ok, msg, _ = validate_yaml_text(text)
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        write_raw_yaml(filename, raw)
        manager.reload_one(filename)
        return {"ok": True, "message": "Правила сохранены", "system_prompt": prompt}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=500)


@app.post("/api/preview-prompt")
async def api_preview_prompt(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    destinations = body.get("destinations") or []
    return {"system_prompt": build_system_prompt(destinations)}


# -------------------- Test classify + messages --------------------

@app.get("/api/messages/{filename}")
async def api_messages(
    filename: str,
    folder: str | None = None,
    limit: int = 30,
    user: str = Depends(verify_auth),
):
    return _get_sorter(filename).list_messages(folder=folder, limit=limit)


@app.post("/api/test")
async def api_test(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    filename = body.get("file")
    if not filename:
        return JSONResponse({"ok": False, "error": "Выберите конфиг"}, status_code=400)
    try:
        cfg = load_mailbox_config(config_path(filename))
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)

    prompt = body.get("system_prompt") or build_system_prompt(cfg.rules.destinations)
    llm_cfg = cfg.llm.model_copy(update={"system_prompt": prompt})
    try:
        with LlmClient(llm_cfg) as llm:
            result = await run_in_threadpool(
                llm.classify,
                body.get("subject", ""),
                body.get("sender", "test@example.com"),
                body.get("text", ""),
            )
        return {"ok": True, "provider": cfg.llm.provider, "model": cfg.llm.model, "result": result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e) if isinstance(e, LlmError) else _public_error(e)},
                            status_code=502)


@app.post("/api/test-model/{filename}")
async def api_test_model(filename: str, user: str = Depends(verify_auth)):
    """Synthetic model-only probe: never reads IMAP or a real message."""
    try:
        cfg = load_mailbox_config(config_path(filename))
        with LlmClient(cfg.llm) as llm:
            answer = await run_in_threadpool(llm.chat, "Ответь одним словом OK.", "Проверка соединения.")
            metrics = llm.last_response
        if not answer:
            raise LlmError("Модель вернула пустой ответ")
        return {"ok": True, "provider": cfg.llm.provider, "model": cfg.llm.model,
                "latency_ms": metrics.latency_ms if metrics else None,
                "usage": metrics.usage if metrics else None}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e) if isinstance(e, LlmError) else _public_error(e)},
                            status_code=502)


@app.get("/api/llm-settings/{filename}")
async def api_llm_settings(filename: str, user: str = Depends(verify_auth)):
    try:
        cfg = load_mailbox_config(config_path(filename))
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)
    return {"ok": True, "provider": cfg.llm.provider, "model": cfg.llm.model,
            "temperature": cfg.llm.temperature, "max_tokens": cfg.llm.max_tokens}


@app.post("/api/llm-settings/{filename}")
async def api_save_llm_settings(filename: str, request: Request, user: str = Depends(verify_auth)):
    try:
        raw = load_raw_yaml(filename)
        body = await request.json()
        provider, model = body.get("provider"), body.get("model")
        if provider not in ("ollama", "cloudru") or not isinstance(model, str) or not model.strip():
            raise ValueError("Укажите провайдера и модель")
        llm = raw.setdefault("llm", {})
        llm["provider"] = provider
        llm["model"] = model.strip()
        if provider == "cloudru":
            llm["base_url"] = "https://foundation-models.api.cloud.ru/v1"
            llm["api_key"] = "ENV:CLOUDRU_FOUNDATION_MODELS_API_KEY"
            llm["max_tokens"] = max(2048, int(llm.get("max_tokens") or 0))
            llm["temperature"] = None
        else:
            llm.pop("api_key", None)
            llm.pop("base_url", None)
            if llm.get("temperature") is None:
                llm["temperature"] = 0.1
        ok, message, _ = validate_yaml_text(yaml.safe_dump(raw, allow_unicode=True))
        if not ok:
            raise ValueError(message)
        write_raw_yaml(filename, raw)
        manager.reload_one(filename)
        return {"ok": True, "provider": provider, "model": model.strip()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)




# -------------------- Auto-rule --------------------

@app.post("/api/autorule/start")
async def api_autorule_start(request: Request, user: str = Depends(verify_auth)):
    body = await request.json()
    filename = (body.get("file") or "").strip()
    folder = (body.get("folder") or "").strip()
    limit = int(body.get("limit") or 25)
    if not filename or not folder:
        return JSONResponse({"ok": False, "error": "Укажите file и folder"}, status_code=400)
    try:
        load_mailbox_config(config_path(filename))
    except Exception as e:
        return JSONResponse({"ok": False, "error": _public_error(e)}, status_code=400)
    job = autorule_manager.start(filename, folder, limit)
    return {"ok": True, "job": job.to_dict()}


@app.get("/api/autorule/{job_id}")
async def api_autorule_status(job_id: str, user: str = Depends(verify_auth)):
    job = autorule_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return {"ok": True, "job": job.to_dict()}


@app.post("/api/autorule/{job_id}/cancel")
async def api_autorule_cancel(job_id: str, user: str = Depends(verify_auth)):
    if not autorule_manager.cancel(job_id):
        raise HTTPException(404, "Задача не найдена")
    return {"ok": True}

@app.get("/api/logs")
async def api_logs(lines: int = 200, user: str = Depends(verify_auth)):
    log_path = BASE_DIR / "logs" / "mailsorter.log"
    for path in list_config_files():
        try:
            cfg = load_mailbox_config(path)
            log_path = Path(cfg.logging.file)
            if not log_path.is_absolute():
                log_path = BASE_DIR / log_path
            break
        except Exception:
            continue
    if not log_path.exists():
        return {"lines": []}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return {"lines": [sanitize_log(line) for line in f.readlines()[-lines:]]}
    except Exception as e:
        return {"lines": [f"Ошибка: {e}"]}
