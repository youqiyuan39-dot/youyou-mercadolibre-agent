"""Recover the original of a known Alibaba thumbnail; never invent another angle."""
import copy
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = 'SAME_ASSET_RESOLUTION_20261018_V1'
LIMIT = 10_000_000


def original_url(url):
    value = urlparse(str(url or ''))
    host = (value.hostname or '').lower()
    if value.scheme != 'https' or not host.endswith('.alicdn.com') or value.query or value.fragment:
        return None
    # Only a documented, observed thumbnail suffix; no generic URL/path guessing.
    if not value.path.endswith('.jpg_sum.jpg'):
        return None
    return urlunparse(value._replace(path=value.path[:-8]))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Image recovery does not follow redirects')


def download(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://detail.1688.com/', 'Accept': 'image/*'})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        if not response.headers.get_content_type().startswith('image/'):
            raise ValueError('Response is not an image')
        data = response.read(LIMIT + 1)
    if not data or len(data) > LIMIT:
        raise ValueError('Image is empty or exceeds 10 MB')
    return data


def _pixels(data, Image):
    if not data or len(data) > LIMIT:
        raise ValueError('Image exceeds byte limit')
    with Image.open(io.BytesIO(data)) as test:
        if test.width * test.height > 24_000_000 or test.width < 1 or test.height < 1:
            raise ValueError('Image exceeds pixel limit')
        test.verify()
    with Image.open(io.BytesIO(data)) as value:
        value.load()
        return value.convert('RGB')


def recover_image(root, image, fetch=download):
    """Return a copied candidate and trace. Raw galleries and human choices stay unchanged."""
    result = copy.deepcopy(image)
    url = str(image.get('remote_url') or '')
    candidate = original_url(url)
    report = {'version': VERSION, 'original_url': url, 'candidate_url': candidate,
              'status': 'NOT_APPLICABLE', 'network_calls': 0, 'sku_id': image.get('sku_id')}
    if not candidate or image.get('local_file') or image.get('review_status') in ('HUMAN_SELECTED', 'APPROVED'):
        return result, report
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        report.update(status='TOOL_UNAVAILABLE', reason='Pillow image verifier is unavailable')
        return result, report
    root = Path(root)
    key = hashlib.sha256(url.encode()).hexdigest()
    folder = root / 'assets/resolved-source'
    manifest = folder / (key + '.json')
    try:
        if manifest.is_file():
            cached = json.loads(manifest.read_text('utf-8'))
            if cached.get('version') == VERSION and cached.get('original_url') == url:
                if cached.get('status') == 'RECOVERED':
                    local = (root / 'assets' / str(cached.get('local_file') or '')).resolve()
                    if local.is_relative_to((root / 'assets').resolve()) and local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == cached.get('sha256'):
                        result['local_file'] = cached['local_file']
                        result['source_resolution'] = {**cached, 'cached': True}
                        return result, {**cached, 'cached': True, 'network_calls': 0}
                elif 0 <= time.time() - cached.get('checked_epoch', 0) < 3600:
                    return result, {**cached, 'cached': True, 'network_calls': 0}
        paths = list((root / 'assets/remote-cache').glob(key + '.*'))
        if paths:
            thumb_data = paths[0].read_bytes()
        else:
            report['network_calls'] += 1
            thumb_data = fetch(url)
        thumb = _pixels(thumb_data, Image)
        report['network_calls'] += 1
        data = fetch(candidate)
        high = _pixels(data, Image)
        report.update(before_dimensions=list(thumb.size), after_dimensions=list(high.size),
            original_sha256=hashlib.sha256(thumb_data).hexdigest(), sha256=hashlib.sha256(data).hexdigest())
        if high.width <= thumb.width or high.height <= thumb.height or min(high.size) < 300:
            raise ValueError('Original is not a useful larger image')
        if abs(high.width / high.height - thumb.width / thumb.height) > .05:
            raise ValueError('Original aspect ratio differs from the thumbnail')
        resized = high.resize(thumb.size, Image.Resampling.LANCZOS)
        error = sum(ImageStat.Stat(ImageChops.difference(thumb, resized)).mean) / 3 / 255
        report['thumbnail_pixel_mae'] = round(error, 6)
        if error > .065:
            raise ValueError('Larger image does not match the source thumbnail')
        suffix = '.png' if data.startswith(b'\x89PNG') else '.webp' if data[:4] == b'RIFF' else '.jpg'
        local = folder / (report['sha256'] + suffix)
        folder.mkdir(parents=True, exist_ok=True)
        if not local.exists():
            temporary = local.with_suffix(local.suffix + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(local)
        report.update(status='RECOVERED', local_file=local.relative_to(root / 'assets').as_posix(),
            identity_check='SAME_HOST_PATH_WITHOUT_SUM_SUFFIX_AND_THUMBNAIL_PIXEL_MATCH')
        result['local_file'] = report['local_file']
        result['source_resolution'] = copy.deepcopy(report)
    except Exception as exc:
        report.update(status='RETAINED_ORIGINAL', reason=str(exc)[:240])
    report['checked_epoch'] = time.time()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(manifest)
    except OSError:
        pass  # Recovery metadata is optional; failure never deletes the source.
    return result, report
