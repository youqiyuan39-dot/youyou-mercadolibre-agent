"""Remove table headers and mixed-variant capture noise; raw capture stays intact."""
import copy
import re


def attributes_for_sku(attributes, selected, skus):
    if not isinstance(attributes, dict): return attributes
    from .supplier_facts_v3 import normalize_attributes
    attributes = normalize_attributes(attributes)
    names = {str(s.get('variant') or '') for s in skus if isinstance(s, dict)} - {''}
    selected_name = str(selected.get('variant') or '')
    result = {}
    for key, value in attributes.items():
        text = str(value)
        if str(key) in ('颜色','尺寸','规格') and re.search(r'专供货源|是否跨境|价格.*库存|颜色\s+重量',text):continue
        if str(key)=='颜色' and re.search(r'\d\s*(?:cm|mm|厘米|毫米)|重量',text,re.I):continue
        if str(key) in names or '进货数量' in text or re.search(r'价格.*库存', text): continue
        if '全部' in text and not re.search(r'包装|包裹', str(key)+text): continue
        if re.fullmatch(r'款式\d+',str(key)) and str(key) not in selected_name.split(' / '): continue
        if any(name != selected_name and name in str(key) for name in names): continue
        if re.fullmatch(r'重量\s*\(?g\)?|全长\s*\(?mm\)?|工作尺寸\s*\(?寸\)?', text.strip(), re.I): continue
        if re.search(r'表面处理|材质|材料|工艺', str(key)) and re.fullmatch(r'[\d\s.,/-]+', text.strip()): continue
        result[key] = copy.deepcopy(value)
    return result
