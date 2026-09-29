"""Loopback-only receiver for the self-owned 1688 collector extension."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .source_intake import IntakeError, save_collection_and_enqueue
except ImportError:
    from source_intake import IntakeError, save_collection_and_enqueue


def make_handler(session_token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MeikeduoLocalIntake/0.1"

        def _send(self, status: int, value: dict) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            origin = self.headers.get("Origin", "")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin", "")
            self.send_response(204)
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Intake-Token")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "mode": "LOCAL_ONLY"})
            else:
                self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/collect":
                return self._send(404, {"ok": False, "error": "not_found"})
            if not secrets.compare_digest(self.headers.get("X-Local-Intake-Token", ""), session_token):
                return self._send(401, {"ok": False, "error": "invalid_session_token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise IntakeError("采集数据大小不正确")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = save_collection_and_enqueue(payload)
                self._send(
                    201,
                    {
                        "ok": True,
                        "saved": str(result["intake"]),
                        "draft": str(result["draft"]),
                        "board": str(result["board"]),
                        "draft_created": result["draft_created"],
                        "status": result["queue_status"],
                        "validation_stage": result["validation_stage"],
                        "next_action": result["next_action"],
                        "image_counts": result["image_counts"],
                    },
                )
            except (IntakeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1688 本机采集接收器")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--copy-token", action="store_true", help="将本次连接码复制到本机剪贴板")
    args = parser.parse_args(argv)
    session_token = secrets.token_urlsafe(24)
    if args.copy_token:
        subprocess.run(["clip.exe"], input=session_token, text=True, check=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(session_token))
    token_status = "COPIED_TO_CLIPBOARD" if args.copy_token else session_token
    print(json.dumps({"ok": True, "url": f"http://127.0.0.1:{args.port}", "session_token": token_status}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
