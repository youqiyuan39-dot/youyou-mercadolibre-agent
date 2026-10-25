"""Limits for automatic originals-only processing; explicit manual generation is separate."""
import json
from pathlib import Path

MIN_IMAGES_PER_SKU = 3  # Existing draft_editor and source_image_screening contract.
MAX_IMAGES_PER_SKU = 20  # Existing editor gallery ceiling.


def policy(root):
    path = Path(root) / 'config/source_image_policy.json'
    value = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    maximum = value.get('max_images_per_sku', 8)
    budget = value.get('image_generation_budget', 0)
    if type(maximum) is not int or not MIN_IMAGES_PER_SKU <= maximum <= MAX_IMAGES_PER_SKU:
        raise ValueError('max_images_per_sku must be an integer between 3 and 20')
    # Fail closed: a positive budget is never permission for this automatic chain.
    if type(budget) not in (int, float) or budget != 0:
        raise ValueError('Automatic originals-only processing requires image_generation_budget=0')
    if value.get('site_id', 'MLM') != 'MLM':
        raise ValueError('Automatic originals-only processing supports MLM only')
    return {'site_id': 'MLM', 'max_images_per_sku': maximum, 'image_generation_budget': 0}


def original(image):
    """Reject known generated assets even when a legacy selection says APPROVED."""
    local = str(image.get('local_file') or '').replace('\\', '/').removeprefix('assets/')
    return (image.get('image_type') != 'generated'
            and 'generated' not in str(image.get('source') or '').lower()
            and not local.startswith(('generated/', 'ai-generated/'))
            and image.get('review_status') != 'REJECTED'
            and bool(image.get('local_file') or image.get('remote_url')))


def shortage_items(reports):
    return [{'sku_id': r['sku_id'], 'status': 'skipped', 'state': 'BLOCKED_EVIDENCE',
             'issues': [{'field': 'sku_images', 'cause': 'IMAGE_COUNT_INSUFFICIENT',
                         'required_evidence': f"该SKU尚缺{r['missing_count']}张合格原图；保留已通过图片，补充原图并审核，不使用AI生图替代"}]}
            for r in reports if r.get('missing_count', 0) > 0]
