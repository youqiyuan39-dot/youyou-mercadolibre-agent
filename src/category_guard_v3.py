"""Check supplier identity against category before running every SKU."""
import json,re
from .supplier_facts_v3 import door_banner, wrong_type

def prepare(root, draft, query, discover):
    ev = draft.setdefault('evidence', {}); payload = draft.setdefault('payload', {})
    supplier = ev.setdefault('supplier_page', {})
    source = root / str(ev.get('source_capture') or '')
    capture = json.loads(source.read_text('utf-8')) if source.is_file() and source.resolve().is_relative_to(root.resolve()) else {}
    title = capture.get('title') or ev.get('source_title') or supplier.get('title') or ''
    supplier['title'] = title; ev['source_title'] = title
    category_id = payload.get('category_id')
    try:category = query(category_id, root) if category_id else None
    except Exception as exc:
        if 'HTTP 404' not in str(exc):raise
        # A removed/invalid ID is recoverable; connectivity/auth failures are not evidence.
        category=None;ev['category_recovery_error']={'previous_category':category_id,'reason':str(exc),'next_action':'RESOLVE_FROM_ORIGINAL_SOURCE'}
    garden_kit=bool(re.search(r'油泡|油管',title) and any('件套' in str(s.get('variant')) for s in capture.get('skus') or []))
    if garden_kit and (not category or any('Plants' in str(p.get('name')) for p in category.get('path_from_root') or [])):
        from .mercadolibre_client import MercadoLibreCategoryClient
        tree=MercadoLibreCategoryClient().category('CBT411906')
        kit=next((c for c in tree.get('children_categories') or [] if c.get('name')=='Replacement Parts Kits'),None)
        if not kit:raise ValueError('园林维修套装的官方类目未取得，不能使用植物球茎类目')
        category=query(kit['id'],root);payload['category_id']=kit['id']
        ev['category_correction']={'previous_category':category_id,'category_id':kit['id'],'reason':'油泡/油管维修套装须属于园林机械配件，不能属于植物球茎','source':'MERCADOLIBRE_OFFICIAL_CATEGORY_TREE'}
    mismatch = bool(category and wrong_type(title, category.get('name')))
    if mismatch:
        if ((draft.get('edit_metadata') or {}).get('field_locks') or {}).get('category_id'):
            raise ValueError('已锁定类目与供应商商品类型冲突，请核对类目')
        phrase = 'Day of the Dead decorative door hanging banner' if door_banner(title) else title
        result = discover(phrase, 'CBT', root=root)
        candidates = result.get('candidates') or []
        # Prefer the official party hanging domain, never choose construction hardware.
        match = next((row for row in candidates if str(row.get('domain_id') or '').endswith('DECORATIVE_PENNANTS_AND_GARLANDS') and str(row.get('category_name') or '').lower() == 'hangings'), None)
        category = query(match['category_id'], root) if match else result['selected']
        if wrong_type(title, category.get('name')):
            raise ValueError('官方类目与节日门帘用途仍冲突，停止生成错误属性')
        ev['category_correction'] = {'previous_category': category_id, 'category_id': category['category_id'],
            'reason': '供应商节日挂旗/门帘与原类目商品类型冲突', 'query': phrase, 'source': category.get('query_source'), 'queried_at': category.get('queried_at')}
        payload['category_id'] = category['category_id']
    if category:
        ev['official_category_snapshot'] = category
    # Existing IDs must pass the same source/type/full-path check as first intake.
    from .category_resolution_v3 import source_context,intent_for,assessment,resolve,apply_category,VERSION
    context=source_context(root,draft);intent=intent_for(context)
    # Legacy minimal test/query adapters lack taxonomy metadata. Real preflights
    # now preserve catalog_domain, so a leaf name cannot bypass validation.
    metadata_present=bool(category and ((category.get('settings') or {}).get('catalog_domain') or category.get('domain_id')))
    prior_review=ev.get('classification_review') or {}
    if metadata_present and intent and assessment(category,intent)[0]:
        decision={'version':VERSION,'status':'SOURCE_AND_PATH_VERIFIED','source_context':context,'intent':intent,'query':intent['query'],'category_id':category['category_id']}
        apply_category(draft,category,decision)
    elif not category or metadata_present and (intent or prior_review.get('version')!=VERSION or prior_review.get('source_context')!=context):
        if ((draft.get('edit_metadata') or {}).get('field_locks') or {}).get('category_id'):
            ev['classification_review']={'version':VERSION,'status':'NEEDS_REVIEW','reason':'人工锁定类目与当前来源或完整路径尚未通过核验','source_context':context}
            raise ValueError('人工锁定类目需先完成商品类型与完整路径核验，未覆盖原值')
        try:result=resolve(root,draft)
        except ValueError as exc:
            from .category_resolution_v3 import semantic_issues,quarantine_conflicting_category
            if category and semantic_issues(draft,category):quarantine_conflicting_category(draft,str(exc))
            else:ev['classification_review']={'version':VERSION,'status':'NEEDS_REVIEW','reason':str(exc),'source_context':context}
            raise
        category=query(result['selected']['category_id'],root)
        apply_category(draft,category,result)
    return category
