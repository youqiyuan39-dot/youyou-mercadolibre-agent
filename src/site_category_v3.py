"""Resolve destination candidates by live leaf/domain/full-path evidence.

These checks are not a platform mapping guarantee; the published item must
still be read back. Unknown translations and ambiguous paths fail closed.
"""
import re
import unicodedata


PATH_TERMS = {
    'herramientas': 'tools', 'herramientas manuales': 'hand tools',
    'fijacion': 'fixing', 'llaves': 'wrenches',
    'accesorios para vehiculos': 'vehicle accessories',
    'herramientas para vehiculos': 'vehicle tools',
}

# Candidate learned from live /categories reads on 2026-10-22. This is only
# a discovery seed, never a bypass of leaf/domain/path/status verification.
# Evidence: docs/类目实时核验_20261022.json and docs/site-path-evidence-20261022.json.
PATH_CANDIDATES = {
    ('MLM', 'WRENCHES', ('tools', 'hand tools', 'fixing', 'wrenches')): ('MLM9171',),
}


def path_key(category):
    def normalize(value):
        text = unicodedata.normalize('NFKD', str(value or ''))
        text = ''.join(c for c in text if not unicodedata.combining(c)).casefold().strip()
        return PATH_TERMS.get(text, text)
    return tuple(normalize(r.get('name')) for r in category.get('path_from_root') or [])


def domain(category):
    return str((category.get('settings') or {}).get('catalog_domain') or
               category.get('domain_id') or '').split('-', 1)[-1]


def same_path(source, target):
    return len(path_key(source)) >= 2 and path_key(source) == path_key(target)


def discover(api, source, site_id, query):
    """Bounded discovery; a matching domain alone cannot select a branch."""
    known = PATH_CANDIDATES.get((site_id, domain(source), path_key(source)), ())
    if known:
        candidates = [{'category_id': cid} for cid in known]
    elif hasattr(api, 'domain_discovery'):
        candidates = api.domain_discovery(site_id, query)
    else:
        candidates = api.get(f'/sites/{site_id}/domain_discovery/search', {'q': query})
    if not isinstance(candidates, list):
        raise ValueError('官方类目候选返回格式异常')
    accepted = {}
    for candidate in candidates[:8]:
        cid = str(candidate.get('category_id') or '')
        if not re.fullmatch(rf'{re.escape(site_id)}\d+', cid):
            continue
        try:
            target = api.category(cid)
        except Exception:
            continue
        settings = target.get('settings') or {}
        if (target.get('id') == cid and target.get('children_categories') == []
                and domain(source) and domain(target) == domain(source)
                and same_path(source, target)
                and settings.get('listing_allowed') is True
                and settings.get('status') == 'enabled'):
            accepted[cid] = target
    if len(accepted) != 1:
        raise ValueError('目标站点完整路径未取得唯一可用叶子；不使用预测第一项或仅同域候选')
    return next(iter(accepted.values()))
