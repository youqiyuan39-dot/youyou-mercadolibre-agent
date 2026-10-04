"""Store-bound, read-only operations. Opening a view never contacts the platform."""
from __future__ import annotations
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from . import store_management as stores, mercadolibre_oauth as oauth
from .mercadolibre_client import MercadoLibreClient

LOCK = threading.RLock()
KINDS = {'listings', 'metrics', 'orders', 'billing', 'messages', 'pending', 'campaigns'}
SITES = ('MLM', 'MLC', 'MCO', 'MLA', 'MLB')

def stamp():
    return datetime.now(timezone.utc).isoformat()

def registry(root):
    return stores.store_status(root).get('stores', [])

def store(root, store_id):
    row = next((s for s in registry(root) if s['id'] == store_id), None)
    if not row:
        raise ValueError('未找到目标店铺')
    return row

def path(root, store_id, kind):
    store(root, store_id)
    if kind not in KINDS:
        raise ValueError('运营栏目无效')
    return root / 'data/v3/operations/mercadolibre' / store_id / (kind + '.json')

def saved(root, store_id, kind):
    p = path(root, store_id, kind)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'state':'NOT_QUERIED','rows':[], 'total':None, 'time':None}

def write(root, store_id, kind, value):
    p = path(root, store_id, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(p)

def client_for(root, row):
    # Existing DPAPI vault is read only inside runtime; no credential is returned.
    private = oauth.load_private_json(stores._token_path(root, row['id']))
    client = MercadoLibreClient(private.get('access_token'))
    try:
        me = client.me()
    except Exception as exc:
        if '失效' not in str(exc): raise
        config=stores.get_runtime_oauth_config(root)
        oauth.refresh_tokens(client_id=config['client_id'],client_secret=config['client_secret'],token_file=stores._token_path(root,row['id']))
        client=MercadoLibreClient(oauth.load_private_json(stores._token_path(root,row['id'])).get('access_token'))
        me=client.me()
    if str(me.get('id')) != str(row.get('merchant_id')):
        raise ValueError('授权身份与店铺登记不一致，已停止查询')
    return client, me

def picked(value, names):
    return {k:value.get(k) for k in names}

def search(client, endpoint, params, key='results', maximum=1000):
    rows, offset, total = [], 0, None
    while offset < maximum:
        data = client.get(endpoint, {**params,'limit':50,'offset':offset})
        batch = data.get(key)
        if batch is None and (data.get('paging') or {}).get('total',data.get('total')) == 0:
            batch=[]
        if not isinstance(batch, list):
            raise ValueError('平台返回格式变化，本次未取得有效列表')
        total = (data.get('paging') or {}).get('total', data.get('total'))
        rows.extend(batch)
        offset += len(batch)
        if not batch or (total is not None and offset >= total) or len(batch) < 50:
            break
    return rows, total if total is not None else len(rows)

def mappings(client, row):
    if row.get('auth_type') != 'CBT':
        return [{'user_id':row['merchant_id'],'site_id':row['auth_type']}]
    data = client.marketplace_mapping(row['merchant_id'])
    return [picked(x, ['user_id','site_id','logistic_type']) for x in data.get('marketplaces', [])]

def listing_query(client, row):
    endpoint = '/marketplace/users/' if row.get('auth_type') == 'CBT' else '/users/'
    ids, total = search(client, endpoint + str(row['merchant_id']) + '/items/search', {}, maximum=100)
    rows, errors = [], []
    started=time.monotonic()
    for item_id in dict.fromkeys(str(i) for i in ids):
        if time.monotonic()-started > 90:
            errors.append('查询超过90秒；当前为部分结果，可重试。')
            break
        try:
            item = client.get('/items/' + item_id)
            listing = picked(item, ['id','title','price','currency_id','status','thumbnail','permalink','site_id'])
            if item_id.startswith('CBT'):
                try:
                    listing['marketplace_items'] = [picked(m, ['item_id','site_id']) for m in client.get('/items/' + item_id + '/marketplace_items').get('marketplace_items', [])]
                except Exception as exc:
                    listing['marketplace_items']=[]
                    errors.append(item_id+'：站点映射未取得：'+str(exc)[:100])
            else:
                listing['marketplace_items'] = [{'item_id':item_id,'site_id':item.get('site_id')}]
            rows.append(listing)
        except Exception as exc:
            errors.append(item_id + '：商品详情未取得：'+str(exc)[:100])
    return {'rows':rows,'total':total,'errors':errors,'complete':len(rows)==total and not errors}

def refresh(root, store_id, kind, options=None):
    options = options or {}
    row = store(root, store_id)
    old = saved(root, store_id, kind)
    result = {'platform':'mercadolibre','store_id':store_id,'kind':kind,'time':stamp(),'rows':[], 'total':None,'state':'SUCCESS','errors':[]}
    try:
        client, me = client_for(root, row)
        cbt = row.get('auth_type') == 'CBT'
        if kind == 'listings':
            result.update(listing_query(client,row))
        elif kind == 'metrics':
            for m in mappings(client,row):
                try:
                    d = client.get('/users/' + str(m['user_id']) + '/items_visits', {'last':7,'unit':'day'})
                    result['rows'].append({'site_id':m['site_id'],**picked(d,['total_visits','date_from','date_to'])})
                except Exception as exc:
                    result['errors'].append(str(m['site_id']) + '：Visits 本次未取得：'+str(exc)[:100])
            result['total'] = len(result['rows'])
        elif kind == 'orders':
            params = {'sort':'date_desc'}
            for k in ('from','to'):
                v = str(options.get(k) or '')
                if v:
                    datetime.strptime(v, '%Y-%m-%d')
                    params['order.date_created.'+k] = v + ('T00:00:00.000-04:00' if k=='from' else 'T23:59:59.999-04:00')
            if options.get('status'):
                if options['status'] not in {'paid','confirmed','cancelled'}: raise ValueError('订单状态无效')
                params['order.status'] = options['status']
            data,total = search(client, '/marketplace/orders/search' if cbt else '/orders/search', params if cbt else {**params,'seller':me['id']})
            for o in data:
                if cbt:
                    o = client.get('/marketplace/orders/'+str(o['id']))
                item_rows = o.get('order_items') or o.get('items') or []
                result['rows'].append({**picked(o,['id','pack_id','status','date_created','total_amount','currency_id']), 'seller_id':(o.get('seller') or {}).get('id'), 'shipment_id':(o.get('shipping') or o.get('shipment') or {}).get('id'), 'items':[{'quantity':i.get('quantity'),**picked(i.get('item') or i,['id','title','permalink','thumbnail'])} for i in item_rows]})
            result.update(total=total,filters=options,complete=len(result['rows'])==total)
        elif kind == 'billing':
            orders = saved(root,store_id,'orders')
            if orders['state'] != 'SUCCESS': raise ValueError('请先成功查询订单，再按 Seller ID 获取账单')
            groups = {}
            for o in orders['rows']:
                if o.get('seller_id'): groups.setdefault(str(o['seller_id']), []).append(str(o['id']))
            if not groups:
                result.update(state='DEPENDENCY_EMPTY',note='当前已读取订单中没有可对账记录；不是结算净额为零')
            for seller, ids in groups.items():
                for start in range(0,len(ids),60):
                    d = client.get('/billing/integration/group/ML/order/details', {'seller_id':seller,'order_ids':','.join(ids[start:start+60])})
                    # No buyer/payment/address data persisted. Raw response not exposed.
                    records = d if isinstance(d,list) else d.get('results') or d.get('orders') or []
                    if not records:
                        result.update(state='SCHEMA_UNVERIFIED',note='官方账单已返回，但本版尚未识别可核对的账单结构；不得记为零金额')
                    result['rows'].extend(picked(x,['order_id','pack_id','currency_id','total_amount','net_amount','shipping_cost']) for x in records if isinstance(x,dict))
            result['total']=len(result['rows'])
            result['note']='官方账单字段待逐项核对；未核对前不计算利润或汇总结算净额'
        elif kind == 'messages':
            orders = saved(root,store_id,'orders')
            packs = {(str(o['pack_id']),str(o['seller_id'])) for o in orders.get('rows',[]) if o.get('pack_id') and o.get('seller_id')}
            if not packs:
                result.update(state='DEPENDENCY_EMPTY',note='已保存订单没有 Pack 会话；不是全部消息为零')
            for pack,seller in sorted(packs):
                try:
                    data,total = search(client,'/messages/packs/'+pack+'/sellers/'+seller, {'tag':'post_sale','mark_as_read':'false'},'messages')
                    for m in data:
                        result['rows'].append({'id':m.get('id'),'pack_id':pack,'status':m.get('status'),'created':(m.get('message_date') or {}).get('created'),'read':(m.get('message_date') or {}).get('read'), 'sender':'卖家' if str((m.get('from') or {}).get('user_id'))==seller else '买家','text':str(m.get('text') or '')[:5000]})
                except Exception:
                    result['errors'].append('Pack '+pack+'：消息未取得')
            result['total']=len(result['rows'])
        elif kind == 'pending':
            for label,endpoint,params,key,fields in [
                ('claims','/marketplace/v2/claims/search' if cbt else '/post-purchase/v1/claims/search',{'status':'opened','user_id':me['id']},'data',['id','status','reason_id','resource_id']),
                ('questions','/marketplace/questions/search' if cbt else '/questions/search',{'seller_id':me['id'],'status':'UNANSWERED'},'questions',['id','status','item_id','text','date_created'])]:
                try:
                    rs,total=search(client,endpoint,params,key)
                    result[label]={'rows':[picked(r,fields) for r in rs],'total':total,'state':'SUCCESS'}
                except Exception as exc:
                    result[label]={'rows':[],'total':None,'state':'FAILED'}
                    result['errors'].append(label+'：本次未取得：'+str(exc)[:100])
            result['messages']=saved(root,store_id,'messages')
        elif kind == 'campaigns':
            if cbt:
                result.update(state='UNSUPPORTED',note='当前 CBT 授权不通过本模块提交 seller-promotions；请到目标国家卖家中心管理。')
            else:
                data=client.get('/seller-promotions/users/'+str(me['id']),{'app_version':'v2'})
                result['rows']=[picked(x,['id','name','type','status','start_date','finish_date']) for x in data.get('results',[])]
                result['total']=len(result['rows'])
        if result['errors']:
            result['state']='PARTIAL' if result['rows'] or kind=='pending' else 'FAILED'
    except Exception as exc:
        result.update(state='FAILED',error='本次未取得：'+ ('授权已失效，请刷新授权后重试' if '失效' in str(exc) else str(exc)[:180]))
    with LOCK:
        if result['state']=='FAILED' and old.get('state') in {'SUCCESS','PARTIAL'}:
            old.update(last_attempt={'time':result['time'],'state':'FAILED','error':result.get('error') or '本次刷新失败，保留上次快照'})
            write(root,store_id,kind,old)
            return old
        write(root,store_id,kind,result)
    return result

def snapshot(root):
    rows=registry(root)
    return {'ok':True,'active_store_id':stores.store_status(root).get('active_store_id'),'stores':[{**r,'operations':{k:saved(root,r['id'],k) for k in sorted(KINDS)}} for r in rows], 'external_write_enabled':False}

def discount_preview(root, value):
    sid=str(value.get('store_id') or '')
    store(root,sid)
    pct=Decimal(str(value.get('discount')))
    if not pct.is_finite() or not 0 < pct < 100: raise ValueError('折扣必须大于0且小于100')
    listing=saved(root,sid,'listings')
    selected=set(value.get('item_ids') or [])
    if not 1 <= len(selected) <= 100: raise ValueError('请选择1–100件已读取商品')
    rows=[]
    for i in listing['rows']:
        if i['id'] in selected:
            p=Decimal(str(i['price']))
            rows.append({'id':i['id'],'title':i['title'],'original':str(p),'discounted':str((p*(100-pct)/100).quantize(Decimal('.01'))),'currency':i['currency_id']})
    if len(rows)!=len(selected): raise ValueError('商品不属于当前店铺快照，拒绝跨店预览')
    return {'ok':True,'store_id':sid,'platform':'mercadolibre','snapshot_time':listing['time'],'discount':str(pct),'rows':rows,'submitted':False,'note':'仅价格预览；未核对成本、平台促销资格与最低价格，不代表利润可行或已生效'}

def shipment(root,value):
    sid=str(value.get('store_id') or '')
    target=str(value.get('shipment_id') or '')
    row=store(root,sid)
    order=next((r for r in saved(root,sid,'orders')['rows'] if str(r.get('shipment_id'))==target and target.isdigit()),None)
    if not order: raise ValueError('物流记录不属于当前店铺已读取订单')
    client,me=client_for(root,row)
    try:
        data=client.get('/shipments/'+target)
    except Exception as exc:
        raise ValueError('本次未取得物流详情：'+str(exc)[:160]) from exc
    public=picked(data,['id','status','substatus','logistic_type','tracking_number','date_created','last_updated'])
    p=root/'data/v3/operations/mercadolibre'/sid/('shipment-'+target+'.json')
    p.write_text(json.dumps(public,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'ok':True,'shipment':public}
