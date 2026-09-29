"""Mercado Libre Global Selling OAuth helper.

Secrets and tokens never appear in logs. Token files are encrypted with the
current Windows user's DPAPI key and cannot be decrypted by another account.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


AUTH_URL = "https://global-selling.mercadolibre.com/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / ".secrets"
PENDING_FILE = SECRET_DIR / "oauth_pending.dat"
TOKEN_FILE = SECRET_DIR / "mercadolibre_tokens.dat"


class OAuthError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _require_windows() -> None:
    if os.name != "nt":
        raise OAuthError("令牌保险箱目前只支持 Windows DPAPI。")


def _protect(data: bytes) -> bytes:
    _require_windows()
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Chanwan Mercado Automation",
        None,
        None,
        None,
        1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    _require_windows()
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 1, ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(base64.b64encode(_protect(raw)))


def load_private_json(path: Path) -> dict:
    encrypted = base64.b64decode(path.read_bytes(), validate=True)
    return json.loads(_unprotect(encrypted).decode("utf-8"))


def create_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    *,
    pending_file: Path = PENDING_FILE,
    context: dict | None = None,
) -> str:
    verifier, challenge = create_pkce_pair()
    state = secrets.token_urlsafe(32)
    # Each store authorization owns a separate pending record.  A single
    # global pending file makes a second browser tab overwrite the first PKCE
    # verifier and causes a misleading state-validation failure on callback.
    save_private_json(pending_file, {
        "state": state,
        "code_verifier": verifier,
        "context": context or {},
    })
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTH_URL}?{query}"


def _post_form(url: str, fields: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = str(payload.get("error", "")).strip()
            description = str(payload.get("message") or payload.get("error_description") or "").strip()
            detail = ": ".join(part for part in (error, description) if part)
        except Exception:
            pass
        suffix = f"（{detail}）" if detail else ""
        raise OAuthError(f"美客多授权失败，HTTP {exc.code}{suffix}。") from exc
    except urllib.error.URLError as exc:
        raise OAuthError("无法连接美客多授权服务器。") from exc


def exchange_code(
    code: str,
    state: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    pending_file: Path = PENDING_FILE,
    token_file: Path = TOKEN_FILE,
) -> None:
    pending = load_private_json(pending_file)
    if not secrets.compare_digest(state, pending["state"]):
        raise OAuthError("state 校验失败，已拒绝本次回调。")
    fields = {
        "grant_type": "authorization_code",
        "client_id": client_id or require_env("ML_CLIENT_ID"),
        "client_secret": client_secret or require_env("ML_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": redirect_uri or require_env("ML_REDIRECT_URI"),
        "code_verifier": pending["code_verifier"],
    }
    tokens = _post_form(TOKEN_URL, fields)
    save_private_json(token_file, tokens)
    pending_file.unlink(missing_ok=True)


def refresh_tokens(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_file: Path = TOKEN_FILE,
) -> None:
    current = load_private_json(token_file)
    fields = {
        "grant_type": "refresh_token",
        "client_id": client_id or require_env("ML_CLIENT_ID"),
        "client_secret": client_secret or require_env("ML_CLIENT_SECRET"),
        "refresh_token": current["refresh_token"],
    }
    replacement = _post_form(TOKEN_URL, fields)
    save_private_json(token_file, replacement)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise OAuthError(f"缺少环境变量 {name}。")
    return value


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: HTTPStatus, message: str) -> None:
        body = ("<!doctype html><meta charset='utf-8'><title>美客多授权</title>"
                f"<h2>{message}</h2><p>可以关闭此页面。</p>").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._reply(HTTPStatus.OK, "服务正常")
            return
        if parsed.path != "/oauth/mercadolibre/callback":
            self._reply(HTTPStatus.NOT_FOUND, "地址不存在")
            return
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        if not code or not state:
            self._reply(HTTPStatus.BAD_REQUEST, "授权参数不完整")
            return
        try:
            exchange_code(code, state)
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: {exc}"
            print(f"回调处理失败：{diagnostic}", flush=True)
            log_dir = ROOT / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "oauth_last_error.txt").write_text(diagnostic, encoding="utf-8")
            self._reply(HTTPStatus.BAD_REQUEST, f"授权失败：{diagnostic}")
            return
        self._reply(HTTPStatus.OK, "授权成功，令牌已加密保存在本机")


def serve() -> None:
    host = os.environ.get("ML_CALLBACK_HOST", "127.0.0.1")
    port = int(os.environ.get("ML_CALLBACK_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), CallbackHandler)
    print(f"本机回调服务已启动：http://{host}:{port}")
    print("请确保外部 HTTPS 地址已安全转发到本机，并与应用设置完全一致。")
    server.serve_forever()


def main() -> int:
    try:
        command = sys.argv[1] if len(sys.argv) > 1 else ""
        if command == "authorize-url":
            print(build_authorization_url(require_env("ML_CLIENT_ID"), require_env("ML_REDIRECT_URI")))
        elif command == "serve":
            serve()
        elif command == "refresh":
            refresh_tokens()
            print("令牌已刷新并加密保存。")
        else:
            print("用法：python mercadolibre_oauth.py authorize-url|serve|refresh")
            return 2
        return 0
    except (OAuthError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
