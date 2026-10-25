"""Public row context follows the user's 1688 search tabs; never use 'last product'."""
import json, re
from pathlib import Path
from .market_follow_v3 import reference
from .auto_processing_v3 import write

def tab_path(root,tab):
    if not re.fullmatch(r'\d{1,12}',str(tab)):raise ValueError('采集标签编号无效')
    return Path(root)/'data/intake/market-tabs'/(str(tab)+'.json')

def resolve(root,tab):
    path=tab_path(root,tab)
    if not path.is_file():return None
    try:return reference(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError):return None

def handle(root,value):
    tab=value.get('tab_id');path=tab_path(root,tab)
    if value.get('operation')=='resolve':return {'ok':True,'market_reference':resolve(root,tab)}
    if value.get('operation')=='inherit':ref=resolve(root,value.get('parent_tab_id'))
    else:
        from .source_intake import _contains_forbidden_key
        if _contains_forbidden_key(value):raise ValueError('参考资料包含禁止保存的字段')
        ref=reference(value.get('market_reference'))
    if ref:write(path,ref)
    return {'ok':True,'market_reference':ref}
