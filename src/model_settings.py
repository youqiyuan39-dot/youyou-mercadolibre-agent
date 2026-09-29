"""Local AI gateway settings with DPAPI protected API keys."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .mercadolibre_oauth import load_private_json, save_private_json
except ImportError:
    from mercadolibre_oauth import load_private_json, save_private_json


ROLE_DEFAULTS = {
    "vision": {"label": "多模态商品识别", "base_url": "https://suoxie.codes/v1", "model": "gpt-5.5"},
    "images": {"label": "商品生图", "base_url": "https://suoxie.codes/v1/images/edits/async", "model": "gpt-image-2"},
    "attributes": {"label": "必填属性补全", "base_url": "https://suoxie.codes/v1", "model": "gpt-5.5"},
}


class ModelSettingsError(ValueError):
    pass


def _paths(root: Path) -> tuple[Path, Path]:
    return root / "config" / "model_settings.json", root / ".secrets" / "model_api_keys.dat"


def _validate_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ModelSettingsError("API 地址必须是有效的 HTTPS 地址。")
    return url


def _load_public(root: Path) -> dict[str, Any]:
    public_path, _ = _paths(root)
    if public_path.is_file():
        value = json.loads(public_path.read_text(encoding="utf-8"))
    else:
        value = {}
    roles = value.get("roles") if isinstance(value, dict) else {}
    strategy = value.get("strategy") if isinstance(value, dict) else {}
    return {"roles": roles if isinstance(roles, dict) else {}, "strategy": strategy if isinstance(strategy, dict) else {}}


def _load_keys(root: Path) -> dict[str, str]:
    _, secret_path = _paths(root)
    if not secret_path.is_file():
        return {}
    value = load_private_json(secret_path)
    return {key: str(item) for key, item in value.items() if key in ROLE_DEFAULTS and str(item).strip()}


def get_public_settings(root: Path) -> dict[str, Any]:
    stored = _load_public(root)
    try:
        keys = _load_keys(root)
        vault_error = None
    except Exception:
        keys, vault_error = {}, "本机密钥保险箱无法读取，请在当前 Windows 用户下重新保存。"
    roles = {}
    for role, defaults in ROLE_DEFAULTS.items():
        current = stored["roles"].get(role) if isinstance(stored["roles"].get(role), dict) else {}
        roles[role] = {
            "label": defaults["label"],
            "base_url": str(current.get("base_url") or defaults["base_url"]),
            "model": str(current.get("model") or defaults["model"]),
            "has_key": bool(keys.get(role)),
        }
    strategy = {
        "image_language": str(stored["strategy"].get("image_language") or "auto"),
        "reference_image_count": int(stored["strategy"].get("reference_image_count") or 3),
        "images_per_product": int(stored["strategy"].get("images_per_product") or 3),
        "generate_per_sku": stored["strategy"].get("generate_per_sku") is not False,
        "auto_process_after_collection": bool(stored["strategy"].get("auto_process_after_collection", False)),
    }
    return {"roles": roles, "strategy": strategy, "vault_error": vault_error}


def save_model_settings(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    public_path, secret_path = _paths(root)
    current = get_public_settings(root)
    try:
        keys = _load_keys(root)
    except Exception:
        keys = {}
    incoming_roles = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
    roles = {}
    for role, defaults in ROLE_DEFAULTS.items():
        incoming = incoming_roles.get(role) if isinstance(incoming_roles.get(role), dict) else {}
        roles[role] = {
            "base_url": _validate_url(incoming.get("base_url") or current["roles"][role]["base_url"]),
            "model": str(incoming.get("model") or current["roles"][role]["model"]).strip()[:120],
        }
        if not roles[role]["model"]:
            raise ModelSettingsError(f"{defaults['label']}缺少模型名称。")
        new_key = str(incoming.get("api_key") or "").strip()
        if new_key:
            keys[role] = new_key
    strategy_in = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    language = str(strategy_in.get("image_language") or current["strategy"]["image_language"])
    if language not in {"auto", "none", "es", "pt"}:
        raise ModelSettingsError("图片文字语言不正确。")
    strategy = {
        "image_language": language,
        "reference_image_count": max(1, min(10, int(strategy_in.get("reference_image_count") or current["strategy"]["reference_image_count"]))),
        "images_per_product": 3,
        "generate_per_sku": strategy_in.get("generate_per_sku", current["strategy"]["generate_per_sku"]) is not False,
        "auto_process_after_collection": bool(strategy_in.get("auto_process_after_collection", current["strategy"]["auto_process_after_collection"])),
    }
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(json.dumps({"roles": roles, "strategy": strategy}, ensure_ascii=False, indent=2), encoding="utf-8")
    if keys:
        save_private_json(secret_path, keys)
    return get_public_settings(root)


def get_api_key(root: Path, role: str) -> str:
    return _load_keys(root).get(role, "")


def get_runtime_role(root: Path, role: str) -> dict[str, Any]:
    public = get_public_settings(root)
    if role not in public["roles"]:
        raise ModelSettingsError("未知的模型用途。")
    result = dict(public["roles"][role])
    result["api_key"] = get_api_key(root, role)
    return result


def test_connection(root: Path, role: str) -> dict[str, Any]:
    config = get_runtime_role(root, role)
    if not config["api_key"]:
        raise ModelSettingsError("尚未保存 API Key。")
    endpoint = config["base_url"]
    parsed = urllib.parse.urlparse(endpoint)
    probe = f"{parsed.scheme}://{parsed.netloc}/v1/models"
    request = urllib.request.Request(probe, headers={
        "Authorization": f"Bearer {config['api_key']}", "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {"connected": 200 <= response.status < 300, "http_status": response.status, "probe": "/v1/models"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ModelSettingsError(f"模型服务返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise ModelSettingsError("无法连接模型服务。") from exc
