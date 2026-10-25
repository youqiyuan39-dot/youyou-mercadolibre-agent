"""Task-scoped source recovery. No generated output may become supplier evidence."""
import copy,hashlib,json,math,re
VERSION='TASK_EVIDENCE_20261017_V2_PACKAGING_TABLE'
FIELDS=('weight_g','length_cm','width_cm','height_cm')
MATERIALS=('PA+30GF','合金钢','不锈钢','聚酯纤维','聚氯乙烯','聚丙烯','工具钢','橡胶','涤纶','塑料','合金','碳钢','钢','铁','棉','纸')

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def positive(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0

def source_for(root,draft):
    relative=(draft.get('evidence') or {}).get('source_capture');path=root/str(relative or '')
    if relative and path.is_file() and path.resolve().is_relative_to(root.resolve()):
        return json.loads(path.read_text('utf-8')),str(relative)
    return {},None

def selected_fields(capture,sku):
    from .supplier_facts_v3 import normalize_attributes
    attrs=normalize_attributes(capture.get('attributes') or {})
    row=str(attrs.get(str(sku.get('variant') or '')) or '')
    header=str(attrs.get('产品规格') or '')
    recovered={};quotes={}
    if '材质' in header and row and not re.search(r'非金属|不含|未标|未知',row):
        material=next((m for m in MATERIALS if re.search(r'(?<![A-Za-z])'+re.escape(m)+r'(?![A-Za-z])',row,re.I)),None)
        if material:recovered['材质']=material;quotes['材质']={'path':'/attributes/'+str(sku.get('variant')),'quote':row,'header':header}
    # Flattened DOM rows often contain an unspaced price+inventory followed by clean price and inventory.
    # Recover only an explicit last currency price, never a bare integer or minimum tier price.
    money=list(re.finditer(r'[¥￥]\s*(\d+(?:\.\d{1,2})?)(?=\s|$)',row))
    cost=float(money[-1][1]) if money else None
    if cost and cost>0:quotes['purchase_cost_cny']={'path':'/attributes/'+str(sku.get('variant')),'quote':row}
    for key in ('品牌','型号','制造商零件编号'):
        value=attrs.get(key)
        if not value or re.search(r'待确认|未知|未标|咨询|其他|其它|\bother\b',str(value),re.I):continue
        if key=='品牌' and str(value).strip() in ('无','无品牌'):value='Generic'
        recovered[key]=value;quotes[key]={'path':'/attributes/'+key,'quote':str(attrs[key])}
    return recovered,cost,quotes

def restore(root,draft):
    """Repair missing materialized facts from the exact source SKU; preserve user values."""
    capture,path=source_for(root,draft);ev=draft.setdefault('evidence',{});actions=[]
    if not capture:
        ev['task_evidence']={'version':VERSION,'state':'NO_CAPTURE','actions':[],'source_path':path};return []
    from .source_intake import build_processing_draft
    fresh=build_processing_draft(capture,root/path,root)
    raw={str(s.get('sku_id')):s for s in fresh['evidence']['supplier_page'].get('skus') or [] if s.get('sku_id')}
    locks=(draft.get('edit_metadata') or {}).get('field_locks') or {}
    ev['package_table_resolution']=copy.deepcopy(fresh['evidence'].get('package_table_resolution') or {})
    current=ev.get('sku_details') or []
    if not current and raw:
        # Restore the recorded collection scope. Do not re-add variants deliberately removed by an editor.
        if (draft.get('edit_metadata') or {}).get('sku_selection_confirmed'):
            ev['task_evidence']={'version':VERSION,'state':'HUMAN_SELECTION_EMPTY','actions':[],'source_path':path};return []
        current=copy.deepcopy(fresh['evidence']['sku_details']);ev['sku_details']=current
        if not ev.get('current_pricing_sku_id'):ev['current_pricing_sku_id']=current[0]['sku_id']
        if not locks.get('seller_sku'):draft.setdefault('payload',{})['seller_sku']=current[0]['seller_sku']
        for sku in current:
            if not positive((raw.get(str(sku['sku_id'])) or {}).get('price')):sku['purchase_cost_cny']=None
            if str(sku['sku_id'])==str(ev.get('current_pricing_sku_id')):
                for key in FIELDS:
                    if locks.get('weight' if key=='weight_g' else 'dimensions'):
                        value=(ev.get('packed_weight') or {}).get('value') if key=='weight_g' else (ev.get('packed_dimensions') or {}).get(key.split('_')[0])
                        sku['package'][key]=value;sku['package'].setdefault('field_status',{})[key]='HUMAN_LOCKED'
                        sku['package'].setdefault('field_sources',{})[key]=[{'path':'/evidence/packed_weight' if key=='weight_g' else '/evidence/packed_dimensions','method':'HUMAN_FIELD_LOCK','value':value}]
        single=fresh['evidence']['package_table_resolution'].get('single_offer_materialized')
        actions.append({'code':'MATERIALIZE_SINGLE_OFFER' if single else 'RESTORE_CAPTURE_SKUS','count':len(current),'source_path':path+('/offer_flags/isSkuOffer' if single else '/skus')})
    locks=(draft.get('edit_metadata') or {}).get('field_locks') or {}
    for sku in current:
        sid=str(sku.get('sku_id'));source=raw.get(sid)
        if not source:continue
        pack=sku.setdefault('package',{}) or {};sku['package']=pack
        source_pack=source.get('package') or {}
        if not all(positive(source_pack.get(k)) for k in FIELDS) and not (ev['package_table_resolution'].get('conflicts')):
            candidates=[r.get('package') or {} for r in capture.get('package_rows') or [] if (r.get('sku_id') and str(r['sku_id'])==sid) or (r.get('spec_id') and r['spec_id']==source.get('spec_id'))]
            unique={digest(p):p for p in candidates}
            if len(unique)==1:source_pack={**next(iter(unique.values())),**{k:v for k,v in source_pack.items() if positive(v)}}
            elif len(unique)>1:actions.append({'code':'SOURCE_CONFLICT','sku_id':sid,'field':'package','source_path':path+'/package_rows'})
        for key in FIELDS:
            if positive(pack.get(key)):
                if pack.get(key)==source_pack.get(key) and not (pack.get('field_status') or {}).get(key) and str(pack.get('status') or '').upper() not in {'USER_CONFIRMED','HUMAN_CONFIRMED','VERIFIED','MEASURED_CONFIRMED'}:
                    pack.setdefault('field_status',{})[key]='SUPPLIER_PACKAGING_INFO'
                    actions.append({'code':'RESTORE_CAPTURE_PROVENANCE','sku_id':sid,'field':'package.'+key,'source_path':path+'/skus/'+sid+'/package/'+key})
                continue
            if sid==str(ev.get('current_pricing_sku_id')) and locks.get('weight' if key=='weight_g' else 'dimensions'):continue
            if positive(source_pack.get(key)):
                pack[key]=source_pack[key]
                pack.setdefault('field_status',{})[key]='SUPPLIER_PACKAGING_INFO'
                source_rows=(source_pack.get('field_sources') or {}).get(key) or []
                if source_rows:pack.setdefault('field_sources',{})[key]=copy.deepcopy(source_rows)
                actions.append({'code':'RESTORE_CAPTURE_FIELD','sku_id':sid,'field':'package.'+key,'source_path':path+(source_rows[0]['path'] if source_rows else '/skus/'+sid+'/package/'+key)})
        fields,cost,quotes=selected_fields(capture,source)
        if not positive(sku.get('purchase_cost_cny')):
            cost=source.get('price') if positive(source.get('price')) else cost
            if positive(cost):
                sku['purchase_cost_cny']=cost
                actions.append({'code':'RESTORE_CAPTURE_FIELD','sku_id':sid,'field':'purchase_cost_cny','source_path':path+('/skus/'+sid+'/price' if positive(source.get('price')) else '/attributes/'+source.get('variant',''))})
        old_fields=(sku.get('supplier_facts') or {}).get('attributes') or {}
        for key,value in fields.items():
            if old_fields.get(key)!=value:actions.append({'code':'RECOVER_SPECIFIC_ATTRIBUTE','sku_id':sid,'field':key,'source_path':path+(quotes.get(key) or {}).get('path',''),'quote':(quotes.get(key) or {}).get('quote')})
        sku['supplier_facts']={'origin':'SUPPLIER_CAPTURE','source_sha256':digest(capture),'attributes':fields,'source_quotes':quotes}
    review=ev.setdefault('image_review',{});existing={str(g.get('sku_id')):g for g in review.get('sku_source_galleries') or []}
    for group in fresh['evidence']['image_review'].get('sku_source_galleries') or []:
        sid=str(group.get('sku_id'))
        if sid in {str(s.get('sku_id')) for s in current} and group.get('gallery') and not (existing.get(sid) or {}).get('gallery'):
            existing[sid]=copy.deepcopy(group);actions.append({'code':'RESTORE_CAPTURE_BINDING','sku_id':sid,'source_path':path+'/sku_image_sets'})
    review['sku_source_galleries']=list(existing.values())
    # Restore common title/attributes in the evidence compartment only.
    supplier=ev.setdefault('supplier_page',{})
    supplier.update(title=capture.get('title'),attributes=copy.deepcopy(capture.get('attributes') or {}))
    if not supplier.get('skus') and fresh['evidence']['package_table_resolution'].get('single_offer_materialized'):
        supplier['skus']=copy.deepcopy(fresh['evidence']['supplier_page']['skus'])
        supplier['sku_details']=copy.deepcopy(current)
        supplier['current_pricing_sku_id']=ev.get('current_pricing_sku_id')
        supplier['source_sku_count']=0
        actions.append({'code':'MATERIALIZE_SINGLE_OFFER_IDENTITY','source_path':path+'/offer_flags/isSkuOffer','supplier_sku_invented':False})
    selected=next((s for s in current if str(s.get('sku_id'))==str(ev.get('current_pricing_sku_id'))),None)
    if selected:
        pack=selected.get('package') or {}
        human=lambda value:any(k in str((value or {}).get('status') or '').upper() for k in ('USER','MANUAL','MEASURED','HUMAN','VERIFIED'))
        if not locks.get('weight') and not human(ev.get('packed_weight')) and positive(pack.get('weight_g')):
            ev['packed_weight']={'value':pack['weight_g'],'unit':'g','status':pack.get('field_status',{}).get('weight_g') or pack.get('status') or 'SUPPLIER_PACKAGING_INFO','field_sources':copy.deepcopy(pack.get('field_sources',{}).get('weight_g') or [])}
        if not locks.get('dimensions') and not human(ev.get('packed_dimensions')) and all(positive(pack.get(k)) for k in FIELDS[1:]):
            ev['packed_dimensions']={k.split('_')[0]:pack[k] for k in FIELDS[1:]}
            ev['packed_dimensions'].update(unit='cm',status=pack.get('status') or 'SUPPLIER_PACKAGING_INFO',field_sources={k:copy.deepcopy(pack.get('field_sources',{}).get(k) or []) for k in FIELDS[1:]})
        if all(positive(pack.get(k)) for k in FIELDS):ev['package_ai']={'status':'NOT_NEEDED_SUPPLIER_EVIDENCE','human_review_required':True}
    ev['source_title']=capture.get('title')
    ev['task_evidence']={'version':VERSION,'source_path':path,'source_sha256':digest(capture),'actions':actions,'state':'RECOVERED' if actions else 'CHECKED_NO_LOSS'}
    return actions

def issues(draft):
    from .attribute_context_v3 import meaningful
    ev=draft.get('evidence') or {};review=ev.get('image_review') or {};rows=[]
    bound={str(g.get('sku_id')) for g in review.get('sku_source_galleries') or [] if g.get('gallery')}
    bound.update(str(g.get('sku_id')) for g in review.get('sku_listing_galleries') or [] if any(i.get('review_status') in ('HUMAN_SELECTED','APPROVED') for i in g.get('gallery') or []))
    for sku in ev.get('sku_details') or []:
        sid=str(sku.get('sku_id'));pack=sku.get('package') or {}
        processing=sku.get('ai_processing') or {}
        if processing.get('status')=='failed':
            rows.append({'sku_id':sid,'field':'ai_content','cause':processing.get('cause') or 'GENERATION_OR_VALIDATION_FAILED','next_action':'CHECK_FAILED_MODEL_OUTPUT','required_evidence':'查看具体校验反馈；这是生成步骤失败，不等于供应商缺料','error':processing.get('error')})
        for key in FIELDS:
            if not positive(pack.get(key)) or (pack.get('field_status') or {}).get(key)=='WEIGHT_TYPE_UNCONFIRMED':
                ambiguous=(pack.get('field_status') or {}).get(key)=='WEIGHT_TYPE_UNCONFIRMED'
                rows.append({'sku_id':sid,'field':'package.'+key,'cause':'WEIGHT_TYPE_UNCONFIRMED' if ambiguous else 'CAPTURE_GAP','next_action':'CONFIRM_PACKED_WEIGHT_TYPE' if ambiguous else 'RECOLLECT_EXACT_SKU_PACKAGING','observed_value':pack.get(key),'required_evidence':'已采到重量，需确认是否含包装；不要求重复提供同一个数值' if ambiguous else '明确标为包装/包裹的三边或重量；商品规格尺寸不能代填'})
        if len(ev.get('sku_details') or [])>1 and sid not in bound:
            rows.append({'sku_id':sid,'field':'source_images','cause':'CAPTURE_GAP','next_action':'RECOLLECT_SKU_IMAGE_BINDING','required_evidence':'SKU或款式标签与原图URL的明确对应；不能按图片顺序猜配'})
        content=sku.get('ai_content') or {};filled={a.get('id') for a in content.get('attributes') or [] if meaningful(a.get('value_name')) or meaningful(a.get('value_id'))}
        for key in (content.get('attribute_completion') or {}).get('required_ids') or []:
            if key.startswith('PACKAGE_') or key in filled:continue
            if key=='MODEL':
                resolution=sku.get('model_resolution') or {}
                rows.append({'sku_id':sid,'field':key,'cause':'MODEL_IDENTIFIER_CONFLICT' if resolution.get('status')=='CONFLICT' else 'SOURCE_FIELD_UNAVAILABLE',
                    'next_action':'CHECK_SOURCE_IDENTIFIERS' if resolution.get('status')=='CONFLICT' else 'PROVIDE_MODEL_OR_TRACEABLE_IDENTIFIER',
                    'required_evidence':resolution.get('reason') or '明确型号，或可追溯的供应商货号/所选规格；无依据时留空，不从知识库案例猜值'})
                continue
            rows.append({'sku_id':sid,'field':key,'cause':'SOURCE_FIELD_UNAVAILABLE','next_action':'RECOLLECT_SUPPLIER_ATTRIBUTE','required_evidence':'供应商明确填写的'+key+'；知识库和已有AI值不能作为依据'})
    return rows
