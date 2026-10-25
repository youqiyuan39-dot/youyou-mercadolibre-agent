"""Lossless SKU blocker counts before tool-output shortening."""
from collections import Counter
from .attribute_context_v3 import meaningful
from .publish_service import _package_attributes

def summarize(draft):
    ev=draft.get('evidence') or {};skus=ev.get('sku_details') or [];review=ev.get('image_review') or {}
    bound={str(g.get('sku_id')) for g in review.get('sku_source_galleries') or [] if g.get('gallery')}
    counts=Counter();required=Counter();done=0;screened=0
    for sku in skus:
        sid=str(sku.get('sku_id'));done+=(sku.get('ai_processing') or {}).get('status')=='done'
        if not sku.get('site_pricing'):counts['目标站点缺失']+=1
        if not all((sku.get('package') or {}).get(k) for k in ('length_cm','width_cm','height_cm')):counts['包装尺寸缺失']+=1
        if sid not in bound and len(skus)>1:counts['花色与货源原图未绑定']+=1
        gallery=next((g.get('gallery') or [] for g in review.get('sku_listing_galleries') or [] if str(g.get('sku_id'))==sid),review.get('gallery') or [] if len(skus)==1 else [])
        refs={i.get('screen_fingerprint') or i.get('local_file') or i.get('remote_url') for i in gallery if i.get('review_status')!='REJECTED'}-{None,''}
        if len(refs)<3:counts['可用上架图片不足3张']+=1
        sites=sku.get('site_pricing') or []
        if not sites or any(s.get('price') is None and s.get('net_proceeds') is None for s in sites):counts['价格或净回款尚未确定']+=1
        content=sku.get('ai_content') or {};attrs=(content.get('attributes') or [])+_package_attributes(sku)
        filled={a.get('id') for a in attrs if meaningful(a.get('value_id')) or meaningful(a.get('value_name'))}
        required.update(set((content.get('attribute_completion') or {}).get('required_ids') or [])-filled)
    labels={'BRAND':'品牌依据缺失','MATERIAL':'材质依据缺失','PACKAGE_HEIGHT':'包装高度缺失','PACKAGE_WIDTH':'包装宽度缺失','PACKAGE_LENGTH':'包装长度缺失','PACKAGE_WEIGHT':'包装重量缺失'}
    for key,count in required.items():
        if key in {'PACKAGE_HEIGHT','PACKAGE_WIDTH','PACKAGE_LENGTH'}:continue
        counts[labels.get(key,'必填属性 '+str(key)+' 缺失')]+=count
    for report in (review.get('source_screening') or {}).get('skus') or []:screened+=len(report.get('items') or [])
    return {'sku_total':len(skus),'content_stage_done':done,'original_public_image_count':len(review.get('source_gallery') or []),
        'vision_screened_count':screened,'blockers':[{'label':label,'sku_count':n} for label,n in sorted(counts.items())],
        'scope_rule':'站点身份可继承；包装、花色图片、成本和定价分别核对SKU。阶段完成不代表属性资料齐全。'}

def diagnostic_answer(title,data):
    state='资料仍未齐，不能发布。' if data['blockers'] else '本机未检出这些SKU资料阻塞；仍需发布核对和平台校验。'
    lines=[str(title or '当前商品'),f"文案与属性处理阶段完成 {data['content_stage_done']}/{data['sku_total']} 个SKU；"+state]
    if data['original_public_image_count'] and not data['vision_screened_count']:lines.append(f"保留公共原图 {data['original_public_image_count']} 张；尚无已完成的逐SKU视觉筛选，不能当可用图库。")
    lines+=['待补：'+b['label']+f"（{b['sku_count']}个SKU）" for b in data['blockers']]
    lines.append('先重新处理以恢复原采集记录中丢失的字段；若提示采集缺口，重新采集该SKU明确标注的包装数据及花色原图对应。原记录未取得不等于供应商没有；重采仍缺时才请求供应商包装三边或带SKU标签的图片。不能把展开尺寸当包装，也不能按图片顺序猜配。利润、生图和发布仍需核对确认。')
    return '\n'.join(lines)
