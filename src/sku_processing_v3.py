"""Serial local AI pipeline: originals -> isolated SKU copy/attributes -> budget.
No paid image calls and no publishing. Each successful SKU is committed separately.
"""
import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from .auto_processing_v3 import write

VERSION = 'INTAKE_LOCAL_SKU_V10_COMPOSITION_AND_RESUME'
RUN_LOCK = threading.Lock()
START_LOCK = threading.Lock()

def copy_input_signature(work,settings):
    from .flow_resume_v3 import text_signature
    safe=copy.deepcopy(work)
    for sku in (safe.get('evidence') or {}).get('sku_details') or []:
        for key in ('site_pricing','listing_price_usd','target_net_proceeds_usd','autopricing_by_site','market_reference'):
            sku.pop(key,None)
    return text_signature(safe,settings)

def copy_source_signature(work,settings):
    safe=copy.deepcopy(work);safe.setdefault('payload',{})['category_id']='COPY_SOURCE_ONLY'
    locks=(safe.get('edit_metadata') or {}).get('field_locks') or {}
    manual={k:safe['payload'].get(k) for k in ('title','description','family_name') if locks.get(k)}
    return hashlib.sha256(json.dumps([copy_input_signature(safe,settings),manual],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def bind_targets(draft):
    """Inherit target identity, never another SKU's price or measurements."""
    targets=draft.setdefault('payload',{}).get('sites_to_sell') or [{'site_id':'MLM','logistic_type':'remote'}]
    if not draft['payload'].get('sites_to_sell'):draft['payload']['sites_to_sell']=copy.deepcopy(targets)
    safe=[{k:copy.deepcopy(t[k]) for k in ('site_id','logistic_type','listing_type_id') if k in t} for t in targets if t.get('site_id')]
    for sku in (draft.get('evidence') or {}).get('sku_details') or []:
        if not sku.get('site_pricing'):sku['site_pricing']=copy.deepcopy(safe)


def scope(draft, sku, root=None):
    result = copy.deepcopy(draft)
    evidence = result.setdefault('evidence', {})
    sid = str(sku['sku_id'])
    metadata=result.setdefault('edit_metadata',{})
    metadata.setdefault('field_locks',{}).update(copy.deepcopy(sku.get('human_field_locks') or {}))
    for key in sku.get('human_attribute_locks') or {}:metadata.setdefault('attribute_sources',{})[key]='manual'
    facts_sku = {k: copy.deepcopy(v) for k, v in sku.items() if k not in {'ai_content', 'ai_processing', 'autopricing_by_site','human_field_locks','human_attribute_locks','previous_valid_ai_content'}}
    evidence['sku_details'] = [facts_sku]
    for key in ('multi_sku_ai', 'processing_automation', 'image_generation_review'):
        evidence.pop(key, None)
    evidence['current_pricing_sku_id'] = sid
    supplier = evidence.setdefault('supplier_page', {})
    if 'procurement' in sku:
        supplier['procurement_skus'] = copy.deepcopy(supplier.get('procurement_skus') or supplier.get('skus') or [])
    raw = next((r for r in supplier.get('skus') or [] if str(r.get('sku_id')) == sid), {})
    supplier['skus'] = [{**copy.deepcopy(raw), **copy.deepcopy(facts_sku)}]
    supplier['sku_details'] = [copy.deepcopy(facts_sku)]
    supplier['selected_variant'] = sku.get('variant')
    supplier['user_attribute_policy'] = copy.deepcopy(sku.get('user_attribute_policy') or {})
    supplier['product_measurements'] = copy.deepcopy(sku.get('product_measurements') or {})
    supplier['current_pricing_sku_id'] = sid
    # Full captures may include other variants; snapshot only the supplier facts in this work package.
    capture_path = (root / str(evidence.get('source_capture') or '')) if root else None
    if capture_path and capture_path.is_file() and capture_path.resolve().is_relative_to(root.resolve()):
        capture = json.loads(capture_path.read_text(encoding='utf-8'))
        supplier['title'] = capture.get('title') or supplier.get('title')
        supplier['description'] = capture.get('description') or supplier.get('description')
        supplier['attributes'] = capture.get('attributes') or supplier.get('attributes') or {}
    evidence['source_title'] = supplier.get('title') or evidence.get('source_title') or (draft.get('payload') or {}).get('title')
    evidence.pop('source_capture', None)
    from .supplier_scope_v3 import attributes_for_sku
    supplier['attributes'] = attributes_for_sku(supplier.get('attributes') or {}, sku, (draft.get('evidence') or {}).get('sku_details') or [])
    source_facts=sku.get('supplier_facts') or {}
    from .task_evidence_v3 import digest
    source_facts_current = not capture_path or not capture_path.is_file() or source_facts.get('source_sha256') == digest(capture)
    if source_facts.get('origin')=='SUPPLIER_CAPTURE' and source_facts_current:
        supplier['attributes'].update(copy.deepcopy(source_facts.get('attributes') or {}))
        supplier['source_quotes']=copy.deepcopy(source_facts.get('source_quotes') or {})
    from .model_resolution_v3 import attach_context
    attach_context(supplier,sku)
    supplier.pop('sku_image_sets', None)
    review = evidence.setdefault('image_review', {})
    sources = next((g.get('gallery') or [] for g in review.get('sku_source_galleries') or [] if str(g.get('sku_id')) == sid), [])
    if not sources and len((draft.get('evidence') or {}).get('sku_details') or []) <= 1:
        sources = review.get('source_gallery') or []
    review['source_gallery'] = copy.deepcopy(sources)
    review['sku_source_galleries'] = [{'sku_id': sid, 'gallery': copy.deepcopy(sources)}]
    review['detail_gallery'] = []
    selected = next((g.get('gallery') or [] for g in review.get('sku_listing_galleries') or [] if str(g.get('sku_id')) == sid), [])
    review['gallery'] = copy.deepcopy(selected)
    review['sku_listing_galleries'] = [{'sku_id': sid, 'gallery': copy.deepcopy(selected)}]
    review['sku_gallery'] = copy.deepcopy(sources)
    for key in ('source_screening', 'generated_gallery', 'rejected_gallery'):
        review.pop(key, None)
    payload = result.setdefault('payload', {})
    payload['description'] = ((sku.get('ai_content') or {}).get('description') or (draft.get('payload') or {}).get('description') or '') if ((result.get('edit_metadata') or {}).get('field_locks') or {}).get('description') else ''
    payload['seller_sku'] = sku.get('seller_sku') or payload.get('seller_sku')
    payload['available_quantity'] = sku.get('available_quantity', 200)
    # Reuse only this SKU's saved attributes. Common attributes can contain another variant's inferred values.
    payload['attributes'] = copy.deepcopy((sku.get('ai_content') or {}).get('attributes') or [])
    if str((draft.get('evidence') or {}).get('current_pricing_sku_id')) == sid:
        human_sources = (draft.get('edit_metadata') or {}).get('attribute_sources') or {}
        human = [a for a in (draft.get('payload') or {}).get('attributes') or [] if human_sources.get(a.get('id')) in {'human_confirmed', 'supplier_confirmed', 'manual', 'user_confirmed'}]
        by_id = {a['id']: a for a in payload['attributes'] + copy.deepcopy(human)}
        payload['attributes'] = list(by_id.values())
    if sku.get('site_pricing'): payload['sites_to_sell'] = copy.deepcopy(sku['site_pricing'])
    pack = sku.get('package') or {}
    # These legacy product_* fields were populated from packaging. Never use them as product measurements.
    for key in ('product_weight_g', 'product_length_cm', 'product_width_cm', 'product_height_cm'):
        supplier.pop(key, None)
    original = draft.get('evidence') or {}
    same_sku = sid == str(original.get('current_pricing_sku_id')) or len(original.get('sku_details') or []) == 1
    for key in ('packed_weight', 'packed_dimensions'):
        evidence.pop(key, None)
    if pack.get('weight_g') is not None:
        evidence['packed_weight'] = {'value': pack['weight_g'], 'unit': 'g', 'status': (pack.get('field_status') or {}).get('weight_g') or pack.get('status') or ((original.get('packed_weight') or {}).get('status') if same_sku else None) or 'UNVERIFIED'}
    if all(pack.get(k) is not None for k in ('length_cm', 'width_cm', 'height_cm')):
        evidence['packed_dimensions'] = {'length': pack['length_cm'], 'width': pack['width_cm'], 'height': pack['height_cm'], 'unit': 'cm', 'status': pack.get('status') or ((original.get('packed_dimensions') or {}).get('status') if same_sku else None) or 'UNVERIFIED'}
    # Explicit user locks on single/current SKU packaging are authoritative, never copied into other variants.
    original = draft.get('evidence') or {}
    if sid == str(original.get('current_pricing_sku_id')):
        for key, lock in (('packed_weight', 'weight'), ('packed_dimensions', 'dimensions')):
            if ((draft.get('edit_metadata') or {}).get('field_locks') or {}).get(lock) and key in original:
                evidence[key] = copy.deepcopy(original[key])
    return result


def capture_signature(draft, root):
    relative = (draft.get('evidence') or {}).get('source_capture')
    path = root / str(relative or '')
    if relative and path.is_file() and path.resolve().is_relative_to(root.resolve()):
        value = json.loads(path.read_text(encoding='utf-8'))
        value = {k: v for k, v in value.items() if k not in {'captured_at', 'collected_at'}}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return relative


def install(legacy):
    if getattr(legacy, '_v3_sku_processing_installed', False): return
    legacy._v3_sku_processing_installed = True

    def run(path, root, full_path):
        with RUN_LOCK:
            job = legacy.read_json(full_path)
            edit_signature = json.dumps(legacy.read_json(path).get('edit_metadata') or {}, sort_keys=True)
            def check():
                latest = legacy.read_json(full_path)
                if latest.get('status') == 'paused': raise InterruptedError('任务已暂停')
                current = legacy.read_json(path)
                if json.dumps(current.get('edit_metadata') or {}, sort_keys=True) != edit_signature:
                    raise InterruptedError('商品编辑已更新，本轮暂停以保留人工修改；检查后可重新处理')
                if capture_signature(current, root) != source_capture:
                    raise InterruptedError('采集资料已更新，本轮停止写入；新资料将另行处理')
            def save(**changes):
                check(); legacy._save_full_job(full_path, job, **changes)
            source_capture = capture_signature(legacy.read_json(path), root)
            try:
                from .source_image_policy_v3 import policy as source_image_policy
                source_image_policy(root)
                check()
                draft = legacy.read_json(path)
                backup = root / 'draft_versions' / (path.stem + '-before-auto-' + datetime.now().strftime('%Y%m%d%H%M%S%f') + '.json')
                write(backup, draft)
                save(status='running',stage='读取单品身份与包装表',progress=1,message='无规格单品建立本地唯一编号；包装表按明确SKU或唯一单品归属恢复')
                from .task_evidence_v3 import restore
                restore(root,draft)
                from .round2_evidence_v3 import recover as recover_exact_source
                recover_exact_source(root,draft)
                bind_targets(draft)
                if not (draft.get('evidence') or {}).get('sku_details'):
                    raise ValueError('采集未取得规格记录，且没有确认这是无规格单品；保留包装表，需重采规格标识，不需要重复手填已有重尺')
                from .product_match_v3 import refresh as review_matching
                matching = review_matching(draft)
                conflicts = {r['sku_id']: r for r in matching['rows'] if r['state'] == 'CONFLICT'}
                conflicts.update({r['sku_id']:r for r in matching['rows'] if (r.get('procurement') or {}).get('issues')})
                if conflicts and len(conflicts) == len(draft['evidence']['sku_details']):
                    # Preserve valid prior copy/images; a new model request cannot
                    # reconcile a different model or bundle without new evidence.
                    draft['evidence'].setdefault('processing_automation',{}).update(
                        version=VERSION, artifact_usable=False, identity_blocked_sku_ids=list(conflicts),
                        completion_state='NEEDS_MATCH')
                    check(); write(path, draft)
                    from .auto_processing_v3 import input_requests
                    blocked_items=[{'sku_id': sid, 'status': 'blocked_identity', 'issues': r['issues']} for sid, r in conflicts.items()]
                    save(status='done', completion_state='NEEDS_MATCH', stage='市场与采购规格冲突', progress=100,
                         items=blocked_items,input_requests=input_requests(blocked_items),
                         error=None, message='已保留采购事实和既有产物；先更正市场/采购对应关系，再继续内容处理。本轮未重复调用模型。')
                    return
                from .category_guard_v3 import prepare
                save(status='running',stage='核对商品类型与官方类目',progress=2,message='先检查源商品与类目是否一致；本批次共用官方属性定义，SKU事实各自处理')
                prepare(root,draft,legacy.query_official_category,legacy.discover_official_category)
                from .package_recovery_v3 import recover
                save(stage='补全明确标注的包装数据',progress=3,message='先查供应商文字与包装标注图；缺失数字不估测，不调用收费模型')
                from .task_control_v3 import task
                with task(root,path.name,source_title=(draft.get('evidence') or {}).get('source_title') or '',request_limit=20,seconds=600):
                    recover(root,draft,[s for s in (draft.get('evidence') or {}).get('sku_details') or [] if str(s['sku_id']) not in conflicts],check=check)
                check();write(path,draft)
                evidence = draft.setdefault('evidence', {})
                skus = evidence.get('sku_details') or []
                if not skus:
                    raise ValueError('采集记录没有 SKU，需重新采集明确规格')
                if not draft.setdefault('payload', {}).get('sites_to_sell'):
                    draft['payload']['sites_to_sell'] = [{'site_id': 'MLM', 'logistic_type': 'remote'}]
                save(status='running', stage='筛选货源原图', progress=3, message=f'先筛 {len(skus)} 个 SKU 的原图，不调用付费生图')
                from .source_image_screening_v3 import screen
                with task(root,path.name,source_title=(draft.get('evidence') or {}).get('source_title') or '',request_limit=80,seconds=1200):
                    reports = screen(root, draft, [s for s in skus if str(s['sku_id']) not in conflicts], check=check,
                        progress=lambda sid, done, total: save(stage='筛选货源原图', progress=5, message=f'SKU {sid}：已检查 {done}/{total} 张原图'))
                check()
                current = legacy.read_json(path)
                current.setdefault('evidence', {})['image_review'] = draft['evidence']['image_review']
                current['evidence']['image_generation_review'] = draft['evidence']['image_generation_review']
                current.setdefault('payload', {}).setdefault('sites_to_sell', draft['payload']['sites_to_sell'])
                if not current['payload'].get('sites_to_sell'): current['payload']['sites_to_sell'] = draft['payload']['sites_to_sell']
                write(path, current)
                from .source_image_policy_v3 import shortage_items
                image_blocked = {i['sku_id']: i for i in shortage_items(reports)}
                items, first_success = [], True
                for index, sku in enumerate(skus):
                    check()
                    sid = str(sku['sku_id'])
                    if sid in conflicts:
                        items.append({'sku_id': sid, 'status': 'blocked_identity', 'issues': conflicts[sid]['issues']})
                        save(items=items)
                        continue
                    if sid in image_blocked:
                        items.append(image_blocked[sid])
                        save(items=items)
                        continue
                    save(stage=f'逐 SKU 文案与属性 {index + 1}/{len(skus)}', progress=15 + int(60 * index / len(skus)), message=f'当前规格：{sku.get("variant") or sid}；其他 SKU 的资料不进入本工作包')
                    current = legacy.read_json(path)
                    work = scope(current, sku, root)
                    work_id = hashlib.sha256((path.name + ':' + sid).encode()).hexdigest()[:20]
                    work_path = root / 'data' / 'v3' / 'sku-work' / (work_id + '-draft.json')
                    # Keep completed output for resume checkpoints if inputs have not changed.
                    from .flow_resume_v3 import text_signature
                    settings=legacy.get_public_settings(root)
                    signature = copy_input_signature(work,settings)
                    previous = legacy.read_json(work_path) if work_path.exists() else {}
                    checkpoint_path=root/'data/v3/checkpoints'/(work_path.stem+'.json')
                    checkpoint=legacy.read_json(checkpoint_path) if checkpoint_path.exists() else {}
                    reuse=bool((previous.get('payload') or {}).get('description') and checkpoint.get('text_signature')==text_signature(previous,settings) and copy_input_signature(previous,settings)==signature)
                    category_only=bool(previous and previous.get('payload',{}).get('category_id')!=work['payload'].get('category_id') and previous.get('payload',{}).get('description') and copy_source_signature(previous,settings)==copy_source_signature(work,settings))
                    if category_only:
                        proposal_path=root/'ai_outputs'/(work_path.stem+'-proposal.json')
                        proposal=(legacy.read_json(proposal_path).get('proposal') or {}) if proposal_path.exists() else {}
                        from .ai_listing_pipeline import proposal_quality_issues
                        category_only=bool(proposal and not proposal_quality_issues(proposal,work))
                    if category_only:
                        for key in ('title','family_name','description','sites_to_sell'):
                            if not ((work.get('edit_metadata') or {}).get('field_locks') or {}).get(key):work['payload'][key]=copy.deepcopy(previous['payload'].get(key))
                    if not reuse:
                        work['evidence']['work_input_signature'] = signature; write(work_path, work)
                    try:
                        # Separate child progress prevents the legacy stage percentages from moving backwards.
                        child_path = root / 'jobs' / (work_id + '-sku-content-job.json')
                        child_job = {'status': 'running', 'sku_id': sid}
                        write(child_path, child_job)
                        from .task_control_v3 import task
                        with task(root,path.name,sid,(current.get('evidence') or {}).get('source_title') or ''):
                            if category_only:
                                repaired=legacy.read_json(work_path);legacy.refresh_category_attributes(repaired,root)
                                write(work_path,repaired);checkpoint['text_signature']=text_signature(repaired,settings);write(checkpoint_path,checkpoint)
                            else:legacy._apply_ai_content_and_attributes(work_path, root, child_path, child_job)
                        check()
                        output = legacy.read_json(work_path)
                        # Revalidate retained attributes without regenerating valid copy.
                        # A weight or style number is not proof of a quantity/length.
                        from .attribute_context_v3 import RISK_KEYS, source_value_supported, SEMANTIC_VERSION, normalize_enum, valid_value
                        supplier=(output.get('evidence') or {}).get('supplier_page') or {}
                        manual=(output.get('edit_metadata') or {}).get('attribute_sources') or {}
                        definitions={a['id']:a for a in (output.get('evidence',{}).get('official_category_snapshot') or {}).get('all_attributes') or []}
                        from .round2_evidence_v3 import candidate_attributes
                        restored={a['id']:a for a in output.get('payload',{}).get('attributes') or []}
                        for a in candidate_attributes(supplier,list(definitions.values())):
                            if manual.get(a['id']) not in {'manual','human_confirmed','supplier_confirmed','user_confirmed'}:restored[a['id']]=a
                        output['payload']['attributes']=list(restored.values())
                        rejected=[];kept=[];enum_corrections=[]
                        for attribute in (output.get('payload') or {}).get('attributes') or []:
                            key=attribute.get('id')
                            human=manual.get(key) in {'manual','human_confirmed','supplier_confirmed','user_confirmed'}
                            if not human and key in definitions:
                                normalized=normalize_enum(attribute,definitions[key])
                                if normalized!=attribute:enum_corrections.append({'id':key,'before':copy.deepcopy(attribute),'after':copy.deepcopy(normalized),'cause':'ENUM_NAME_ID_CONFLICT'})
                                attribute=normalized
                            if not human and (not source_value_supported(attribute,supplier,definitions.get(key)) or key in definitions and not valid_value(attribute,definitions[key])):
                                rejected.append({'id':key,'previous_value':copy.deepcopy(attribute),'cause':'NO_FIELD_SPECIFIC_SOURCE','sku_id':sid,'next_action':'READ_EXPLICIT_FIELD_LABEL_OR_RETAIN_UNKNOWN'})
                            else:kept.append(attribute)
                        if rejected or enum_corrections:
                            output['payload']['attributes']=kept
                            completion=output.setdefault('evidence',{}).setdefault('attribute_completion',{})
                            completion['unresolved']=list(dict.fromkeys((completion.get('unresolved') or [])+[r['id'] for r in rejected]))
                            write(work_path,output)
                            checkpoint=legacy.read_json(checkpoint_path) if checkpoint_path.exists() else {}
                            checkpoint['text_signature']=text_signature(output,settings);write(checkpoint_path,checkpoint)
                        content = {key: copy.deepcopy(output.get('payload', {}).get(key)) for key in ('title', 'family_name', 'description', 'attributes', 'category_id', 'sites_to_sell')}
                        content['attribute_completion'] = (output.get('evidence') or {}).get('attribute_completion') or {}
                        content['attribute_origins'] = (output.get('evidence') or {}).get('attribute_origins') or {}
                        from .model_resolution_v3 import resolve as resolve_model
                        resolution=resolve_model(supplier)
                        content['model_resolution']=copy.deepcopy(resolution)
                        if any(a.get('id')=='MODEL' and a.get('value_name')==resolution.get('value_name') for a in kept):
                            if manual.get('MODEL') not in {'manual','human_confirmed','supplier_confirmed','user_confirmed'}:
                                content['attribute_origins']['MODEL']=resolution['origin']
                            content['attribute_completion']['unresolved']=[key for key in content['attribute_completion'].get('unresolved') or [] if key!='MODEL']
                        # Persist the same provenance and completion state used by the
                        # parent draft, so checkpoint reuse cannot revive stale gaps.
                        output.setdefault('evidence',{}).update({
                            'model_resolution':copy.deepcopy(resolution),
                            'attribute_origins':copy.deepcopy(content['attribute_origins']),
                            'attribute_completion':copy.deepcopy(content['attribute_completion'])})
                        output['payload']['attributes']=copy.deepcopy(kept)
                        write(work_path,output)
                        checkpoint=legacy.read_json(checkpoint_path) if checkpoint_path.exists() else {}
                        checkpoint['text_signature']=text_signature(output,settings);write(checkpoint_path,checkpoint)
                        content['attribute_semantic_review']={'version':SEMANTIC_VERSION,'rejected':rejected,'enum_corrections':enum_corrections,'supplier_scope':sid,'manual_values_preserved':True}
                        content['status'] = 'READY_FOR_FINAL_REVIEW'
                        content['human_approved'] = False
                        current = legacy.read_json(path)
                        target = next(row for row in current['evidence']['sku_details'] if str(row['sku_id']) == sid)
                        target['ai_content'] = content
                        target['model_resolution']={**(target.get('model_resolution') or {}),**copy.deepcopy(resolution)}
                        target['ai_processing'] = {'status': 'done', 'source_signature': signature}
                        # Localized copy and target identity are different fields.
                        titles={s.get('site_id'):s.get('title') for s in content.get('sites_to_sell') or [] if s.get('title')}
                        for site in target.get('site_pricing') or []:
                            if titles.get(site.get('site_id')):site['title']=titles[site['site_id']]
                        locks = (current.get('edit_metadata') or {}).get('field_locks') or {}
                        if first_success:
                            for key in ('title', 'family_name', 'description', 'attributes', 'category_id'):
                                if not locks.get(key) and not (key == 'family_name' and locks.get('title')):
                                    current['payload'][key] = copy.deepcopy(content[key])
                            first_success = False
                        current['human_approved'] = False
                        write(path, current)
                        items.append({'sku_id': sid, 'status': 'done', 'variant': sku.get('variant'),'reused_checkpoint':reuse,'category_attributes_only':category_only})
                        legacy._save_full_job(child_path, child_job, status='done', progress=100)
                    except InterruptedError: raise
                    except Exception as exc:
                        check()
                        current = legacy.read_json(path)
                        target = next(row for row in current['evidence']['sku_details'] if str(row['sku_id']) == sid)
                        save_failure=isinstance(exc,(PermissionError,OSError)) or '保存目标已被其他操作修改' in str(exc)
                        target['ai_processing'] = {'status': 'failed', 'error': str(exc)[:500], 'cause':'SAVE_IO_CONFLICT' if save_failure else 'GENERATION_OR_VALIDATION_FAILED','source_signature':signature,'next_action':'RETRY_SAVE_FROM_CHECKPOINT' if save_failure else 'CHECK_FAILED_MODEL_OUTPUT'}
                        # Stale prior text must never masquerade as the latest successful SKU output.
                        if target.get('ai_content') and not save_failure:
                            target['previous_valid_ai_content']=target.pop('ai_content')
                        write(path, current)
                        items.append({'sku_id': sid, 'status': 'failed', 'error': str(exc)[:500]})
                    save(items=items)
                save(stage='计算全 SKU 利润预算', progress=85, message='只保存预算建议，发布前仍需人工确认利润；缺成本的 SKU 标为待补')
                pricing_error = None
                try: legacy.refresh_competitor_pricing(path, root)
                except Exception as exc: pricing_error = str(exc)[:600]
                check()
                current = legacy.read_json(path)
                from .listing_brand_v3 import apply as apply_brand
                apply_brand(current,root)
                current['evidence']['processing_automation'] = {'version': VERSION, 'sku_results': items,
                    'paid_image_calls': 0, 'profit_requires_human_confirmation': True, 'pricing_error': pricing_error,
                    'image_missing': [{'sku_id': r['sku_id'], 'count': r['missing_count']} for r in reports]}
                from .task_evidence_v3 import issues as evidence_issues
                from .product_match_v3 import blocking_issues as matching_issues
                review_matching(current)
                missing_evidence=evidence_issues(current)
                missing_evidence += [{'field':'market_match', 'cause':'IDENTITY_UNVERIFIED', 'required_evidence':text} for text in matching_issues(current)]
                from .procurement_v3 import image_scope_issues
                missing_evidence += [{'field':'sku_images','cause':'SALES_UNIT_IMAGE_SCOPE','required_evidence':text} for text in image_scope_issues(current)]
                missing_evidence += [{'sku_id':r['sku_id'],'field':'sku_images','cause':'IMAGE_COUNT_INSUFFICIENT',
                                      'required_evidence':f'该SKU尚缺{r["missing_count"]}张合格图片；先检查已有源图及拒绝原因，保留已通过图片，不要求整组重做'}
                                     for r in reports if r.get('missing_count')]
                if pricing_error:missing_evidence.append({'field':'pricing','cause':'PRICING_INPUT_REQUIRED','required_evidence':pricing_error})
                current['evidence']['processing_automation']['evidence_issues']=missing_evidence
                current['evidence']['processing_automation']['artifact_usable']=not missing_evidence and not pricing_error and not any(r.get('missing_count') for r in reports) and all(i['status']=='done' for i in items)
                write(path, current)
                failed = sum(i['status'] == 'failed' for i in items)
                attention=bool(any(i['status'] != 'done' for i in items) or pricing_error or missing_evidence or any(r.get('missing_count') for r in reports))
                quality_count=len(((current.get('evidence') or {}).get('image_review') or {}).get('source_quality_review',{}).get('items') or [])
                from .auto_processing_v3 import input_requests
                save(status='done', stage='处理阶段结束，有缺项待补' if attention else '本地处理完成，待发布核对', progress=100,
                    message=f'{len(items) - failed}/{len(items)} 个 SKU 完成；失败 {failed} 个。公共原图质量记录 {quality_count} 张；逐SKU原图核对 {sum(len(r.get("items") or []) for r in reports)} 张；未绑定不能算可用图库，生图与利润需人工确认',
                    items=items,input_requests=input_requests(items,missing_evidence), pricing_error=pricing_error, error=None,completion_state='NEEDS_REPAIR' if failed else 'NEEDS_INPUT' if attention else 'READY_FOR_FINAL_REVIEW')
            except InterruptedError as exc:
                latest = legacy.read_json(full_path)
                if latest.get('status') != 'paused': legacy._save_full_job(full_path, job, status='paused', message=str(exc),pause_reason='DATA_CHANGED')
            except Exception as exc:
                review=((locals().get('draft') or {}).get('evidence') or {}).get('classification_review') or {}
                if review.get('status')=='NEEDS_REVIEW':
                    check();write(path,draft)
                    legacy._save_full_job(full_path,job,status='done',completion_state='NEEDS_INPUT',stage='类目待核验，已保留其他产物',progress=100,error=None,category_error=str(exc)[:800],message=review.get('reason') or str(exc))
                else:legacy._save_full_job(full_path, job, status='failed', error=str(exc)[:800], message='本地处理未完成，请查看原因后重试')

    def start(name, drafts_dir=legacy.DRAFTS_DIR, root=legacy.ROOT):
        path = legacy._draft_path(name, drafts_dir)
        full_path = root / 'jobs' / (path.stem + '-full-ai-job.json')
        with START_LOCK:
            if full_path.exists() and legacy.read_json(full_path).get('status') in {'queued', 'running'}:
                raise legacy.EditorError('AI 处理已在队列中')
            now = datetime.now(timezone.utc).isoformat()
            job = {'status': 'queued', 'stage': '等待本地处理', 'progress': 0, 'draft': name,
                'message': '先筛原图，再逐 SKU 文案、属性和利润预算；不自动付费生图',
                'estimated_image_count': 0, 'created_at': now, 'updated_at': now, 'processing_version': VERSION,
                'publishing_performed': False, 'paid_image_calls': 0}
            write(full_path, job)
            threading.Thread(target=legacy._run_full_ai_flow, args=(path, root, full_path), daemon=True).start()
        return job
    legacy._run_full_ai_flow = run
    legacy.start_full_ai_flow = start
    previous_preflight = legacy.publish_preflight_issues
    def preflight(draft):
        issues = previous_preflight(draft)
        from .listing_brand_v3 import issues as brand_issues
        issues.extend(brand_issues(draft))
        from .product_match_v3 import blocking_issues
        issues.extend(blocking_issues(draft))
        from .procurement_v3 import image_scope_issues
        issues.extend(image_scope_issues(draft))
        for group in ((draft.get('evidence') or {}).get('image_review') or {}).get('sku_listing_galleries') or []:
            gallery=group.get('gallery') or []
            if gallery and all(i.get('source_listing_role')=='secondary' and i.get('review_status') not in {'HUMAN_SELECTED','APPROVED'} for i in gallery):
                issues.append(f"SKU {group.get('sku_id')}：现有候选均为辅助图，仍缺清晰主图；不能用兼容示意图代替主图")
        if (draft.get('evidence') or {}).get('processing_automation'):
            for sku in (draft.get('evidence') or {}).get('sku_details') or []:
                if (sku.get('ai_processing') or {}).get('status') != 'done':
                    issues.append(f"SKU {sku.get('seller_sku') or sku.get('sku_id')}：本轮文案与属性未完成，请重试该商品处理")
                content = sku.get('ai_content') or {}
                from .publish_service import _package_attributes
                from .attribute_context_v3 import meaningful
                submitted_attributes = (content.get('attributes') or []) + _package_attributes(sku)
                for field in (content.get('attribute_completion') or {}).get('required_ids') or []:
                    if not any(a.get('id') == field and (meaningful(a.get('value_id')) or meaningful(a.get('value_name'))) for a in submitted_attributes):
                        issues.append(f"SKU {sku.get('seller_sku') or sku.get('sku_id')}：必填属性 {field} 缺少依据")
        return list(dict.fromkeys(issues))
    legacy.publish_preflight_issues = preflight
