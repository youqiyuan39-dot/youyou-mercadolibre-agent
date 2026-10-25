"""Materialize fixed packaging tables without changing immutable collection data."""
import copy,math

VERSION='PACKAGING_TABLE_20261017_V1'
FIELDS=('weight_g','length_cm','width_cm','height_cm')

def positive(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0

def materialize(capture):
    data=copy.deepcopy(capture);skus=data.get('skus') or [];single_offer=False
    if not skus and (data.get('offer_flags') or {}).get('isSkuOffer') is False:
        # Internal stable identity, explicitly NOT a supplier SKU or a new variant.
        single_offer=True;skus=[{'sku_id':'offer-'+str(data['offer_id']),'spec_id':'',
            'variant':'无规格选项单品','sku_id_origin':'SINGLE_OFFER_WITHOUT_OPTIONS',
            'price':(data.get('pricing') or {}).get('amount'),'inventory':None,'package':{}}]
        data['skus']=skus
    records=[]
    for key in ('package_rows','unassigned_package_rows'):
        for index,row in enumerate(data.get(key) or []):
            if isinstance(row,dict) and isinstance(row.get('package'),dict):records.append((key,index,row))
    report={'version':VERSION,'single_offer_materialized':single_offer,'raw_source_sku_count':len(capture.get('skus') or []),'items':[],'unassigned':[],'conflicts':[]}
    used=set()
    for sku in skus:
        sid=str(sku.get('sku_id') or '');spec=str(sku.get('spec_id') or '');label=str(sku.get('variant') or '')
        matched=[]
        for key,index,row in records:
            rid=str(row.get('sku_id') or '');rspec=str(row.get('spec_id') or '');rlabel=str(row.get('variant') or '')
            if rid or rspec:
                ok=bool(rid and rid==sid or rspec and spec and rspec==spec)
                method='EXACT_SKU_OR_SPEC_ID'
            elif rlabel:
                ok=bool(rlabel==label and sum(str(s.get('variant') or '')==rlabel for s in skus)==1)
                method='EXACT_UNIQUE_VARIANT_LABEL'
            else:
                ok=len(skus)==1;method='SINGLE_OFFER_TABLE' if single_offer else 'ONLY_SOURCE_SKU_TABLE'
            if ok:matched.append((key,index,row,method));used.add((key,index))
        original=sku.get('package') or {};pack=copy.deepcopy(original);found={k:[] for k in FIELDS};sources={}
        for field in FIELDS:
            if positive(original.get(field)):found[field].append(original[field])
            if len(skus)==1 and positive((data.get('package') or {}).get(field)):found[field].append(data['package'][field])
        for key,index,row,method in matched:
            for field in FIELDS:
                dimension_type=str(row.get('dimension_type') or 'PACKAGING').upper()
                weight_type=str(row.get('weight_type') or 'PACKED_WEIGHT').upper()
                if field=='weight_g' and weight_type in ('NET_WEIGHT','UNSPECIFIED'):continue
                if field!='weight_g' and dimension_type not in ('PACKAGING','PACKAGE'):continue
                value=row['package'].get(field)
                if positive(value):
                    found[field].append(value);sources.setdefault(field,[]).append({'path':f'/{key}/{index}/package/{field}',
                        'method':method,'headers':row.get('headers') or [],'values':row.get('values') or [],
                        'quote':row.get('quoted_text') or row.get('table_title') or '1688 productPackInfo / pieceWeightScale packaging row'})
        for field,values in found.items():
            unique=set(values)
            manual=(data.get('package') or {}).get(field)
            if len(skus)==1 and data.get('package_input_mode')=='manual' and positive(manual):
                pack[field]=manual;sources[field]=[{'path':'/package/'+field,'method':'USER_INPUT_UNVERIFIED'}]
            elif len(unique)==1:pack[field]=values[0]
            elif len(unique)>1:
                pack[field]=None;report['conflicts'].append({'sku_id':sid,'field':field,'values':sorted(unique),'sources':sources.get(field,[])})
            else:pack.setdefault(field,None)
        if sources:
            pack.update(evidence='1688_packaging_table',status='SUPPLIER_PACKAGING_INFO',field_sources=sources)
            pack['field_status']={f:'SUPPLIER_PACKAGING_INFO' for f in FIELDS if positive(pack.get(f))}
            for field,entries in sources.items():
                if entries and entries[0]['method']=='USER_INPUT_UNVERIFIED':pack['field_status'][field]='USER_INPUT_UNVERIFIED'
        sku['package']=pack
        report['items'].append({'sku_id':sid,'matched_rows':[{'path':f'/{key}/{index}','method':method} for key,index,_,method in matched],
                                'recovered_fields':[k for k in FIELDS if positive(pack.get(k))],'sources':sources})
    for key,index,row in records:
        if (key,index) not in used:report['unassigned'].append({'path':f'/{key}/{index}','sku_id':row.get('sku_id'),'variant':row.get('variant'),'package':row['package'],'reason':'NO_UNIQUE_SKU_ID_SPEC_OR_LABEL'})
    if len(skus)==1 and data.get('package_input_mode')!='manual':
        top=copy.deepcopy(data.get('package') or {})
        for field in FIELDS:top[field]=skus[0]['package'].get(field)
        if skus[0]['package'].get('field_sources'):top.update(evidence='1688_packaging_table',field_sources=skus[0]['package']['field_sources'])
        data['package']=top
    data['package_table_resolution']=report
    return data
