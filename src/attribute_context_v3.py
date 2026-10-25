"""Bounded attribute batches; source data is not an instruction or verified AI copy."""
import copy,json
import re
from .product_core_v3 import clean_description
POLICY_VERSION='ATTR_TYPED_EVIDENCE_20261019_V8_MODEL_CANDIDATES'
SEMANTIC_VERSION='ATTR_SEMANTIC_20261019_V5_MODEL_CANDIDATES'

RISK_KEYS={'BRAND':('品牌','brand'),'MATERIAL':('材质','材料','material'),'MODEL':('型号','model'),'MPN':('厂商型号','制造商零件编号','manufacturer part number','mpn')}
RISK_KEYS.update({'IS_PERSONALIZED_PRODUCT':('是否个性化','是否定制','personalized','personalization'),
    'UNITS_PER_PACK':('每包装数量','包装内数量','units per pack'),'SALE_FORMAT':('销售形式','sale format')})
RISK_KEYS.update({'GAUGE':('槽宽','gauge'),'PITCH':('节距','pitch'),'RIVETS_NUMBER':('铆钉数量','rivets'),
    'CHAIN_LINKS_NUMBER':('链节数','链条节数','chain links'),'LINKS_NUMBER':('链节数','链条节数','chain links'),
    'INCLUDES_CHAIN':('包含链条','includes chain'),'WITH_INTERCHANGEABLE_TIP':('可更换头','interchangeable tip')})
RISK_KEYS.update({'LENGTH':('商品长度','产品长度','product length'),'WIDTH':('商品宽度','产品宽度','product width'),
    'HEIGHT':('商品高度','产品高度','product height'),'WEIGHT':('商品净重','产品净重','product net weight'),
    'SIZE':('商品尺寸','产品尺寸','尺寸','size')})
RISK_KEYS.update({'TOTAL_LENGTH':('商品总长度','整体长度','总长','total length','overall length'),
    'PENNANT_LENGTH':('单片旗长度','旗片长度','pennant length'),
    'PENNANT_WIDTH':('单片旗宽度','旗片宽度','pennant width'),
    'PENNANTS_NUMBER':('旗片数量','旗帜数量','pennants number','number of pennants'),
    'IS_SUITABLE_FOR_OUTDOORS':('适用于户外','适合室外','suitable for outdoors')})
TRANSLATIONS={'无品牌':('generic','genérico','no brand','unbranded'),'塑料':('plastic','plástico'),'聚酯纤维':('polyester',),'涤纶':('polyester',),'钢':('steel','acero'),'不锈钢':('stainless steel','acero inoxidable'),'铁':('iron','hierro'),'木':('wood','madera'),'金属':('metal',),'纸':('paper','papel'),'聚丙烯':('polypropylene','pp'),'聚氯乙烯':('pvc',),'棉':('cotton','algodón')}
RISK_KEYS.update({'COLOR':('颜色','color'),'MAIN_COLOR':('主色','颜色','main color'),
    'PATTERN_NAME':('图案','pattern'),'ALPHANUMERIC_MODEL':('型号','model'),
    'HEATER_MAX_POWER':('额定功率','最大功率','rated power'),
    'VOLTAGE':('额定电压','电压','voltage'),'LINE':('系列','产品系列','product line'),
    'ELECTRIC_HEATER_TYPE':('加热器类型','取暖器类型','electric heater type')})
for _key in ('ENERGY_EFFICIENCY_LABEL','ENERGY_EFFICIENCY','INMETRO_CERTIFICATION_REGISTRATION_NUMBER',
             'ELECTRICAL_SAFETY_CERTIFICATE_NUMBER','OCP_CERTIFICATION_AGENCY','SEC_STAMP',
             'ANATEL_HOMOLOGATION_LABEL','TELECOMMUNICATION_HOMOLOGATION_NUMBER'):
    RISK_KEYS[_key]=(_key.lower().replace('_',' '),)
RISK_KEYS['HAND_SAW_TYPE']=('手锯类型','hand saw type')
COLORS={'深紫色':'Deep Purple','黑色':'Black','白色':'White','红色':'Red','紫色':'Purple','粉色':'Pink','蓝色':'Blue'}
TRANSLATIONS.update({k:(v.casefold(),) for k,v in COLORS.items()})
TRANSLATIONS['十字架/皇冠/罗马数字']=('cross/crown/roman numerals',)

def selected_color(supplier):
    variant=str(supplier.get('selected_variant') or (supplier.get('selected_sku') or {}).get('variant') or '')
    # Longest label first: 深紫色 is not 紫色. Never choose from a family-wide color list.
    for label,name in COLORS.items():
        if label in variant:return name
    return None

def explicit_hand_saw_type(supplier):
    # Map only the original supplier identity, never generated copy or packaging.
    from .category_resolution_v3 import intent_for
    title=str(supplier.get('title') or supplier.get('supplier_title') or '')
    intent=intent_for({'source_title':title})
    if (intent or {}).get('kind')!='manual_pull_saw':return None
    if re.search(r'链条锯|链锯|\bchain\s*saw\b',title,re.I):return 'Manual chain saw'
    if re.search(r'钢丝锯|\bwire\s+saw\b',title,re.I):return 'Wire saw'
    if re.search(r'绳锯|手拉锯',title):return 'Pull saw'
    return None


def source_value_supported(row,supplier,definition=None):
    key=str(row.get('id') or '')
    aliases=RISK_KEYS.get(key) or (key.casefold().replace('_',' '),str((definition or {}).get('name') or '').casefold())
    from .supplier_facts_v3 import normalize_attributes
    attrs=normalize_attributes(supplier.get('attributes') or {})
    if not isinstance(attrs,dict):return False
    value=str(row.get('value_name') or '').strip().casefold()
    if not meaningful(value):return False
    if key=='MODEL':
        from .model_resolution_v3 import resolve
        model=resolve(supplier)
        return model.get('status') in {'EXPLICIT','CANDIDATE'} and value==str(model.get('value_name') or '').casefold()
    policy=(supplier.get('user_attribute_policy') or {}).get(key) or {}
    if key=='BRAND' and policy.get('origin')=='USER_BRAND_POLICY' and policy.get('enforcement')=='STRICT':
        return value=='generic'
    if key=='BRAND' and policy.get('origin')=='USER_BRAND_POLICY' and value==str(policy.get('value_name') or '').casefold():
        return True
    variant=str(supplier.get('selected_variant') or (supplier.get('selected_sku') or {}).get('variant') or '')
    if key in {'COLOR','MAIN_COLOR'} and selected_color(supplier):
        return value==selected_color(supplier).casefold()
    if key=='HAND_SAW_TYPE' and explicit_hand_saw_type(supplier):
        return value==explicit_hand_saw_type(supplier).casefold()
    if key in {'LINKS_NUMBER','CHAIN_LINKS_NUMBER'}:
        match=re.search(r'(\d+)\s*(?:节|links?\b)',variant,re.I)
        if match and value==match[1]:return True
    if key=='IS_PERSONALIZED_PRODUCT':
        return any(any(word in str(label).lower() for word in RISK_KEYS[key]) and str(text).strip().lower() in ('否','no','false') and value in ('no','false','否') or any(word in str(label).lower() for word in RISK_KEYS[key]) and str(text).strip().lower() in ('是','yes','true') and value in ('yes','true','是') for label,text in attrs.items())
    for label,source in attrs.items():
        if not any(word and (word in str(label).casefold() if key in RISK_KEYS else word==str(label).casefold()) for word in aliases):continue
        text=str(source).strip().casefold()
        if not meaningful(text) or text==str(label).strip().casefold() or text in ('其他','其它','other','咨询客服'):continue
        if key=='BRAND' and text in ('无','无品牌') and value in ('generic','genérico','unbranded','no brand'):return True
        if re.search(r'未知|不详|未标|未填|不含|不是|非金属|非纯|仿制|\bnot\s|\bnon[- ]',text):continue
        if value==text or re.search(r'(?<![a-z0-9])'+re.escape(value)+r'(?![a-z0-9])',text):return True
        if key=='HEATER_MAX_POWER' and re.fullmatch(r'\d+(?:\.\d+)?\s*(?:w|瓦)?',text,re.I) and re.sub(r'\s*(?:w|瓦)$','',value,flags=re.I).strip()==re.sub(r'\s*(?:w|瓦)$','',text,flags=re.I).strip():return True
        for word,names in TRANSLATIONS.items():
            cleaned=re.sub(r'^(?:'+re.escape(str(label).casefold())+r'[:：\s]*)+','',text)
            cleaned=re.sub(r'[\s:：,，;；]','',cleaned)
            if re.fullmatch('(?:'+re.escape(word)+'){1,4}',cleaned) and value in names:return True
    return False

def supplier_from_requests(requests):
    supplier={};pieces={}
    for request in requests:
        context=json.loads(request['input'])
        if context.get('supplier'):supplier=context['supplier']
        for part in context.get('supplier_segments') or []:
            path=part.get('path','')
            if path.startswith('/attributes/'):
                pieces.setdefault(path,{})[part.get('offset',0)]=part.get('text','')
    if pieces:
        supplier={**supplier,'attributes':{path[len('/attributes/'):]:''.join(values[k] for k in sorted(values)) for path,values in pieces.items()}}
    return supplier

def compact_source(context):
    supplier=context.get('supplier') or {};skus=supplier.get('skus') or []
    # Use original supplier title, never the already-generated AI title, for product classification.
    source={k:copy.deepcopy(supplier[k]) for k in ('selected_variant','attributes','product_weight_g','product_length_cm','product_width_cm','product_height_cm','package_table_evidence','material_listed','application_listed') if k in supplier}
    source['description_excerpt']=clean_description(supplier.get('description',''))
    source['supplier_title'] = str(supplier.get('title') or '')
    source['description_truncated']=False
    if supplier.get('source_quotes'): source['source_quotes'] = copy.deepcopy(supplier['source_quotes'])
    if supplier.get('user_attribute_policy'):source['user_attribute_policy']=copy.deepcopy(supplier['user_attribute_policy'])
    for key in ('model_resolution','model_resolution_rules','model_selected_attributes','model_source_sku_count','model_identifier_observations'):
        if key in supplier:source[key]=copy.deepcopy(supplier[key])
    source['selected_sku']=copy.deepcopy(skus[0]) if len(skus)==1 else None
    source['sku_scope']='只有单SKU时提供该规格；多SKU未明确选择时不得使用其他规格值'
    return {'supplier':source,'ai_proposed_title_not_evidence':str((context.get('proposal') or {}).get('title') or '')[:60],
      'evidence_rule':'已生成文案和已有属性不是事实证明；缺依据留空，包装不是商品尺寸。省略资料不得推断。'}

def editable(definition):
    key=str(definition.get('id') or '')
    tags=definition.get('tags') or {}
    if isinstance(tags,list):tags={str(t):True for t in tags}
    return not (tags.get('read_only') or tags.get('hidden') or key.startswith(('PACKAGE_', 'SELLER_PACKAGE_')) or key in {'CATALOG_TITLE','PRODUCT_FEATURES','WITH_POSITIVE_IMPACT','AGID','SELLER_SKU','IEPS','IVA_FOR_RESALE','SAT_KEY','MEASURE_UNIT_KEY','MEASURE_UNIT_DESCRIPTION','INVOICE_PRODUCT_NAME','IMPORT_DECLARATION_NUMBER'})

def valid_value(row, definition):
    value=str(row.get('value_name') or '').strip()
    if not meaningful(value) and not meaningful(row.get('value_id')):return False
    if value.casefold() in {str(definition.get('name') or '').strip().casefold(),str(definition.get('id') or '').strip().casefold()}:return False
    if re.search(r'专供货源|铺货分销|价格\s*[|/]\s*库存|是否跨境',value):return False
    if definition.get('id')=='COLOR' and re.search(r'\d\s*(?:cm|mm|厘米|毫米)|颜色\s|重量',value,re.I):return False
    if definition.get('value_type')=='number' and not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',value):return False
    if definition.get('value_type')=='number_unit' and not re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*[A-Za-zµ°²³/一-龥]*',value):return False
    if definition.get('value_type')=='boolean' and value and value.casefold() not in {'yes','no','sí','si','sim','não','true','false','是','否'} and not row.get('value_id'):return False
    return True

def normalize_enum(row,definition):
    """An enum ID must name the same value, never merely be a valid ID."""
    result=copy.deepcopy(row)
    enums={str(v['id']):str(v.get('name') or '') for v in definition.get('values') or [] if v.get('id') is not None}
    key=str(result.get('value_id'));name=str(result.get('value_name') or '').strip().casefold()
    if key in enums:
        official=enums[key].strip().casefold()
        equivalent=name==official or any(name==word.casefold() and official in values or official==word.casefold() and name in values for word,values in TRANSLATIONS.items())
        if not name:result['value_name']=enums[key]
        elif not equivalent:result['value_id']=None
    else:result['value_id']=None
    return result

def batches(request):
    context=json.loads(request['input']);attributes=[a for a in context.get('official_attributes') or [] if editable(a)]
    base=compact_source(context);result=[];groups=[];group=[];size=0
    for attr in attributes:
        item={k:copy.deepcopy(attr[k]) for k in ('id','name','value_type','tags') if k in attr}
        values=attr.get('values') or attr.get('values_preview') or []
        if values:
            item['values']=[{k:v[k] for k in ('id','name') if k in v} for v in values[:8]]
            item['values_truncated']=len(values)>8
        cost=len(json.dumps(item,ensure_ascii=False))
        if group and (len(group)>=10 or size+cost>2500):groups.append(group);group=[];size=0
        if cost>2500:
            item={k:item[k] for k in ('id','name','value_type') if k in item};item['definition_truncated']=True
        group.append(item);size+=len(json.dumps(item,ensure_ascii=False))
    if group:groups.append(group)
    bases = [base]
    if len(json.dumps(base, ensure_ascii=False)) > 4500:
        from .context_segments_v3 import segments
        bases = [{'supplier_segments': chunk, 'evidence_rule': base['evidence_rule'],
                  'segment_number': i + 1} for i, chunk in enumerate(segments(base['supplier']))]
    for group, source_base in ((group, source_base) for group in groups or [[]] for source_base in bases):
        ids={a['id'] for a in group};body={**source_base,'official_attributes':group,
          'existing_attributes':[a for a in context.get('existing_attributes',[]) if a.get('id') in ids]}
        req=copy.deepcopy(request);req['input']=json.dumps(body,ensure_ascii=False)
        req['instructions']=str(req.get('instructions') or '')+' MODEL may use the supplied model_resolution value verbatim when status is EXPLICIT or CANDIDATE. A derived listing identifier remains a candidate, not a manufacturer-confirmed model. Never copy a knowledge-base example or a competitor model.'
        # Keep output bounded to the exact field IDs in this batch.
        props=req['text']['format']['schema']['properties'];props['attributes']['maxItems']=len(group);props['unresolved']['maxItems']=len(group)
        if ids:
            props['attributes']['items']['properties']['id']['enum']=sorted(ids)
            props['unresolved']['items']['enum']=sorted(ids)
        req['max_output_tokens']=1600;result.append(req)
    return result

def meaningful(value):
    text=str(value or '').strip().casefold()
    return text not in {'', 'unknown', 'null', 'none', 'n/a', 'not specified', 'unspecified', '未知', '未取得', '待确认', '未提供'} and not re.search(r'\b(?:not (?:provided|specified|available)|no .{0,80}(?:provided|available)|no evidence|unknown)\b|未提供|未取得|不清楚|待确认',text)

def merge(responses,requests,output_text):
    values={};unresolved=[];conflicts=set();all_ids=set()
    supplier=supplier_from_requests(requests)
    for response,request in zip(responses,requests):
        definitions={a['id']:a for a in json.loads(request['input'])['official_attributes']}
        ids=set(definitions);all_ids.update(ids);data=json.loads(output_text(response))
        for a in data.get('attributes',[]):
            key=a.get('id')
            if key in ids and valid_value(a,definitions[key]):
                a=normalize_enum(a,definitions[key])
                # A model-written value is not an official enumeration ID.
                enums={str(v['id']):v.get('name') for v in definitions[key].get('values',[]) if v.get('id') is not None}
                if str(a.get('value_id')) not in enums:a['value_id']=None
                if not meaningful(a.get('value_name')):a['value_name']=enums.get(str(a.get('value_id'))) or ''
                if not (meaningful(a.get('value_id')) or meaningful(a.get('value_name'))):continue
                if not source_value_supported(a,supplier,definitions[key]):continue
                if key in values and (values[key].get('value_id'),values[key].get('value_name')) != (a.get('value_id'),a.get('value_name')):
                    conflicts.add(key)
                else:values[key]=a
        unresolved.extend(x for x in data.get('unresolved',[]) if x in ids)
    for key in conflicts: values.pop(key,None)
    unresolved=list((all_ids-set(values))|conflicts)
    result=copy.deepcopy(responses[-1]);result['output']=[{'type':'message','content':[{'type':'output_text','text':json.dumps({'attributes':list(values.values()),'unresolved':list(dict.fromkeys(unresolved))},ensure_ascii=False)}]}]
    result['attribute_batch_count']=len(requests)
    result['usage']={k:sum((r.get('usage') or {}).get(k,0) for r in responses) for k in ('input_tokens','output_tokens')}
    return result
