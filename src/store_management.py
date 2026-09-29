"""Safe local store-management facade for Mercado Libre OAuth.

Only non-secret account metadata is returned to the browser. OAuth tokens stay
inside the Windows DPAPI encrypted token vault.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import mercadolibre_oauth as oauth
    from .mercadolibre_client import MercadoLibreAPIError, MercadoLibreClient, account_probe, write_json
except ImportError:
    import mercadolibre_oauth as oauth
    from mercadolibre_client import MercadoLibreAPIError, MercadoLibreClient, account_probe, write_json


SUPPORTED_AUTH_TYPES = {"CBT", "MLM", "MLB", "MLC"}


def _metadata_path(root: Path) -> Path:
    return root / "config" / "mercadolibre_store.json"


def _stores_path(root: Path) -> Path:
    return root / "config" / "mercadolibre_stores.json"


def _token_path(root: Path, store_id: str) -> Path:
    if store_id == "legacy":
        return root / ".secrets" / oauth.TOKEN_FILE.name
    return root / ".secrets" / f"mercadolibre_tokens_{store_id}.dat"


def _stores(root: Path) -> dict[str, Any]:
    path = _stores_path(root)
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("stores"), list):
                return value
        except (OSError, ValueError):
            pass
    legacy = _safe_metadata(root)
    return {"active_store_id": "legacy", "stores": ([{"id": "legacy", **legacy}] if legacy else [])}


def _save_stores(root: Path, value: dict[str, Any]) -> None:
    write_json(_stores_path(root), value)


def _public_store(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    store_id = str(row.get("id") or "legacy")
    token_saved = _token_path(root, store_id).is_file()
    return {
        "id": store_id,
        "alias": str(row.get("alias") or "Mercado Libre Store")[:80],
        "auth_type": str(row.get("auth_type") or "CBT").upper(),
        "connected_at": str(row.get("connected_at") or "")[:80],
        "merchant_id": row.get("merchant_id"),
        "nickname": str(row.get("nickname") or "")[:100],
        "token_saved": token_saved,
        "status": "SAVED_UNVERIFIED" if token_saved else "NOT_AUTHORIZED",
        "status_text": "已保存令牌，尚未实时验证" if token_saved else "等待授权",
    }


def _oauth_config_path(root: Path) -> Path:
    return root / ".secrets" / "mercadolibre_oauth_config.dat"


def _load_oauth_config(root: Path) -> dict[str, str]:
    path = _oauth_config_path(root)
    if not path.is_file():
        return {}
    value = oauth.load_private_json(path)
    return {key: str(value.get(key) or "").strip() for key in ("client_id", "client_secret", "redirect_uri")}


def get_runtime_oauth_config(root: Path) -> dict[str, str]:
    stored = _load_oauth_config(root)
    return {
        "client_id": stored.get("client_id") or os.environ.get("ML_CLIENT_ID", "").strip(),
        "client_secret": stored.get("client_secret") or os.environ.get("ML_CLIENT_SECRET", "").strip(),
        "redirect_uri": stored.get("redirect_uri") or os.environ.get("ML_REDIRECT_URI", "").strip(),
    }


def _safe_metadata(root: Path) -> dict[str, Any]:
    path = _metadata_path(root)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        "alias": str(value.get("alias") or "Mercado Libre Store")[:80],
        "auth_type": str(value.get("auth_type") or "CBT").upper(),
        "connected_at": str(value.get("connected_at") or "")[:80],
        "merchant_id": value.get("merchant_id"),
        "nickname": str(value.get("nickname") or "")[:100],
    }


def oauth_configuration(root: Path) -> dict[str, Any]:
    config = get_runtime_oauth_config(root)
    labels = {"client_id": "Client ID", "client_secret": "Client Secret", "redirect_uri": "Redirect URI"}
    missing = [labels[key] for key, value in config.items() if not value]
    return {
        "complete": not missing,
        "missing": missing,
        "has_client_id": bool(config["client_id"]),
        "has_client_secret": bool(config["client_secret"]),
        "redirect_uri": config["redirect_uri"],
    }


def save_oauth_configuration(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        current = _load_oauth_config(root)
    except Exception:
        current = {}
    client_id = str(payload.get("client_id") or "").strip() or current.get("client_id", "")
    client_secret = str(payload.get("client_secret") or "").strip() or current.get("client_secret", "")
    redirect_uri = str(payload.get("redirect_uri") or "").strip() or current.get("redirect_uri", "")
    if not client_id or not client_id.isdigit() or len(client_id) > 40:
        raise ValueError("Client ID 应为美客多开发者应用中的数字 ID")
    if not client_secret or len(client_secret) > 300:
        raise ValueError("Client Secret 未填写或格式不正确")
    parsed = urllib.parse.urlparse(redirect_uri)
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if not parsed.netloc or parsed.username or parsed.password or (parsed.scheme != "https" and not loopback):
        raise ValueError("Redirect URI 必须是 HTTPS 地址，或本机 localhost 回调地址")
    if not parsed.path.endswith("/oauth/mercadolibre/callback"):
        raise ValueError("Redirect URI 路径必须以 /oauth/mercadolibre/callback 结尾")
    oauth.save_private_json(_oauth_config_path(root), {
        "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
    })
    return oauth_configuration(root)


def store_status(root: Path, *, live: bool = False, client: MercadoLibreClient | None = None) -> dict[str, Any]:
    registry = _stores(root)
    rows = [row for row in registry.get("stores", []) if isinstance(row, dict)]
    active_id = str(registry.get("active_store_id") or "legacy")
    meta = next((row for row in rows if str(row.get("id") or "legacy") == active_id), _safe_metadata(root))
    token_path = _token_path(root, active_id)
    configured = oauth_configuration(root)
    result: dict[str, Any] = {
        "alias": meta.get("alias") or "Mercado Libre Store",
        "auth_type": meta.get("auth_type") if meta.get("auth_type") in SUPPORTED_AUTH_TYPES else "CBT",
        "connected_at": meta.get("connected_at") or "",
        "merchant_id": meta.get("merchant_id"),
        "nickname": meta.get("nickname") or "",
        "token_saved": token_path.is_file(),
        "oauth_config": configured,
        "status": "NOT_AUTHORIZED",
        "status_text": "尚未授权",
        "live_checked": False,
        "marketplaces": [],
        "active_store_id": active_id,
        "stores": [_public_store(root, row) for row in rows],
    }
    if not token_path.is_file():
        result["status_text"] = "OAuth 配置不完整" if not configured["complete"] else "等待授权"
        return result
    result.update({"status": "SAVED_UNVERIFIED", "status_text": "已保存令牌，尚未实时验证"})
    if not live:
        return result
    try:
        api = client or MercadoLibreClient()
        me = api.me()
        probe = account_probe(api)
    except (MercadoLibreAPIError, OSError, ValueError, KeyError) as exc:
        result.update({"status": "REAUTH_REQUIRED", "status_text": str(exc), "live_checked": True})
        return result
    merchant_id = me.get("id")
    nickname = str(me.get("nickname") or me.get("email") or "")[:100]
    now = datetime.now(timezone.utc).isoformat()
    saved = {
        "alias": result["alias"], "auth_type": result["auth_type"], "connected_at": result["connected_at"] or now,
        "merchant_id": merchant_id, "nickname": nickname,
    }
    if active_id == "legacy":
        write_json(_metadata_path(root), saved)
    for index, row in enumerate(rows):
        if str(row.get("id") or "legacy") == active_id:
            rows[index] = {"id": active_id, **saved}
            _save_stores(root, {"active_store_id": active_id, "stores": rows})
            break
    result.update({
        **saved,
        "status": "CONNECTED",
        "status_text": "已连接",
        "live_checked": True,
        "sell_allowed": probe.get("sell_allowed"),
        "business_model": probe.get("business_model"),
        "marketplaces": probe.get("marketplaces") or [],
    })
    return result


def save_store_preferences(root: Path, alias: str, auth_type: str) -> dict[str, Any]:
    clean_alias = " ".join(str(alias or "Mercado Libre Store").split())[:80]
    clean_type = str(auth_type or "CBT").upper()
    if clean_type not in SUPPORTED_AUTH_TYPES:
        raise ValueError("授权类型不支持")
    current = _safe_metadata(root)
    current.update({"alias": clean_alias or "Mercado Libre Store", "auth_type": clean_type})
    write_json(_metadata_path(root), current)
    return store_status(root)


def set_active_store(root: Path, store_id: str) -> dict[str, Any]:
    registry = _stores(root)
    rows = [row for row in registry.get("stores", []) if isinstance(row, dict)]
    if store_id not in {str(row.get("id") or "legacy") for row in rows}:
        raise ValueError("未找到要切换的店铺")
    if not _token_path(root, store_id).is_file():
        raise ValueError("该店铺尚未完成授权，不能设为发布店")
    _save_stores(root, {"active_store_id": store_id, "stores": rows})
    return store_status(root)


def refresh_active_authorization(root: Path) -> dict[str, Any]:
    registry = _stores(root)
    store_id = str(registry.get("active_store_id") or "legacy")
    config = get_runtime_oauth_config(root)
    if not all(config.values()):
        raise ValueError("OAuth 配置不完整，无法刷新令牌")
    try:
        oauth.refresh_tokens(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            token_file=_token_path(root, store_id),
        )
    except (KeyError, oauth.OAuthError, OSError, ValueError) as exc:
        raise ValueError(f"令牌自动续期失败，需要重新授权：{exc}") from exc
    return store_status(root, live=True)


def authorization_url(root: Path, alias: str, auth_type: str, store_id: str = "") -> dict[str, Any]:
    clean_alias = " ".join(str(alias or "").split())[:80]
    clean_type = str(auth_type or "CBT").upper()
    if not clean_alias:
        raise ValueError("请先填写新店铺备注，用于区分各店铺")
    if clean_type not in SUPPORTED_AUTH_TYPES:
        raise ValueError("授权类型不支持")
    public_config = oauth_configuration(root)
    if not public_config["complete"]:
        raise ValueError("OAuth 配置不完整：" + "、".join(public_config["missing"]))
    config = get_runtime_oauth_config(root)
    registry = _stores(root)
    known_ids = {str(row.get("id") or "legacy") for row in registry.get("stores", []) if isinstance(row, dict)}
    store_id = store_id if store_id in known_ids else uuid.uuid4().hex
    pending_file = root / ".secrets" / f"oauth_pending_{store_id}.dat"
    return {
        "url": oauth.build_authorization_url(
            config["client_id"], config["redirect_uri"], pending_file=pending_file,
            context={"store_id": store_id, "alias": clean_alias, "auth_type": clean_type},
        ),
        "publishing_performed": False,
    }


def _pending_for_state(root: Path, state: str) -> tuple[Path, dict[str, Any]]:
    candidates = [root / ".secrets" / oauth.PENDING_FILE.name]
    candidates.extend((root / ".secrets").glob("oauth_pending_*.dat"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = oauth.load_private_json(path)
        except Exception:
            continue
        if secrets.compare_digest(str(value.get("state") or ""), state):
            return path, value
    raise oauth.OAuthError("state 校验失败或本次授权链接已过期，请回到店铺管理重新发起授权。")


def complete_authorization(root: Path, code: str, state: str) -> None:
    config = get_runtime_oauth_config(root)
    if not all(config.values()):
        raise ValueError("OAuth 配置不完整，无法完成授权回调")
    pending_file, pending = _pending_for_state(root, state)
    context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
    store_id = str(context.get("store_id") or "legacy")
    oauth.exchange_code(
        code,
        state,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["redirect_uri"],
        pending_file=pending_file,
        token_file=_token_path(root, store_id),
    )
    if store_id == "legacy":
        return
    registry = _stores(root)
    rows = [row for row in registry.get("stores", []) if isinstance(row, dict)]
    replacement = {
        "id": store_id,
        "alias": str(context.get("alias") or "Mercado Libre Store")[:80],
        "auth_type": str(context.get("auth_type") or "CBT").upper(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    for index, row in enumerate(rows):
        if str(row.get("id") or "legacy") == store_id:
            rows[index] = replacement
            break
    else:
        rows.append(replacement)
    _save_stores(root, {"active_store_id": registry.get("active_store_id") or "legacy", "stores": rows})
