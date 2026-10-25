"""Read-only selection evidence for the workbench assistant; no model or live writes."""
from datetime import datetime,timezone
from . import selection_v3 as selection,selection_discovery_v3 as discovery,selection_research_v3 as research

BOUNDARY='有条件线索不是准确月销量已验证候选；市场粗筛不证明同规格、供货、合规、真实利润或可以发布。历史采集记录不是实时平台数据。'
ROW_KEYS=('item_id','title','site_id','currency','price','state','missing','reasons','notes','sales_30d','cumulative_sales_interval','age_days','source_captured_at','conditional_research_allowed','profit_state','novelty','ai_fit')

def overview(root):
    history=selection.list_batches(root)
    cases=research.list_cases(root)['items']
    return {'batches':history['items'][:10],'sources':history['sources'],'default_rules':history['default_rules'],
        'research_versions':[{'revision_id':c['id'],'item_id':c['item']['item_id'],'title':c['item']['title'],'created_at':c['created_at'],'assessment':c['assessment']['status']} for c in cases[:10]],
        'limitations':BOUNDARY,'next':'可按batch_id查看批次，按revision_id查看研究缺项；无编号时先读取列表，不猜编号。'}

def batch(root,batch_id='',item_id=''):
    if not batch_id:
        history=selection.list_batches(root)['items']
        if not history:return {'found':False,'message':'当前没有选品批次，尚未取得筛选结果。'}
        batch_id=history[0]['id']
    b=selection.get_batch(root,batch_id)
    rows=b['rows']
    if item_id:rows=[r for r in rows if r['item_id']==item_id]
    # Recheck against today's date without modifying historical batch evidence.
    checked=[selection.screen(r,b['rules']) for r in rows]
    counts={state:sum(r['state']==state for r in checked) for state in ('CANDIDATE','SECONDARY','CONDITIONAL_LEAD','NEEDS_EVIDENCE','EXCLUDED')}
    return {'found':bool(rows),'batch_id':b['id'],'created_at':b['created_at'],'rules':b['rules'],
        'stored_counts':b['counts'],'current_recheck_counts':counts,'matched':len(rows),
        'items':[{**{k:r.get(k) for k in ROW_KEYS},'current_recheck':{k:c[k] for k in ('state','missing','reasons')}} for r,c in list(zip(rows,checked))[:12]],
        'truncated':len(rows)>12,'scope':'stored_counts是批次当时结果；current_recheck是使用原快照按今天日期复核，并非重新查询平台。items最多12条，matched/counts基于全部匹配记录。',
        'group_count':len(b.get('groups',[])),'ai_state':b['ai_state'],'limitations':BOUNDARY}

def exploration(root):
    tasks=discovery.latest(root)[:3]
    hb=discovery.folder(root)/'collector.json'
    heartbeat=None
    if hb.is_file():
        import json
        record=json.loads(hb.read_text(encoding='utf-8'))
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(record['at'])).total_seconds()
            heartbeat={'last_seen':record['at'],'connected':0<=age<20,'enabled':record.get('enabled') is True and 0<=age<20}
        except (ValueError,KeyError,TypeError):heartbeat={'connected':False,'enabled':False}
    return {'collector':heartbeat or {'connected':False,'enabled':False},'tasks':[
        {'task_id':t['id'],'status':t['status'],'stop_reason':t.get('stop_reason'),'max_rounds':t['max_rounds'],
         'pending_query':(t.get('request') or {}).get('query'),'rounds':[
             {'label':r.get('label'),'query':r.get('query'),'feedback':r['feedback'],'feedback_seen_by_model':r.get('feedback_seen_by_model',False)} for r in t['rounds']]} for t in tasks],
        'limitations':BOUNDARY+' 接收端启动与扩展已连接、采集开启、真实新数据已回传是不同状态。只读查询不启动或停止探索。'}

def case(root,revision_id=''):
    if revision_id:c=research.get(root,revision_id)
    else:
        cases=research.list_cases(root)['items']
        if not cases:return {'found':False,'message':'尚无研究记录；在AI选品点击目标商品的研究入口。'}
        c=cases[0]
    return {'found':True,'revision_id':c['id'],'item_id':c['item']['item_id'],'title':c['item']['title'],
        'created_at':c['created_at'],'assessment':research.assess(c),'recorded_evidence':c['evidence'],
        'source_checks':c.get('source_checks',[])[-3:],'profit_state':c['profit_state'],
        'next_steps':['AI选品→目标商品研究→1688找货源→打开具体商品→启用悠悠采集并采集→检查回传记录/草稿→核对SKU、规格和包装重量→补齐成本并计算利润→再进入处理与发布中心'],
        'limitations':BOUNDARY+' 找到相似图或打开1688不等于同规格、采集成功或供应商确认；未知值不填0。此工具仅查询，不代浏览器采集。'}
