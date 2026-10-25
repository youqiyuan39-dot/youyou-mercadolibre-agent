"""Read-only operational priorities derived from current local product records."""
from collections import Counter
from datetime import datetime, timezone

PRIORITY_BASIS = '先回查已有提交，避免重复发布；再排查失败；再补待发布资料；最后推进新任务。是工作台建议，未按利润或销量排序。'
SYSTEM_PREREQUISITE = '发布中心已接入店铺绑定、发布前检查和结果回查；真实商品发布尚未验收。你需选择目标店铺并确认具体内容，助手不能代确认或提交。'

def issues_of(row):
    values = []
    for field in ('errors', 'publish_issues', 'evidence_issues'):
        items = row.get(field) or []
        values.extend(items if isinstance(items, list) else [items])
    values.extend(row.get(field) for field in ('full_ai_error', 'image_job_error', 'publish_error') if row.get(field))
    return list(dict.fromkeys(str(value) for value in values if value))

def action_for(row):
    stage = row.get('stage'); issues = issues_of(row)
    if stage == 'PUBLISHED': return None
    if stage in {'PUBLISH_PARTIAL', 'PUBLISH_PENDING_CONFIRMATION'}:
        rank, category, owner, step = 1, '回查发布结果', '你核实目标店铺和平台记录；助手解释本机日志', '打开发布中心，按目标店铺回查已提交结果；先区分成功与失败的SKU，避免整件重复发布。'
    elif stage in {'AI_FAILED', 'READ_ERROR'}:
        rank, category, owner, step = 2, '排查处理失败', '助手查日志；你确认事实与重试费用', '先查看AI日志，区分已记录错误和原因猜测，补齐资料后再准备失败任务重试预览；本次不启动生成。'
    elif stage == 'AI_PAUSED':
        rank, category, owner, step = 5, '核对暂停原因', '你决定是否继续；助手查日志', '查看暂停原因和在途任务，确认停止后再决定下一步；不要把暂停当作失败自动重试。'
    elif stage == 'AI_PROCESSING' or row.get('full_ai_status') in {'queued', 'running'}:
        rank, category, owner, step = 6, '等待当前任务', '系统继续当前任务；助手按需查进度', '查看AI日志和当前进度，不重复启动任务；出现错误后再排查。'
    elif issues or stage in {'PUBLISH_BLOCKED', 'BLOCKED_EVIDENCE', 'CAPTURE_READY'}:
        rank = 3 if row.get('in_publish_center') else 4
        category = '补齐待发布资料' if row.get('in_publish_center') else '补齐处理前资料'
        owner = '你提供供应商或实测依据；助手准备允许字段的修改预览'
        step = '打开商品，逐项核对下方缺项。重量、尺寸需可靠依据，不能靠改提示词或反复生成补造；已有明确值时可请助手准备修改预览，未开放字段在商品页处理。'
    elif row.get('in_publish_center'):
        rank, category, owner, step = 7, '核对并发布', '你核对目标店铺和具体发布内容', '打开发布中心，选择目标店铺并点击发布；核对商品、SKU、站点、价格、库存与图片后确认提交。平台最终结果需回查。'
    else:
        rank, category, owner, step = 7, '推进商品处理', '你确认商品事实与生成费用；系统执行固定流程', '核对选定SKU、原图与来源，在商品处理页按需启动AI流程；确认前不产生生成费用。'
    return {'name':row.get('name'), 'title':row.get('title') or row.get('name'), 'stage':stage, 'priority':rank, 'category':category, 'owner':owner, 'next_step':step, 'issues':issues, 'page':'publish' if row.get('in_publish_center') else 'processing'}

def build(rows, name=''):
    selected = [row for row in rows if not name or row.get('name') == name]
    tasks = [task for row in selected if (task := action_for(row)) is not None]
    tasks.sort(key=lambda task:(task['priority'], str(task['name'])))
    return {'read_at':datetime.now(timezone.utc).isoformat(), 'scope':'本机当前商品记录，非实时平台状态',
            'found':bool(selected) if name else True, 'product_total':len(selected), 'action_total':len(tasks),
            'recorded_published':sum(row.get('stage') == 'PUBLISHED' for row in selected),
            'category_counts':dict(Counter(task['category'] for task in tasks)), 'tasks':tasks[:5], 'truncated':len(tasks)>5,
            'priority_basis':PRIORITY_BASIS, 'system_prerequisite':SYSTEM_PREREQUISITE,
            'measurement':{'state':'待采集基线', 'manual_minutes_per_sku':None, 'model_cost_per_sku':None, 'rework_count':None, 'verified_profit':None,
                           'note':'当前仅有状态统计；未记录每SKU人工耗时、模型实际费用及返工次数，不能计算节省比例或利润。'}}
