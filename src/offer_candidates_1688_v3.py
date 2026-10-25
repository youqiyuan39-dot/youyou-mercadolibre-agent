"""Bounded public DOM intake. No remote transport, draft creation or SKU inference."""
import copy
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .zying_detail_v3 import SAVE_LOCK
from .offer_search_v3 import OfferSearchProvider, prepare

MAX_BODY = 131072
MAX_CANDIDATES = 40


def text(v, n=300):
    # DOM strings are public product fields; discard obvious credential assignments.
    s = str(v or '').strip()
    if re.search(r'(cookie|token|password|authorization|api[_-]?key)\s*[:=]', s, re.I):
        return ''
    return s[:n]


def url(v, kind='page'):
    p = urlsplit(str(v or ''))
    host = p.hostname or ''
    allowed = (host == 'meli.zying.net' or host == '1688.com' or host.endswith('.1688.com'))
    if kind == 'image':
        allowed = allowed or host == 'alicdn.com' or host.endswith('.alicdn.com') or host.endswith('.tbcdn.cn')
    if p.scheme != 'https' or p.username or p.password or not allowed:
        return ''
    return 'https://' + host + p.path[:950]


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('候选采集必须是对象')
    sid = value.get('market_item_id')
    if not isinstance(sid, str) or not re.fullmatch(r'MLM\d{6,20}', sid) or value.get('site_id') != 'MLM':
        raise ValueError('仅支持明确 MLM 商品编号')
    source = url(value.get('source_url'))
    if not source:
        raise ValueError('来源页面不在允许范围')
    rate = value.get('rate_limit')
    if not isinstance(rate, dict) or type(rate.get('daily_count')) is not int or not 1 <= rate['daily_count'] <= 50:
        raise ValueError('每日计数字段无效')
    if type(rate.get('interval_ms')) is not int or rate['interval_ms'] < 5000 or rate.get('daily_limit') != 50:
        raise ValueError('采集间隔字段无效')
    try:
        observed = datetime.fromisoformat(value.get('captured_at', '').replace('Z', '+00:00'))
        if observed.tzinfo is None or observed.date().isoformat() != rate.get('day'):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('采集时间/计数日期无效') from None
    status = value.get('status')
    if status not in ('READY', 'BLOCKED_ON_LOGIN'):
        raise ValueError('候选状态无效')
    rows = value.get('candidates')
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise ValueError('最多40条当前页候选')
    if value.get('page_count') != 1 or type(value.get('page_count')) is not int:
        raise ValueError('只允许当前一页')
    clean = []
    for row in ([] if status == 'BLOCKED_ON_LOGIN' else rows):
        if not isinstance(row, dict):
            raise ValueError('候选结构无效')
        link = url(row.get('offer_url'))
        match = re.fullmatch(r'https://detail\.1688\.com/offer/(\d{6,20})\.html', link)
        if not match or (row.get('offer_id') and str(row['offer_id']) != match[1]):
            raise ValueError('候选offer链接/编号无效')
        clean.append({'offer_id': match[1], 'offer_url': link, 'title': text(row.get('title')),
                      'price': text(row.get('price'), 80), 'currency': 'CNY',
                      'spec_summary': text(row.get('spec_summary')), 'min_order': text(row.get('min_order'), 80),
                      'image_url': url(row.get('image_url'), 'image'), 'shop_name': text(row.get('shop_name'), 120)})
    return {'market_item_id': sid, 'site_id': 'MLM', 'source_url': source, 'captured_at': observed.isoformat(),
            'rate_limit': {k: rate[k] for k in ('daily_count', 'daily_limit', 'interval_ms', 'day')},
            'page_count': 1, 'status': status, 'candidates': clean,
            'diagnostic': 'MANUAL_LOGIN_OR_VERIFICATION_REQUIRED' if status == 'BLOCKED_ON_LOGIN' else '',
            'parser_version': text(value.get('parser_version'), 80),
            'background_extension_version': text(value.get('background_extension_version'),80) or 'UNKNOWN_LEGACY'}


class ZyingDomCandidates(OfferSearchProvider):
    name = 'zying_dom_candidates'

    def __init__(self, payload):
        self.payload = payload

    def search(self, query):
        if self.payload['status'] == 'BLOCKED_ON_LOGIN':
            return {'status': 'BLOCKED_ON_LOGIN', 'reason': '请主人手动完成登录/滑块验证', 'candidates': []}
        candidates = []
        for row in self.payload['candidates']:
            # An offer-level list is not a selected supplier SKU. This identifier is
            # explicitly synthetic; specs/quantity/package stay missing for scoring.
            candidates.append({'capture': {'source': '1688', 'source_url': row['offer_url'],
                'title': row['title'], 'images': [row['image_url']] if row['image_url'] else [],
                'skus': [{'sku_id': 'DOM_OFFER_' + row['offer_id'], 'variant': row['spec_summary']}]},
                'sku_identity': {}})
        return {'status': 'READY', 'candidates': candidates}


def evaluate(root, draft, payload):
    ev = draft.get('evidence') or {}
    if ev.get('source_kind') != 'zying_detail' or ev.get('source_product_id') != payload['market_item_id']:
        raise ValueError('草稿MLM绑定不一致')
    result = prepare(root, draft, ZyingDomCandidates(payload))
    report = result['evidence']['source_offer']
    by_id = {c['offer_id']: c for c in payload['candidates']}
    scores = {}
    for row in report['rows']:
        for c in row['candidates']:
            if c.get('offer_id') in by_id:
                c.update({k: by_id[c['offer_id']][k] for k in ('title', 'price', 'currency', 'shop_name')})
                key = c['offer_id']
                if key not in scores or c['confidence'] > scores[key]['confidence']:
                    scores[key] = c
    top = sorted(scores.values(), key=lambda c: (-c['confidence'], c['offer_id']))[:3]
    status = ('BLOCKED_ON_LOGIN' if payload['status'] == 'BLOCKED_ON_LOGIN' else
              'CANDIDATE' if any(r['status'] == 'CANDIDATE' for r in report['rows']) else 'SKIPPED')
    report['dom_summary'] = {'status': status, 'top3': copy.deepcopy(top), 'payload_archive': payload['payload_archive']}
    result['human_approved'] = False
    if isinstance(result.get('pricing_plan'), dict):
        result['pricing_plan']['status'] = 'STALE'
    return result


def save(root, value):
    root = Path(root)
    clean = normalize(value)
    with SAVE_LOCK:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
        archive = Path('data/v3/offer_candidates') / clean['market_item_id'] / (stamp + '.json')
        target = root / archive
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as f:
            json.dump(clean, f, ensure_ascii=False, allow_nan=False)
        clean['payload_archive'] = archive.as_posix()
        matches = []
        for p in (root / 'drafts').glob('*-draft.json'):
            if p.is_symlink():
                continue
            ev = json.loads(p.read_text('utf-8')).get('evidence') or {}
            if ev.get('source_kind') == 'zying_detail' and ev.get('source_product_id') == clean['market_item_id']:
                matches.append(((ev.get('zying_detail') or {}).get('captured_at', ''), p.name))
        if not matches:
            return {'ok': True, 'status': 'ARCHIVED_UNBOUND', 'payload_archive': archive.as_posix(),
                    'message': '已存档；未找到对应智赢草稿，请先采集该MLM详情，再重新采集候选'}
        name = max(matches)[1]
        try:
            from .draft_editor import save_edited_draft
            save_edited_draft(name, {}, root/'drafts', root/'draft_versions', offer_capture=clean)
        except ValueError:
            return {'ok': True, 'status': 'ARCHIVED_PRESERVED', 'payload_archive': archive.as_posix(),
                    'message': '已存档；现有货源证据或配置不兼容，保留草稿，请人工核对'}
        ev = json.loads((root/'drafts'/name).read_text('utf-8'))['evidence']
        return {'ok': True, 'name': name, 'status': ev['source_offer']['dom_summary']['status'],
                'payload_archive': archive.as_posix(), 'message': '已保存候选证据；规格/件数/重尺及同款需人工核对'}
