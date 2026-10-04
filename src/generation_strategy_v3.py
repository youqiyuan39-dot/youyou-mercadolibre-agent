"""Public, credential-free generation preferences."""
import copy
import json


def load(root):
    path = root / 'data' / 'v3' / 'generation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'image_count': 2, 'main_background': 'white', 'image_text': 'none'}


def validate(value):
    count = value.get('image_count')
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10:
        raise ValueError('生图数量需为0–10的整数')
    if value.get('main_background') != 'white':
        raise ValueError('本版仅验收白底主图策略；透明通道尚未验收')
    if value.get('image_text', 'none') not in {'none', 'market'}:
        raise ValueError('图片文字策略无效')
    return {'image_count': count, 'main_background': value['main_background'], 'image_text': value.get('image_text', 'none')}


def apply(prompts, strategy):
    strategy = validate(strategy)
    if not prompts and strategy['image_count']: raise ValueError('缺少基础生图提示词')
    result = []
    for index in range(strategy['image_count']):
        row = copy.deepcopy(prompts[index] if index < len(prompts) else prompts[1 if len(prompts) > 1 else 0])
        if index >= len(prompts):
            row['kind'] = 'detail_' + str(index + 1)
            row['label'] = '补充展示图 ' + str(index + 1)
            row['prompt'] += ' 从不同展示角度呈现同一SKU，不能增加配件或改变实物结构。'
        row['prompt'] = row['prompt'].replace('Mercado Libre', '目标电商平台').replace('维修工作台', '中性的商品展示环境')
        result.append(row)
    return result
