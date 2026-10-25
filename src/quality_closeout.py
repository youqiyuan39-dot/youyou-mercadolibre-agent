"""Narrow quality fixes: retain history, synchronize local title suggestions."""
import copy
from datetime import datetime,timezone

def replace_generated_gallery(review,generated):
    if not generated:return
    previous=review.get('gallery') or []
    if previous and previous != generated:
        review.setdefault('archived_galleries',[]).append({'at':datetime.now(timezone.utc).isoformat(),'reason':'REPLACED_BY_CURRENT_GENERATION','gallery':copy.deepcopy(previous)})
    review['gallery']=copy.deepcopy(generated)

def sync_localized_titles(draft,titles,locked=False):
    if locked:return
    payload=draft.setdefault('payload',{})
    for row in payload.get('sites_to_sell') or []:
        if isinstance(row,dict) and titles.get(row.get('site_id')):row['title']=titles[row['site_id']]
    for sku in (draft.get('evidence') or {}).get('sku_details') or []:
        for row in sku.get('site_pricing') or []:
            if isinstance(row,dict) and titles.get(row.get('site_id')):row['title']=titles[row['site_id']]
    draft.setdefault('evidence',{})['localized_title_delivery']={'mode':'PLATFORM_GENERATED_AT_CREATION','suggestions':copy.deepcopy(titles),'note':'本地站点标题是核对建议；当前创建流程由平台生成展示标题，发布后须核对。'}

def unresolved_packaging(draft):
    conflict=(draft.get('evidence') or {}).get('packaging_conflict') or {}
    return bool(conflict) and conflict.get('state') not in ('RESOLVED','USER_CONFIRMED')
