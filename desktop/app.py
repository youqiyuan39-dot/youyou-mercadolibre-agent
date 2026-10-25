"""Windows desktop entry; no credentials or product data are bundled."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import http.client
import faulthandler

TITLE = '悠悠 Agent 工作台'
URL = 'http://127.0.0.1:8790'
CLIENT_VERSION = 'NATIVE_CLIENT_20261023_V5'
HEALTH_ERROR = None


def run_window(root, backend):
    """Create controls on the GUI thread; retain diagnostics without user data."""
    import webview
    logs = root / 'data/v3'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f'desktop-client-{os.getpid()}.log').open('a', encoding='utf-8') as trace:
        def event(name):
            trace.write(json.dumps({'event': name, 'version': CLIENT_VERSION,
                                    'pid': os.getpid(), 'at': time.time()}) + '\n')
            trace.flush()
        event('gui_start')
        faulthandler.enable(file=trace)
        faulthandler.dump_traceback_later(20, file=trace)
        try:
            window = webview.create_window(TITLE, URL, width=1360, height=900,
                    min_size=(1024, 700), background_color='#f5f6fb')
            window.events.shown += lambda: event('shown')
            def loaded():
                faulthandler.cancel_dump_traceback_later()
                event('loaded')
            window.events.loaded += loaded
            window.events.closed += backend.close
            icon = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'yoyo.ico'
            # WinForms consumes this inside BrowserForm construction. Do not
            # assign window.native.Icon from an asynchronous shown callback.
            webview.start(gui='edgechromium', debug=False, private_mode=False,
                    icon=str(icon) if icon.is_file() else None,
                    storage_path=str(root / 'data/v3/desktop-webview-v2'))
            event('gui_exit')
        finally:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()


def workspace():
    location = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    for directory in [location, *location.parents]:
        if (directory / 'src/workbench_v3.py').is_file() and (directory / 'web/workbench_v3.html').is_file():
            return directory
    raise RuntimeError('请将桌面程序保留在原工作区内；未找到Agent项目。')


def health():
    global HEALTH_ERROR
    connection = None
    try:
        # This endpoint is fixed plain HTTP loopback. Constructing urllib's
        # default handlers also initializes HTTPS/certificate machinery, which
        # can fail in a frozen client before any local request is even made.
        connection = http.client.HTTPConnection('127.0.0.1', 8790, timeout=3)
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        if response.status != 200:
            HEALTH_ERROR = {'type': 'HTTPError', 'http_status': response.status}
            return None
        result = json.loads(response.read(100000).decode('utf-8'))
        HEALTH_ERROR = None
        return result
    except Exception as exc:
        HEALTH_ERROR = {'type': type(exc).__name__, 'module': getattr(exc, 'name', None),
                        'errno': getattr(exc, 'errno', None), 'http_status': getattr(exc, 'code', None)}
        return None
    finally:
        if connection is not None:
            connection.close()


def validate_identity(state, root):
    if state and (state.get('version') != '0.3.0-evidence' or Path(state.get('workspace', '')).resolve() != root.resolve()):
        raise RuntimeError('8790端口由其他工作台占用，未连接错误实例。')
    return bool(state)


def ensure_server(root, *, mode='window_bound'):
    from backend_lifecycle import BackendLifetime
    if mode not in ('window_bound','resident'):raise ValueError('Unknown backend lifetime mode')
    state=health()
    if validate_identity(state, root):
        if state.get('desktop_lifecycle')!='WINDOW_BOUND_V1':raise RuntimeError('检测到旧后台；请先退出旧后台一次，再打开新版工作台。')
        return BackendLifetime(root,URL,health,mode=mode)
    # This is an attach-only client mode, not an alternate service launcher.
    # Service deployment/activation must happen through its approved lifecycle.
    if mode=='resident':
        raise RuntimeError('常驻客户端未找到已运行的工作台后台；未启动或修改服务。')
    logs = root / 'data/v3'; logs.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '--serve'] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve()), '--serve']
    with (logs / 'desktop-server.stdout.log').open('ab') as out, (logs / 'desktop-server.stderr.log').open('ab') as err:
        process = subprocess.Popen(command, cwd=root, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(100):
        if validate_identity(health(), root): return BackendLifetime(root,URL,health,process)
        if process.poll() is not None: raise RuntimeError('后台启动失败，请查看data/v3/desktop-server.stderr.log。')
        time.sleep(.2)
    raise RuntimeError('后台启动超时。未提交任务或发布商品。')


def serve(root):
    # Existing files remain the source of truth. Bundled code has no copied keys/data.
    sys.path.insert(0, str(root))
    from src import workbench_v3
    code_root = Path(workbench_v3.__file__).resolve().parents[1]
    for name, module in list(sys.modules.items()):
        if name.startswith('src.') and module:
            for key, value in list(vars(module).items()):
                if isinstance(value, Path):
                    try: setattr(module, key, root / value.relative_to(code_root))
                    except ValueError: pass
    sys.argv = [sys.argv[0], '--port', '8790']
    workbench_v3.main()


def main():
    root = workspace()
    if '--diagnose-health' in sys.argv:
        state = health()
        report = {'version': CLIENT_VERSION, 'ok': bool(state), 'error': HEALTH_ERROR,
                  'backend_pid': (state or {}).get('process_id')}
        destination = root / 'docs/desktop-repair-20261023/frozen-health.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return  # Diagnostic only: no window, server, model or task starts.
    if '--strategy-self-check' in sys.argv:
        sys.path.insert(0,str(root))
        from PIL import Image, __version__ as pillow_version
        from src import product_match_v3, source_image_resolution_v3, sku_processing_v3, source_image_screening_v3
        sample = root/'docs/strategy-loop-20261018/real-b-resolution/assets/resolved-source'
        image = next(sample.glob('*.jpg'), None)
        pixels = None
        if image:
            with Image.open(image) as decoded:
                decoded.load(); pixels = list(decoded.size)
        report = {'frozen':bool(getattr(sys,'frozen',False)), 'executable':sys.executable,
                  'pillow_version':pillow_version, 'decoded_real_sample':pixels,
                  'processing_version':sku_processing_v3.VERSION,
                  'matching_version':product_match_v3.VERSION,
                  'resolution_version':source_image_resolution_v3.VERSION,
                  'screening_version':source_image_screening_v3.VERSION,
                  'screening_loaded_from':source_image_screening_v3.__file__,
                  'processing_loaded_from':sku_processing_v3.__file__}
        destination = root/'docs/strategy-loop-20261018/native-self-check.json'
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return
    if '--self-check' in sys.argv:
        sys.path.insert(0,str(root))
        import html.parser,sqlite3
        from src import pricing_intelligence,project_knowledge_v3
        report={'ok':True,'html_parser_available':True,'sqlite_available':True,'knowledge':project_knowledge_v3.retrieve(root,'利润定价10%')['version'],'frozen':bool(getattr(sys,'frozen',False))}
        destination=root/'docs/flow-repair-20261016/native-self-check.json'
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return
    if '--serve' in sys.argv: return serve(root)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Yoyo.Agent.Workbench')
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    # A repaired attach-only client can coexist with a hung legacy window.
    identity = str(root) + ('|resident-v2' if '--resident' in sys.argv else '')
    mutex = kernel.CreateMutexW(None, False, 'Local\\YoyoAgent-' + hashlib.sha256(identity.encode()).hexdigest()[:20])
    if kernel.GetLastError() == 183:
        existing = ctypes.windll.user32.FindWindowW(None, TITLE)
        if existing:
            ctypes.windll.user32.ShowWindow(existing, 9)
            ctypes.windll.user32.SetForegroundWindow(existing)
        return
    backend=None
    try:
        backend=ensure_server(root,mode='resident' if '--resident' in sys.argv else 'window_bound')
        run_window(root, backend)
    finally:
        if backend is not None:backend.close()
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), TITLE + '｜启动失败', 16)
        raise SystemExit(1)
