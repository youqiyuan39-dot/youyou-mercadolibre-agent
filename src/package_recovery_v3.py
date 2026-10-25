"""Recover only explicitly labelled packaging facts; bounded local OCR, no estimates."""
import base64,copy,json,re

def extract(text):
    from decimal import Decimal
    result={}
    pattern=r'(?:包装尺寸|含包装尺寸|package\s*(?:size|dimensions))\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米)\b?'
    # The final boundary is deliberately omitted for Chinese unit labels.
    pattern=pattern.replace(r'\b?', '')
    matches=list(re.finditer(pattern,str(text),re.I))
    dims=[]
    for m in matches:
        scale={'mm':Decimal('.1'),'毫米':Decimal('.1'),'cm':Decimal(1),'厘米':Decimal(1),'m':Decimal(100)}[m.group(4).lower()]
        values=tuple(float(Decimal(m.group(i))*scale) for i in (1,2,3))
        if min(values)>0:dims.append(values)
    if dims and len(set(dims))==1:result.update(zip(('length_cm','width_cm','height_cm'),dims[0]))
    weights=[]
    for m in re.finditer(r'(?:包装重量|含包装重(?:量)?|毛重|packed\s*weight|package\s*weight)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(kg|g|千克|公斤|克)',str(text),re.I):
        value=Decimal(m.group(1))*(1000 if m.group(2).lower() in ('kg','千克','公斤') else 1)
        if value>0:weights.append(float(value))
    if weights and len(set(weights))==1:result['weight_g']=weights[0]
    return result

def inspect(root,image):
    from .publish_service import _image_bytes
    from .local_model_runtime import responses
    from .package_ai_pipeline import PACKAGE_SCHEMA,INSTRUCTIONS
    from .source_image_resolution_v3 import recover_image
    resolved,resolution=recover_image(root,image)
    data,_,mime=_image_bytes(resolved,root)
    if len(data)>10000000 or mime not in ('image/jpeg','image/png','image/webp'):raise ValueError('包装证据图片不可读')
    request={'instructions':INSTRUCTIONS+' Quote the exact printed PACKAGE SIZE / PACKED WEIGHT or 包装尺寸 / 包装重量 in evidence, preserving any SKU/style label or explicit all-variant scope in that same quote. Never use product size or net weight. Unknown means null.',
      'input':[{'role':'user','content':[{'type':'input_image','image_url':'data:'+mime+';base64,'+base64.b64encode(data).decode()}]}],
      'text':{'format':{'type':'json_schema','name':'packaging_evidence','strict':True,'schema':PACKAGE_SCHEMA}},'max_output_tokens':450}
    response=responses(root,request)
    result=json.loads(''.join(p.get('text','') for o in response.get('output') or [] for p in o.get('content') or []))
    result['source_resolution']=resolution
    parsed=extract('\n'.join(str(x) for x in result.get('evidence') or []))
    dims=('length_cm','width_cm','height_cm')
    # OCR may call the longest edge "length". Preserve the printed axis order
    # only when both readings agree on all three measurements.
    if all(result.get(k) is not None and parsed.get(k) is not None for k in dims) and sorted(result[k] for k in dims)==sorted(parsed[k] for k in dims):
        result.update({k:parsed[k] for k in dims})
    return result

def scoped_quote(text,sku,skus):
    """A common parcel needs an explicit all-variant statement, not a common product size."""
    if len(skus)==1:return True
    if re.search(r'(?:全部|所有|各个|各)\s*(?:SKU|款式|规格|花色).{0,16}(?:包装|包裹)|(?:same|all).{0,20}(?:variants|skus).{0,20}packag',text,re.I):return True
    parts=lambda row:[p.strip() for p in str(row.get('variant') or '').split(' / ') if p.strip()]
    tokens=[t for t in parts(sku) if sum(t in parts(s) for s in skus)==1]
    tokens += [str(sku[k]) for k in ('sku_id','spec_id') if sku.get(k)]
    return any(re.search(r'(?<![A-Za-z0-9])'+re.escape(t)+r'(?![A-Za-z0-9])',text,re.I) for t in tokens)

def recover(root,draft,skus,inspector=inspect,check=lambda:None):
    ev=draft.setdefault('evidence',{});supplier=ev.get('supplier_page') or {};review=ev.get('image_review') or {}
    from .task_evidence_v3 import digest
    source_signature=digest({'version':'PACKAGE_RECOVERY_ORIGINAL_RESOLUTION_V2','supplier':supplier,'source_capture':ev.get('source_capture'),'skus':[(s.get('sku_id'),s.get('variant')) for s in skus],
        'details':review.get('detail_gallery'),'bound':review.get('sku_source_galleries')})
    old=ev.get('package_recovery') or {}
    cache={r['image']:r['result'] for r in old.get('image_inspections') or [] if old.get('source_signature')==source_signature and not (r.get('result') or {}).get('error')}
    reports=[];observations=[];new_checks=0
    from .supplier_facts_v3 import normalize_attributes
    attributes=normalize_attributes(supplier.get('attributes') or {})
    for sku in skus:
        pack=sku.setdefault('package',{}) or {};sku['package']=pack
        keys=('weight_g','length_cm','width_cm','height_cm');missing=[k for k in keys if not pack.get(k) or (pack.get('field_status') or {}).get(k)=='WEIGHT_TYPE_UNCONFIRMED']
        locks=(draft.get('edit_metadata') or {}).get('field_locks') or {}
        if str(sku.get('sku_id'))==str(ev.get('current_pricing_sku_id')):
            missing=[k for k in missing if not locks.get('weight' if k=='weight_g' else 'dimensions')]
        if not missing:continue
        old_status=pack.get('status') or 'UNVERIFIED'
        for key in keys:
            if pack.get(key):pack.setdefault('field_status',{}).setdefault(key,old_status)
        found={};evidence=[];errors=[]
        texts=[str(k)+': '+str(v) for k,v in attributes.items()]+str(supplier.get('description') or '').splitlines()
        for text in texts:
            parsed=extract(text)
            if parsed and scoped_quote(text,sku,skus):
                for key,value in parsed.items():
                    if key in found and found[key]!=value:found.pop(key);errors.append(key+' 包装证据冲突')
                    elif key+' 包装证据冲突' not in errors:found[key]=value
                evidence.append({'quoted_text':text[:700],'scope':'explicit_sku_or_all_variants'})
            elif parsed and not any(o.get('quoted_text')==text[:700] for o in observations):
                observations.append({'quoted_text':text[:700],'status':'SKU_SCOPE_UNCONFIRMED','facts':parsed})
        bound=next((g.get('gallery') or [] for g in review.get('sku_source_galleries') or [] if str(g.get('sku_id'))==str(sku.get('sku_id'))),[])
        details=review.get('detail_gallery') or []
        # Supplier packaging panels commonly occur at the end. Scan every retained
        # panel within the explicit task budget; cache shared panels across SKUs.
        images=bound+details[-4:]+details[:-4]
        for image in images[:20] if any(k not in found for k in missing) else []:
            check()
            try:
                ref=image.get('local_file') or image.get('remote_url')
                if ref not in cache:
                    new_checks+=1
                    try:cache[ref]=inspector(root,image)
                    except Exception as exc:cache[ref]={'error':str(exc)[:180]}
                result=cache[ref]
                if result.get('error'):raise ValueError(result['error'])
                if result.get('confidence')!='high':continue
                quoted='\n'.join(str(x) for x in result.get('evidence') or [])
                parsed=extract(quoted)
                if len(skus)>1 and image not in bound and not scoped_quote(quoted,sku,skus):
                    if parsed and not any(o.get('image')==ref for o in observations):observations.append({'image':ref,'quoted_text':quoted[:700],'status':'SKU_SCOPE_UNCONFIRMED','facts':parsed})
                    continue
                for key,value in parsed.items():
                    if result.get(key)==value:
                        if key in found and found[key]!=value:found.pop(key);errors.append(key+' 包装证据冲突')
                        elif key+' 包装证据冲突' not in errors:found[key]=value
                if parsed:evidence.append({'image':image.get('local_file') or image.get('remote_url'),'quoted_text':quoted[:700]})
            except Exception as exc:errors.append(str(exc)[:180])
        for key in missing:
            if key in found:pack[key]=found[key];pack.setdefault('field_status',{})[key]='SUPPLIER_PACKAGING_INFO'
        if found:pack.update(status='SUPPLIER_PACKAGING_INFO',source_method='LABEL_EXTRACTION_NOT_MEASUREMENT',evidence=evidence)
        reports.append({'sku_id':str(sku.get('sku_id')),'recovered':{k:found[k] for k in missing if k in found},'missing':[k for k in keys if not pack.get(k)],'errors':errors,'guessed':False})
    ev['package_recovery']={'items':reports,'paid_api_used':False,'method':'explicit_packaging_labels_and_sku_scope',
        'source_signature':source_signature,'image_inspections':[{'image':ref,'result':value} for ref,value in cache.items()],
        'unique_images_checked':new_checks,'reused_image_checks':len(cache)-new_checks,'unassigned_evidence':observations,
        'missing_action':'当前采集未取得包装三边，先重新读取对应SKU包装表/详情；未完成真实页面复核前不能断言供应商没有提供。展开尺寸不得代填'}
    return reports
