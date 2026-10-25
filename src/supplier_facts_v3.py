"""Recover literal supplier cells without treating repeated DOM text as new facts."""
import copy
import re

LABELS = ('额定功率', '包装体积', '产品类型', '温控档位', '插头规格', '颜色', '图案', '制造商零件编号', '含包装尺寸', '包装尺寸', '包装重量', '适用节日', '是否IP授权',
          '是否跨境出口专供货源', '是否专利货源', '生产企业', '加工定制', '品牌', '货号', '型号', '材质', '材料', '适用', '产地', '规格', '尺寸')
ALIASES = {'brand': '品牌', 'material': '材质', 'model': '型号', 'mpn': '制造商零件编号'}

def repeated_cell(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip(' :：')
    tokens = text.split()
    for width in range(1, len(tokens) // 2 + 1):
        if len(tokens) % width == 0 and tokens == tokens[:width] * (len(tokens) // width):
            return ' '.join(tokens[:width])
    return text

def normalize_attributes(attributes):
    result = copy.deepcopy(attributes) if isinstance(attributes, dict) else {}
    findings = {}
    pattern = re.compile(r'(?<!\S)(' + '|'.join(map(re.escape, LABELS)) + r')(?=\s|[:：]|$)')
    for key, raw in list(result.items()):
        label = ALIASES.get(str(key).lower(), str(key))
        if label not in LABELS:
            continue
        text = str(raw or '').strip()
        # Only parse labelled cell text or the exact attribute's own value.
        matches = list(pattern.finditer(text))
        if matches and matches[0].start() == 0 and matches[0][1] == label:
            for i, match in enumerate(matches):
                value = repeated_cell(text[match.end():matches[i+1].start() if i+1 < len(matches) else None])
                if value and value not in LABELS:
                    findings.setdefault(match[1], set()).add(value)
        else:
            value = repeated_cell(text)
            if value and value not in LABELS:
                findings.setdefault(label, set()).add(value)
    for key, values in findings.items():
        # Contradictory rows are retained as unresolved source text, never selected arbitrarily.
        if len(values) == 1:
            result[key] = next(iter(values))
        else:
            result.pop(key, None)  # Immutable raw capture retains the conflicting text.
    return result

def literal_attributes(supplier, definitions):
    from .attribute_context_v3 import meaningful, editable
    attrs = normalize_attributes(supplier.get('attributes') or {})
    labels = {'BRAND': '品牌', 'MATERIAL': '材质', 'MODEL': '型号', 'MPN': '制造商零件编号'}
    rows = []
    for definition in definitions:
        key = definition.get('id'); label = labels.get(key)
        value = attrs.get(label) if label else None
        if not value or not meaningful(value) or not editable(definition):
            continue
        if any(mark in str(value) for mark in ('未知', '未标', '待确认', '其他', '其它', '咨询')):
            continue
        if key=='BRAND' and str(value).strip() in ('无','无品牌'):value='Generic'
        if len(str(value)) > 80 or any(other != label and other in str(value) for other in LABELS):
            continue
        # Preserve proper brand spelling, do not invent a Romanized manufacturer name.
        rows.append({'id': key, 'value_id': None, 'value_name': str(value)})
    return rows

def door_banner(title):
    return bool(re.search(r'门帘|门幅|对联', str(title)) and re.search(r'亡灵|万灵|骷髅|挂旗|横幅', str(title)))

def wrong_type(source_title, text):
    return door_banner(source_title) and bool(re.search(r'\bhinges?\b|\bbisagras?\b|\bdobradiças?\b|门铰链|^(?:Decorative No Light )?Signs$', str(text), re.I))
