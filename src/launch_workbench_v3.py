"""Start one hidden local server, verify its identity, then open its workbench."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8790"


def identity():
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as r:
            return json.load(r)
    except Exception:
        return None


def main():
    current = identity()
    if current:
        if current.get("version") != "0.3.0-evidence" or Path(current.get("workspace", "")).resolve() != ROOT.resolve():
            raise RuntimeError("8790端口由其他程序或其他工作区占用，未打开错误实例")
        webbrowser.open(URL)
        return
    logs = ROOT / "data" / "v3"
    logs.mkdir(parents=True, exist_ok=True)
    out = (logs / "server.stdout.log").open("ab")
    err = (logs / "server.stderr.log").open("ab")
    process = subprocess.Popen([sys.executable, "-m", "src.workbench_v3", "--port", "8790"], cwd=ROOT, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    out.close(); err.close()
    for _ in range(30):
        current = identity()
        if current and current.get("version") == "0.3.0-evidence":
            webbrowser.open(URL)
            return
        if process.poll() is not None: raise RuntimeError("启动失败，请查看 data/v3/server.stderr.log")
        time.sleep(.3)
    raise RuntimeError("启动超过等待时间，未打开页面")


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "悠悠 Agent 启动失败", 16)
        else: print(str(exc), file=sys.stderr)
        raise SystemExit(1)
