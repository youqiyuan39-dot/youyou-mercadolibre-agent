"""Read-only missing-input index and explicit, versioned local field imports.

Captured source facts and review/approval flags are never writable via this API.
Unknown evidence contracts remain visible and require their existing editor/intake.
"""
import copy
import csv
import hashlib
import io
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK = threading.RLock()
PREVIEWS = {}
COLUMNS = ('request_id', 'draft', 'product', 'sku_id', 'field', 'reason', 'source',
           'current_value', 'fill_value', 'editable', 'validation')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def target(draft, sid, field, source=''):
    """Allowlist only local editable facts. Never use an uploaded path."""
    ev = draft.get('evidence') or {}
    skus = ev.get('sku_details') or []
    matches = [i for i, s in enumerate(skus) if str(s.get('sku_id')) == sid]
    base = ('evidence', 'sku_details', matches[0]) if len(matches) == 1 else None
    # Captured detail/candidate/pricing-result inputs need recapture, not fabricated provenance.
    if any(x in source for x in ('zying_detail', 'source_offer', 'pricing_result')):
        return None
    if base and field in ('purchase_cost_cny', 'domestic_shipping_cny', 'shipping_cost_cny', 'cny_per_mxn', 'fx_source'):
        return base + (field,)
    leaf = field.removeprefix('package.')
    if base and leaf in ('weight_g', 'length_cm', 'width_cm', 'height_cm'):
        return base + ('package', leaf)
    if base and field in ('market_item_id', 'units_per_pack'):
        return base + ('market_reference', field)
    if not sid and field == 'domestic_shipping_cny':
        return ('pricing_plan', field)
    if not sid and field in ('gtin_or_exemption', 'compatibility_evidence'):
        return ('evidence', field)
    return None


def get_at(obj, path):
    for key in path or ():
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return None
    return obj if path else None


def issues(value, source, inherited_sid=''):
    """Walk structured issue containers, retaining scope and source locations."""
    if isinstance(value, list):
        for i, child in enumerate(value):
            yield from issues(child, f'{source}[{i}]', inherited_sid)
    elif isinstance(value, dict):
        sid = str(value.get('sku_id') or inherited_sid)
        emitted = False
        for key, children in value.items():
            if key in ('input_requests', 'blocking_reasons', 'issues', 'missing_inputs', 'evidence_issues') and isinstance(children, list):
                for i, child in enumerate(children):
                    row = child if isinstance(child, dict) else {'reason': str(child)}
                    if row.get('resolved') is True or row.get('status') in ('RESOLVED', 'resolved'):
                        continue
                    scopes = row.get('sku_ids') or [row.get('sku_id') or sid]
                    if not isinstance(scopes, list):
                        scopes = [scopes]
                    for scope in scopes:
                        yield row, str(scope or ''), f'{source}.{key}[{i}]'
                    emitted = True
            elif isinstance(children, (dict, list)) and key not in ('raw', 'supplier_page', 'supplier_facts', 'history'):
                yield from issues(children, source + '.' + key, sid)
        state = value.get('state') or value.get('decision') or value.get('status')
        if not emitted and (state == 'BLOCKED_EVIDENCE' or str(value.get('status')).lower() == 'skipped'):
            yield {'field': value.get('field') or '未核', 'code': state,
                   'reason': value.get('reason') or value.get('message') or value.get('error') or '缺少结构化原因，需核对原记录'}, sid, source


def aggregate(root):
    root = Path(root)
    rows, warnings = {}, []
    for path in sorted((root / 'drafts').glob('*-draft.json')):
        if path.is_symlink() or not path.resolve().is_relative_to((root / 'drafts').resolve()):
            warnings.append({'file': path.name, 'error': '拒绝链接草稿'})
            continue
        try:
            draft = load(path)
            if not isinstance(draft, dict):
                raise ValueError('草稿需为对象')
            docs = [(draft, 'draft')]
            if (draft.get('evidence') or {}).get('source_kind')=='zying_detail':
                from .pricing_result_v3 import contribution
                docs.append(({'sku_id':draft['evidence'].get('source_product_id'),
                              'input_requests':contribution(root,draft)['input_requests']},'local_contribution'))
            for relative in ('jobs/' + path.stem + '-full-ai-job.json',
                             'data/v3/auto-processing/' + path.stem + '.json'):
                other = root / relative
                if other.is_file() and not other.is_symlink():
                    try:
                        docs.append((load(other), relative))
                    except (ValueError, OSError) as exc:
                        warnings.append({'file': relative, 'error': type(exc).__name__})
            for doc, location in docs:
                for issue, sid, source in issues(doc, location):
                    field = str(issue.get('field') or '未核')
                    reason = str(issue.get('request') or issue.get('required_evidence') or issue.get('reason') or issue.get('code') or issue.get('cause') or '未核')
                    code = str(issue.get('code') or issue.get('cause') or 'NEEDS_INPUT')
                    identity = [path.name, sid, field, code, reason]
                    rid = 'ir_' + digest(identity)[:32]
                    mapping = target(draft, sid, field, source + str(issue.get('origin') or ''))
                    row = rows.setdefault(rid, dict(zip(COLUMNS, [rid, path.name,
                        (draft.get('payload') or {}).get('title') or draft.get('title') or path.stem,
                        sid, field, reason, [], get_at(draft, mapping), '', bool(mapping),
                        '单位：g/cm/CNY；重量≤15000g；仅MLM' if mapping else '未核：请使用原证据采集/审核入口补齐，禁止伪造来源或审核状态'])))
                    row['source'].append(source)
                    # A duplicate source contract cannot widen the writable surface.
                    if not mapping:
                        row['editable'] = False
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            warnings.append({'file': path.name, 'error': type(exc).__name__})
    result = sorted(rows.values(), key=lambda r: (r['draft'], r['sku_id'], r['field'], r['request_id']))
    for row in result:
        row['source'] = ' | '.join(sorted(set(row['source'])))
    return {'ok': True, 'rows': result, 'count': len(result), 'warnings': warnings,
            'read_only': True, 'site_id': 'MLM'}


def cell(value):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value if value is not None else '')
    # Prevent Excel formula execution while preserving an importable stable ID.
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def export_csv(root):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    for row in aggregate(root)['rows']:
        writer.writerow({k: cell(row[k]) for k in COLUMNS})
    return stream.getvalue().encode('utf-8-sig')


def validate_value(field, value):
    field = field.removeprefix('package.')
    if field in ('compatibility_evidence', 'gtin_or_exemption', 'fx_source'):
        if not isinstance(value, str) or not value.strip() or len(value) > 3000:
            raise ValueError('需填写1～3000字的明确证据说明')
        return value.strip()
    if field == 'market_item_id':
        if not isinstance(value, str) or not re.fullmatch(r'MLM\d{6,20}', value):
            raise ValueError('仅支持MLM商品编号')
        return value
    units = {'weight_g': ('g', 'kg'), 'length_cm': ('cm', 'mm', 'm'),
             'width_cm': ('cm', 'mm', 'm'), 'height_cm': ('cm', 'mm', 'm'),
             'purchase_cost_cny': ('CNY',), 'domestic_shipping_cny': ('CNY',),
             'shipping_cost_cny': ('CNY',), 'cny_per_mxn': ()}
    if isinstance(value, bool):
        raise ValueError('不接受布尔值')
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*', str(value))
    if not match:
        raise ValueError('数值/单位格式错误，禁止NaN/Infinity/负数')
    number, unit = float(match[1]), match[2]
    if unit and unit not in units.get(field, ()):
        raise ValueError('单位错误；使用字段名所示单位或g/kg、cm/mm/m、CNY')
    number *= {'kg': 1000, 'mm': .1, 'm': 100}.get(unit, 1)
    if not math.isfinite(number) or number < 0 or (field not in ('domestic_shipping_cny','shipping_cost_cny') and number == 0):
        raise ValueError('数值必须有限且为正数；国内运费可为0')
    if field == 'weight_g' and number > 15000:
        raise ValueError('不可发：重量超过15kg')
    if field == 'units_per_pack' and (not number.is_integer() or number > 100000):
        raise ValueError('套装数量需为1～100000整数')
    if field.endswith('_cm') and number > 1000:
        raise ValueError('包装边长超出本导入器范围(0,1000]cm，需人工核查')
    if field.endswith('_cny') and number > 1000000:
        raise ValueError('成本超出本导入器范围[0,1000000]CNY，需人工核查')
    return int(number) if field == 'units_per_pack' else number


def apply_updates(draft, updates):
    """Internal scoped save: validate again and preserve every unrelated field."""
    result = copy.deepcopy(draft)
    for item in updates:
        path = target(result, item['sku_id'], item['field'], item.get('source', ''))
        if path is None:
            raise ValueError('字段不在可写白名单')
        value = validate_value(item['field'], item['value'])
        node = result
        for key in path[:-1]:
            node = node[key] if isinstance(key, int) or isinstance(node, list) else node.setdefault(key, {})
        node[path[-1]] = value
        if len(path) > 3 and path[-2] == 'package':
            node.setdefault('field_status', {})[path[-1]] = 'USER_INPUT_UNVERIFIED'
            node.setdefault('field_sources', {})[path[-1]] = [{'source': 'HUMAN_INPUT_TABLE',
                'request_id': item.get('request_id'), 'unit': 'g' if path[-1] == 'weight_g' else 'cm'}]
            node['status'] = 'USER_INPUT_UNVERIFIED'
        elif path[-2] == 'market_reference':
            node['spec_match'] = 'SOURCE_SELECTED_PENDING_REVIEW'
    # Editing inputs invalidates approval; never upgrade evidence, price or publish state.
    result['human_approved'] = False
    result['requested_action'] = 'draft'
    result['status'] = 'DRAFT_LOCAL_ONLY'
    metadata = result.setdefault('edit_metadata', {})
    metadata['input_request_import'] = {
        'saved_at': datetime.now(timezone.utc).isoformat(),
        'request_ids': [u.get('request_id') for u in updates],
        'publishing_performed': False,
        'source': 'HUMAN_INPUT_TABLE',
    }
    if isinstance(result.get('pricing_plan'), dict):
        result['pricing_plan']['status'] = 'STALE'
    return result


def import_table(root, payload, legacy=None):
    """Preview is stateless/read-only. Confirm must repeat payload and preview hash.

    Hash binds every affected draft plus issue snapshot; any edit requires a new preview.
    Validation is batch-wide, writes are versioned per draft (not cross-draft atomic).
    """
    if not isinstance(payload, dict):
        raise ValueError('请求需为对象')
    mode = payload.get('mode', 'dry_run')
    if mode not in ('dry_run', 'confirm'):
        raise ValueError('mode需为dry_run或confirm')
    content = payload.get('content', payload.get('rows'))
    if isinstance(content, str):
        if len(content) > 5_000_000:
            raise ValueError('导入最大5MB')
        text = content.lstrip('\ufeff')
        content = json.loads(text) if payload.get('format') == 'json' or text.lstrip().startswith(('[', '{')) else list(csv.DictReader(io.StringIO(text)))
    if isinstance(content, dict):
        content = content.get('rows')
    if not isinstance(content, list) or len(content) > 10000:
        raise ValueError('导入需为最多10000行CSV/JSON')
    root = Path(root)
    with LOCK:
        index = {r['request_id']: r for r in aggregate(root)['rows']}
        updates, errors, seen, originals, targets = [], [], set(), {}, set()
        for i, incoming in enumerate(content):
            try:
                if not isinstance(incoming, dict):
                    raise ValueError('行需为对象')
                value = incoming.get('fill_value', '')
                if value is None or value == '':
                    continue
                rid = incoming.get('request_id')
                if rid in seen:
                    raise ValueError('重复request_id')
                seen.add(rid)
                row = index.get(rid)
                if not row:
                    raise ValueError('request_id已失效或未知，请重新导出')
                if not row['editable']:
                    raise ValueError(row['validation'])
                for key in ('draft', 'sku_id', 'field'):
                    if key in incoming and incoming[key] != row[key]:
                        raise ValueError('行身份被修改：' + key)
                path = root / 'drafts' / row['draft']
                draft = originals.setdefault(row['draft'], load(path))
                sites = (draft.get('payload') or {}).get('sites_to_sell') or []
                if (any(s.get('site_id') not in (None, '', 'MLM') for sku in (draft.get('evidence') or {}).get('sku_details', []) for s in sku.get('site_pricing', []))
                        or (draft.get('payload') or {}).get('site_id', 'MLM') != 'MLM'
                        or draft.get('site_id', 'MLM') != 'MLM'
                        or any((s.get('site_id') if isinstance(s, dict) else s) != 'MLM' for s in sites)):
                    raise ValueError('仅支持MLM草稿')
                job = root / 'jobs' / (Path(row['draft']).stem + '-full-ai-job.json')
                if job.is_file() and load(job).get('status') in ('queued', 'running'):
                    raise ValueError('商品处理中，需等待完成或按原入口暂停后再补填')
                mapping = target(draft, row['sku_id'], row['field'], row['source'])
                if mapping is None:
                    raise ValueError('来源合同不支持直接回填')
                key = (row['draft'], mapping)
                if key in targets:
                    raise ValueError('多个待补行指向同一字段，请只填写一行')
                targets.add(key)
                normalized = validate_value(row['field'], value)
                updates.append({**row, 'value': normalized})
            except (ValueError, TypeError, KeyError, OSError) as exc:
                errors.append({'row': i + 1, 'error': str(exc)})
        preview_hash = digest({'updates': updates, 'drafts': originals})
        result = {'ok': True, 'mode': mode, 'valid': not errors, 'errors': errors,
                  'updates': updates, 'count': len(updates), 'preview_hash': preview_hash,
                  'written': [], 'publishing_performed': False}
        if mode == 'dry_run':
            now = time.monotonic()
            for key, expiry in list(PREVIEWS.items()):
                if expiry < now:
                    PREVIEWS.pop(key, None)
            if not errors and updates:
                PREVIEWS[(str(root.resolve()), preview_hash)] = now + 900
            return result
        preview_key = (str(root.resolve()), preview_hash)
        if (payload.get('confirm') is not True or payload.get('preview_hash') != preview_hash
                or PREVIEWS.get(preview_key, 0) < time.monotonic()):
            raise ValueError('需先dry_run预览，再明确confirm；草稿变化后必须重新预览')
        if errors:
            return result
        if legacy is None:
            from . import draft_editor as legacy
        PREVIEWS.pop(preview_key, None)
        for name in originals:
            grouped = [u for u in updates if u['draft'] == name]
            if not grouped:
                continue
            # Recheck immediately before each versioned save, including external editor changes.
            if load(root / 'drafts' / name) != originals[name]:
                result.update(valid=False, errors=[{'draft': name, 'error': '草稿已变更，请重新预览'}])
                break
            try:
                paths = legacy.save_edited_draft(name, {}, root / 'drafts', root / 'draft_versions', field_updates=grouped)
                result['written'].append({'draft': name, 'version': Path(paths[1]).name})
            except Exception as exc:
                result.update(valid=False, errors=[{'draft': name, 'error': type(exc).__name__ + '；请核对版本及草稿后重新导出，可能已保存'}])
                break
        return result
