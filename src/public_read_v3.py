"""Bounded TLS-verified fallback for intermittent Windows public-read handshakes."""
import json
import gzip
import io
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

VERSION = 'PUBLIC_READ_GZIP_20261018_V2'


def _decoded_public_body(data, limit):
    # Some public category responses (including the curl fallback) arrive as
    # gzip bytes. Bound the decoded body as well as the wire response.
    if data.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(limit + 1)
        except (OSError, EOFError) as exc:
            raise OSError('公开资料压缩响应不完整，未当作商品缺项') from exc
    if len(data) > limit:
        raise OSError('公开资料解压后超过读取预算')
    return data

def read(url, headers=None, limit=10_000_001):
    request = urllib.request.Request(url, headers=headers or {}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return _decoded_public_body(response.read(limit), limit), response.headers.get_content_type()
    except urllib.error.HTTPError:
        raise  # HTTP authorization/rate-limit errors must not be retried via another transport.
    except urllib.error.URLError:
        host = urllib.parse.urlparse(url).hostname or ''
        if urllib.parse.urlparse(url).scheme != 'https' or not (host == 'api.mercadolibre.com' or host.endswith('.alicdn.com')):
            raise
        exe = shutil.which('curl.exe')
        if not exe:
            raise
        args = [exe, '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--proto-redir', '=https', '--max-time', '20', '--max-filesize', str(limit)]
        for key, value in (headers or {}).items():
            if key.lower() in ('accept', 'user-agent'):
                args.extend(['--header', str(key)+': '+str(value)])
        result = subprocess.run(args + [url], capture_output=True, timeout=23,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or len(result.stdout) > limit:
            raise OSError('公开资料下载失败（TLS校验保持启用）')
        data = _decoded_public_body(result.stdout, limit)
        mime = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg' if data.startswith(b'\xff\xd8') else 'image/webp' if data[:4] == b'RIFF' else 'application/json'
        return data, mime

def read_json(url):
    return json.loads(read(url, {'Accept':'application/json'}, limit=3_000_000)[0])
