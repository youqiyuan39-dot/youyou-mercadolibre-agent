"""Explicit source-image bindings and parcel confirmations from the product editor."""
import copy
import math

def apply(draft, changes):
    ev=draft.setdefault('evidence',{});skus={str(s['sku_id']):s for s in ev.get('sku_details') or []}
    review=ev.setdefault('image_review',{})
    pool={str(i.get('local_file') or i.get('remote_url')):i for i in (review.get('source_gallery') or [])+(review.get('detail_gallery') or []) if i.get('local_file') or i.get('remote_url')}
    groups={str(g.get('sku_id')):copy.deepcopy(g) for g in review.get('sku_source_galleries') or []}
    listings={str(g.get('sku_id')):copy.deepcopy(g) for g in review.get('sku_listing_galleries') or []}
    for assignment in changes.get('sku_source_assignments') or []:
        sid=str(assignment.get('sku_id') or '');ref=str(assignment.get('reference') or '')
        if sid not in skus or ref not in pool:raise ValueError('原图绑定必须选择当前商品的SKU和已有原图')
        image=copy.deepcopy(pool[ref]);image.update(sku_id=sid,variant=skus[sid].get('variant'),binding_source='HUMAN_SELECTED',image_type='sku')
        group=groups.setdefault(sid,{'sku_id':sid,'variant':skus[sid].get('variant'),'gallery':[]})
        if not any((i.get('local_file') or i.get('remote_url'))==ref for i in group['gallery']):group['gallery'].append(image)
        selected=listings.setdefault(sid,{'sku_id':sid,'variant':skus[sid].get('variant'),'gallery':[]})
        if not any((i.get('local_file') or i.get('remote_url'))==ref for i in selected['gallery']):
            selected['gallery'].append({**copy.deepcopy(image),'review_status':'HUMAN_SELECTED'})
    if changes.get('sku_source_assignments'):
        review['sku_source_galleries']=list(groups.values())
        review['sku_listing_galleries']=list(listings.values())
        for sid,sku in skus.items():sku['source_image_count']=len((groups.get(sid) or {}).get('gallery') or [])
    for confirmation in changes.get('sku_package_confirmations') or []:
        ids=[str(sid) for sid in confirmation.get('sku_ids') or []]
        note=str(confirmation.get('evidence') or '').strip()[:1000]
        if confirmation.get('confirmed') is not True or not note or not ids or any(sid not in skus for sid in ids):
            raise ValueError('包装补齐需选择当前SKU，并记录实测或供应商确认依据')
        values={}
        for key in ('length_cm','width_cm','height_cm'):
            try:value=float(confirmation.get(key))
            except (TypeError,ValueError):raise ValueError('包装三边必须为正数（厘米）')
            if not math.isfinite(value) or value<=0:raise ValueError('包装三边必须为正数（厘米）')
            values[key]=value
        for sid in ids:
            pack=skus[sid].setdefault('package',{})
            # The control is explicitly "fill missing"; existing measurements remain unchanged.
            filled={k:v for k,v in values.items() if not pack.get(k)}
            pack.update(filled)
            if filled:
                statuses=pack.setdefault('field_status',{})
                statuses.setdefault('weight_g',pack.get('status') or 'UNVERIFIED')
                statuses.update({k:'USER_CONFIRMED' for k in filled})
                pack.update(status='USER_CONFIRMED',evidence=note,source_method='EDITOR_EXPLICIT_PACKAGE_CONFIRMATION')
    return draft
