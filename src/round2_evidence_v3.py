"""Exact-source recovery and explicit user policy, kept outside supplier facts."""
import copy,json,re

VERSION='EVIDENCE_RECOVERY_20261018_V3'

def recover(root,draft):
    from .task_evidence_v3 import source_for,digest
    from .supplier_facts_v3 import normalize_attributes
    capture,path=source_for(root,draft)
    ev=draft.setdefault('evidence',{});review=ev.setdefault('image_review',{})
    skus=ev.get('sku_details') or [];actions=[]
    policy_file=root/'config/attribute_policy.json'
    policy=json.loads(policy_file.read_text('utf-8')) if policy_file.is_file() else {}
    attrs=normalize_attributes(capture.get('attributes') or {})
    brand=str(attrs.get('品牌') or '').strip()
    explicit_brand=bool(brand and brand.casefold() not in {'其他','其它','other','未知','未提供','未标注','品牌','不详','none','unknown','n/a'} and not re.search(r'待确认|咨询客服',brand))
    groups={str(g.get('sku_id')):g for g in review.get('sku_source_galleries') or []}
    raw_sets=list(capture.get('sku_image_sets') or [])+list(capture.get('sku_image_evidence') or [])
    for sku in skus:
        sid=str(sku.get('sku_id'));variant=str(sku.get('variant') or '')
        tokens=[t.strip() for t in variant.split(' / ') if t.strip()]
        exclusive=[t for t in tokens if sum(t in str(s.get('variant') or '').split(' / ') for s in skus)==1]
        matches=[]
        for row in raw_sets:
            if not isinstance(row,dict):continue
            owner=str(row.get('sku_id') or '')
            spec=str(row.get('spec_id') or '')
            label=str(row.get('variant') or row.get('label') or '')
            # Explicit ownership takes precedence; a conflicting ID cannot fall back to a label.
            if owner:
                matched=owner==sid and (not spec or spec==str(sku.get('spec_id') or ''))
            elif spec:
                matched=spec==str(sku.get('spec_id') or '') and sum(str(s.get('spec_id') or '')==spec for s in skus)==1
            else:
                matched=bool(label and ((label==variant and sum(str(s.get('variant') or '')==label for s in skus)==1) or label in exclusive))
            if matched:
                for url in row.get('images') or [row.get('url')]:
                    if isinstance(url,str) and url.startswith('https://') and url not in matches:matches.append(url)
        if matches and not (groups.get(sid) or {}).get('gallery'):
            groups[sid]={'sku_id':sid,'variant':variant,'gallery':[{'remote_url':u,'image_type':'sku','sku_id':sid,'review_status':'UNREVIEWED','binding_source':path,'binding_method':'EXACT_CAPTURE_LABEL'} for u in matches]}
            actions.append({'sku_id':sid,'field':'source_images','count':len(matches),'source':path,'method':'EXACT_CAPTURE_LABEL'})
        # A dimension written in the selected option describes the item, never the parcel.
        m=re.search(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(cm|mm|厘米|毫米)?\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(cm|mm|厘米|毫米)(?![A-Za-z])',variant)
        if m and not re.search(r'包装|包裹|装箱',variant):
            unit=m[4];scale=.1 if unit in ('mm','毫米') else 1
            if not m[2] or m[2]==unit:
                sku['product_measurements']={'dimensions_cm':[float(m[1])*scale,float(m[3])*scale], 'dimension_type':'PRODUCT_OPTION_SIZE','origin':'SUPPLIER_CAPTURE','source':path,'quote':variant,'sku_id':sid}
        raw_sku=next((s for s in capture.get('skus') or [] if str(s.get('sku_id'))==sid),{})
        raw_pack=raw_sku.get('package') or {};pack=sku.get('package') or {}
        if raw_pack.get('weight_g') is not None:
            weight_context=str((capture.get('package') or {}).get('evidence') or '')+' '+str(raw_pack.get('evidence') or '')+' '+str(raw_pack.get('source_label') or '')
            if 'visible_package_table' in weight_context and not re.search(r'毛重|包装重量|含包装|1688_packaging_table',weight_context):
                sku['supplier_weight_observation']={'value':raw_pack['weight_g'],'unit':'g','weight_type':'UNSPECIFIED','origin':'SUPPLIER_CAPTURE','source':path,'quote':'重量(g): '+str(raw_pack['weight_g']),'sku_id':sid}
                locks=(draft.get('edit_metadata') or {}).get('field_locks') or {}
                confirmed=str(pack.get('status') or '').upper() in {'USER_CONFIRMED','HUMAN_CONFIRMED','VERIFIED','MEASURED_CONFIRMED'}
                if pack.get('weight_g')==raw_pack['weight_g'] and not confirmed and not (sid==str(ev.get('current_pricing_sku_id')) and locks.get('weight')):
                    pack.setdefault('field_status',{})['weight_g']='WEIGHT_TYPE_UNCONFIRMED'
                    if sid==str(ev.get('current_pricing_sku_id')) and (ev.get('packed_weight') or {}).get('value')==raw_pack['weight_g']:
                        ev['packed_weight']['status']='WEIGHT_TYPE_UNCONFIRMED'
        # This is a user default, not a statement made by the supplier.
        sku.pop('user_attribute_policy',None)
        if (not explicit_brand or policy.get('brand_mode')=='STRICT_GENERIC') and policy.get('brand_default') and policy.get('origin')=='USER_BRAND_POLICY':
            sku['user_attribute_policy']={'BRAND':{'value_name':policy['brand_default'],'origin':'USER_BRAND_POLICY','version':policy.get('version'),'enforcement':'STRICT' if policy.get('brand_mode')=='STRICT_GENERIC' else 'DEFAULT','category_check':'STRING_DEFINITION_REQUIRED'}}
    from .listing_brand_v3 import apply as apply_brand
    apply_brand(draft,root)
    from .model_resolution_v3 import refresh as refresh_model
    refresh_model(root,draft)
    review['sku_source_galleries']=list(groups.values())
    ev['round2_evidence']={'version':VERSION,'actions':actions,'source_signature':digest(capture),'page_access':'SAVED_CAPTURE_ONLY','source_path':path}
    return actions

def candidate_attributes(supplier,definitions):
    from .supplier_facts_v3 import literal_attributes
    from .attribute_context_v3 import valid_value
    from .attribute_context_v3 import TRANSLATIONS
    rows=literal_attributes(supplier,definitions);by={r['id']:r for r in rows}
    from .model_resolution_v3 import resolve as resolve_model
    model=resolve_model(supplier)
    by.pop('MODEL',None)
    from .attribute_context_v3 import selected_color,normalize_enum,explicit_hand_saw_type
    from .supplier_facts_v3 import normalize_attributes
    attrs=normalize_attributes(supplier.get('attributes') or {})
    extra={'COLOR':selected_color(supplier),'MAIN_COLOR':selected_color(supplier),
           'PATTERN_NAME':attrs.get('图案'),'ALPHANUMERIC_MODEL':attrs.get('型号'),
           'HAND_SAW_TYPE':explicit_hand_saw_type(supplier)}
    if model.get('status') in {'EXPLICIT','CANDIDATE'}:extra['MODEL']=model['value_name']
    if extra['PATTERN_NAME']=='十字架/皇冠/罗马数字':extra['PATTERN_NAME']='Cross/Crown/Roman Numerals'
    power=str(attrs.get('额定功率') or '').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:W|瓦)?',power,re.I):extra['HEATER_MAX_POWER']=re.sub(r'\s*(?:W|瓦)$','',power,flags=re.I).strip()+' W'
    for definition in definitions:
        key=definition.get('id');p=(supplier.get('user_attribute_policy') or {}).get(key) or {}
        if extra.get(key):
            row=normalize_enum({'id':key,'value_id':None,'value_name':extra[key]},definition)
            match=next((v for v in definition.get('values') or [] if str(v.get('name') or '').casefold()==str(extra[key]).casefold()),None)
            if match:row.update(value_id=str(match['id']),value_name=match['name'])
            if valid_value(row,definition):by[key]=row
        if key=='MATERIAL' and key in by:
            raw=by[key]['value_name'];names={raw.casefold(),*(v.casefold() for v in TRANSLATIONS.get(raw,()))}
            matches=[v for v in definition.get('values') or [] if str(v.get('name') or '').casefold() in names]
            if len(matches)==1:by[key]={'id':key,'value_id':str(matches[0]['id']),'value_name':matches[0]['name']}
        if key=='BRAND' and (key not in by or p.get('enforcement')=='STRICT') and p.get('origin')=='USER_BRAND_POLICY' and definition.get('value_type')=='string':
            row={'id':key,'value_id':None,'value_name':p.get('value_name')}
            if valid_value(row,definition):by[key]=row
    return list(by.values())
