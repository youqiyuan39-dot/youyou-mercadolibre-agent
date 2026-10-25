"""Loopback collector receiver shared by standalone and workbench startup."""
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from .draft_editor import make_handler
from .source_intake import ROOT


def make_collector_handler(root=ROOT):
    root = Path(root).resolve()
    base = make_handler(root)
    from .draft_editor import YOUYOU_EXTENSION_ORIGIN
    allowed = {YOUYOU_EXTENSION_ORIGIN}
    config = root / 'config/collector_allowed_origins.json'
    if config.is_file():
        import re
        configured = json.loads(config.read_text(encoding='utf-8'))
        for value in configured.get('allowed_origins', []):
            if isinstance(value, str) and re.fullmatch(r'chrome-extension://[a-p]{32}', value):
                allowed.add(value)

    class Handler(base):
        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            origin = getattr(self, '_collector_origin', self.headers.get('Origin'))
            if origin in allowed:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
                self.send_header('Access-Control-Allow-Private-Network', 'true')
            super().end_headers()

        def do_GET(self):
            if self.path == "/api/health":
                return self._json(200, {"ok": True, "mode": "COLLECTOR_ONLY", "workspace": str(root), "receiver_version":"COLLECTOR_20261022_SLICE10", "receiver_loaded_from":__file__, "supported_operations":["zying_detail","offer_candidates_1688","zying_dom_snapshot"], "allowed_origins":sorted(allowed)})
            return self._json(404, {"ok": False, "error": "collector_only"})

        def do_POST(self):
            if self.path == '/api/market-reference':
                if self.headers.get('X-Youyou-Collector')!='1' or (self.headers.get('Origin') not in allowed and self.headers.get('Origin') is not None):
                    return self._json(403,{'ok':False,'error':'extension_not_allowed'})
                try:
                    size=int(self.headers.get('Content-Length') or 0)
                    if not 0<size<=131072:return self._json(400,{'ok':False,'step':'request_size','error':'请求体过大或为空（最大128KiB）'})
                    value = json.loads(self.rfile.read(size))
                    if not isinstance(value, dict):
                        raise ValueError('参考资料必须是 JSON 对象')
                    if value.get('operation') == 'offer_candidates_1688':
                        from .offer_candidates_1688_v3 import save
                        return self._json(200, save(root, value.get('detail')))
                    if size > 32768:
                        return self._json(400,{'ok':False,'step':'request_size','error':'请求体过大（当前operation最大32KiB或12000字节）'})
                    if value.get('operation') == 'zying_dom_snapshot':
                        from .zying_detail_v3 import save_dom_snapshot
                        return self._json(200,save_dom_snapshot(root,value.get('detail')))
                    if isinstance(value, dict) and value.get('operation') == 'zying_detail':
                        from .zying_detail_v3 import save
                        return self._json(200, save(root, value.get('detail')))
                    if size > 12000:
                        return self._json(400,{'ok':False,'step':'request_size','error':'请求体过大（当前operation最大32KiB或12000字节）'})
                    from .market_context_v3 import handle
                    return self._json(200,handle(root,value))
                except (ValueError,TypeError,OSError) as exc:return self._json(400,{'ok':False,'error':str(exc)[:300]})
            if self.path != "/api/collect":
                return self._json(404, {"ok": False, "error": "collector_only"})
            # Preserve the parent collect route and its fixed production-origin check.
            # Only this collector subclass admits explicitly configured extension IDs.
            origin = self.headers.get('Origin')
            if origin not in allowed:
                return self._json(403, {'ok':False,'error':'extension_not_allowed'})
            self._collector_origin = origin
            self.headers.replace_header('Origin', YOUYOU_EXTENSION_ORIGIN)
            try:
                return super().do_POST()
            finally:
                self.headers.replace_header('Origin', origin)
                del self._collector_origin

        def do_OPTIONS(self):
            if self.path not in ("/api/collect",'/api/market-reference'):
                return self._json(404, {"ok": False, "error": "collector_only"})
            origin = self.headers.get('Origin')
            if origin not in allowed:
                return self._json(403, {'ok':False,'error':'extension_not_allowed: 扩展ID不在白名单'})
            self.send_response(204)
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Youyou-Collector')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.end_headers()

    return Handler


class CollectorServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def receiver_health(root=ROOT, port=8789):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            value = json.loads(response.read(8192))
        return (value.get("ok") is True and value.get("mode") == "COLLECTOR_ONLY"
                and Path(value.get("workspace", "")).resolve() == Path(root).resolve())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def ensure_receiver(root=ROOT, port=8789):
    """Return (owned server or None, status); never take over a foreign port."""
    try:
        server = CollectorServer(("127.0.0.1", port), make_collector_handler(root))
    except OSError:
        if receiver_health(root, port):
            return None, {"ok": True, "state": "REUSED", "port": port}
        return None, {"ok": False, "state": "PORT_UNAVAILABLE", "port": port}
    thread = threading.Thread(target=server.serve_forever, name="collector-receiver", daemon=True)
    try:
        thread.start()
    except Exception:
        server.server_close()
        raise
    return server, {"ok": True, "state": "STARTED", "port": server.server_port}


if __name__ == "__main__":
    server = CollectorServer(("127.0.0.1", 8789), make_collector_handler())
    print("Collector-only receiver ready on 127.0.0.1:8789", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
