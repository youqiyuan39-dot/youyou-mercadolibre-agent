// Serialize reservations across tabs; persist only aggregate counters/timestamps.
let offerReservation=Promise.resolve();
function reserveOfferCapture(){
 const task=offerReservation.then(async()=>{
  const now=Date.now(),day=new Date(now).toISOString().slice(0,10);
  let stored;
  try {stored=(await chrome.storage.local.get('offerCandidateRate')).offerCandidateRate||{};}
  catch(e){throw Error('storage.local.get: '+e.message);}
  const count=stored.day===day?Number(stored.count)||0:0;
  const interval=stored.last?now-stored.last:5000;
  if(interval<5000)throw Object.assign(Error('两次采集至少间隔5秒'),{step:'rate_limit'});
  if(count>=50)throw Object.assign(Error('今日已达50次采集上限'),{step:'rate_limit'});
  try {await chrome.storage.local.set({offerCandidateRate:{day,count:count+1,last:now}});}
  catch(e){throw Error('storage.local.set: '+e.message);}
  return {ok:true,captured_at:new Date(now).toISOString(),rate_limit:{day,daily_count:count+1,daily_limit:50,interval_ms:Math.floor(interval)}};
 });
 offerReservation=task.catch(()=>{});return task;
}
const LOCAL_COLLECT_URL = 'http://127.0.0.1:8789/api/collect';
const ensureSelectedSkuRuntime = async payload => {
  if (!payload?.selected_sku_id) return;
  let health;
  try {
    const response = await fetch('http://127.0.0.1:8790/api/health');
    if (!response.ok) throw new Error('offline');
    health = await response.json();
  } catch (_) {
    throw new Error('请打开悠悠工作台后再采集；本次未提交');
  }
  if (health.source_intake_version !== 'SOURCE_INTAKE_20261018_SINGLE_SKU_V2') {
    throw new Error('请先保存编辑并正常重开工作台，以加载单SKU选择修复；本次未提交');
  }
};
function failure(e, step='background→8789') {
 return {ok:false,step:e.step||step,error:e.message,hint:e.hint||'接收端未启动，请重开悠悠工作台；若已启动，请检查本机端口与扩展白名单',background_extension_version:chrome.runtime.getManifest().version};
}
async function localRequest(url, value) {
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
 try {
  let r;
  try {r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Youyou-Collector':'1'},body:JSON.stringify(value),signal:controller.signal});}
  catch(e){throw Object.assign(Error((e.name==='AbortError'?'超时：':'连接失败（可能连接被拒绝）：')+e.message),{step:'background→8789'});}
  let body;
  try {body=await r.json();}catch(e){throw Object.assign(Error('HTTP '+r.status+' JSON解析失败：'+e.message),{step:'response_parse',hint:'请确认端口是悠悠采集接收端，再重开工作台'});}
  if(!r.ok)throw Object.assign(Error('HTTP '+r.status+'：'+(body.error||'服务端未提供错误')),{step:'receiver_http',hint:r.status===403?'扩展ID不在白名单；核对扩展ID与接收端配置后重开工作台':r.status===413?'请求体过大，请减少当前页候选数量':'请按服务端错误修正后重试'});
  return {...body,step:'saved',background_extension_version:chrome.runtime.getManifest().version};
 } finally {clearTimeout(timer);}
}
const marketContext = value => {
 if(value.detail)value={...value,detail:{...value.detail,background_extension_version:chrome.runtime.getManifest().version}};
 return localRequest('http://127.0.0.1:8789/api/market-reference',value);
};
chrome.tabs.onUpdated.addListener((tabId,change,tab)=>{
  if(change.url && /^https:\/\/[^/]*1688\.com\//.test(change.url) && tab.openerTabId)
    marketContext({operation:'inherit',tab_id:tabId,parent_tab_id:tab.openerTabId}).catch(()=>{});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if(message?.type==='YOUYOU_OFFER_RESERVE') {
    reserveOfferCapture().then(r=>sendResponse({...r,step:'rate_reserved',background_extension_version:chrome.runtime.getManifest().version})).catch(e=>sendResponse({...failure(e,'rate_storage'),hint:'间隔至少5秒、每日最多50次；若storage失败，请重新加载扩展授予storage权限并刷新本页'}));return true;
  }
  if(message?.type==='YOUYOU_OFFER_CANDIDATES_1688') {
    const host=(()=>{try{return new URL(sender.tab?.url||'').hostname;}catch(_){return '';}})();
    if(host!=='meli.zying.net'&&!/(^|\.)1688\.com$/.test(host)){sendResponse(failure(Object.assign(Error('来源页面不符'),{step:'sender_validation',hint:'请在智赢或1688允许的页面采集，并刷新本页'})));return false;}
    marketContext({operation:'offer_candidates_1688',detail:message.payload}).then(sendResponse).catch(e=>sendResponse(failure(e)));return true;
  }
  if(message?.type==='YOUYOU_ZYING_DOM_SNAPSHOT') {
    if(!/^https:\/\/meli\.zying\.net\//.test(sender.tab?.url||'')){sendResponse({ok:false,error:'来源页面不符'});return false;}
    marketContext({operation:'zying_dom_snapshot',detail:message.payload}).then(sendResponse).catch(e=>sendResponse(failure(e)));return true;
  }
  if(message?.type==='YOUYOU_ZYING_DETAIL') {
    if(!/^https:\/\/meli\.zying\.net\//.test(sender.tab?.url||'')){sendResponse(failure(Object.assign(Error('来源页面不符'),{step:'sender_validation',hint:'请在智赢或1688允许的页面采集，并刷新本页'})));return false;}
    marketContext({operation:'zying_detail',detail:{...message.payload, background_extension_version:chrome.runtime.getManifest().version}}).then(sendResponse).catch(e=>sendResponse(failure(e)));return true;
  }
  if(message?.type==='YOUYOU_MARKET_REFERENCE') {
    if(!/^https:\/\/meli\.zying\.net\//.test(sender.tab?.url||'')){sendResponse(failure(Object.assign(Error('来源页面不符'),{step:'sender_validation',hint:'请在智赢或1688允许的页面采集，并刷新本页'})));return false;}
    marketContext({operation:'bind',tab_id:sender.tab.id,market_reference:message.market_reference}).then(sendResponse).catch(e=>sendResponse(failure(e)));return true;
  }
  if (message?.type === 'YOUYOU_DETAIL_IMAGES') {
    try {
      const url = new URL(message.url || '');
      if (url.protocol !== 'https:' || (!/(^|\.)(1688\.com|alicdn\.com|tbcdn\.cn)$/i.test(url.hostname) && url.hostname !== 'itemcdn.tmall.com')) {
        sendResponse({ok: false, error: '详情地址不在允许范围'});
        return false;
      }
      fetch(url.toString()).then(async response => {
        if (!response.ok) throw new Error(`详情图读取失败 ${response.status}`);
        const html = (await response.text()).slice(0, 3000000);
        const images = [];
        const seen = new Set();
        const pattern = /(?:src|data-src|data-lazy-load-src)=["']([^"']+)["']|((?:https?:)?\\?\/\\?\/[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\\-]+\.(?:jpg|jpeg|png|webp)(?:\?[^"'\s<]*)?)/gi;
        let match;
        while ((match = pattern.exec(html)) && images.length < 60) {
          const value = (match[1] || match[2] || '').replace(/\\u002[fF]/g, '/').replace(/\\\//g, '/').replace(/&amp;/g, '&').replace(/^\/\//, 'https://');
          if (!/^https:\/\//i.test(value) || !/alicdn|tbcdn|1688/i.test(value) || seen.has(value)) continue;
          seen.add(value);
          images.push(value);
        }
        sendResponse({ok: true, images});
      }).catch(error => sendResponse({ok: false, error: error.message}));
      return true;
    } catch (error) {
      sendResponse({ok: false, error: error.message});
      return false;
    }
  }
  if (message?.type !== 'YOUYOU_COLLECT') return false;
  if (!/^https:\/\/detail\.1688\.com\/offer\/\d+\.html/i.test(sender.tab?.url || '')) {
    sendResponse({ok: false, error: '只允许采集1688商品详情页'});
    return false;
  }
  ensureSelectedSkuRuntime(message.payload).then(() =>
    marketContext({operation:'resolve',tab_id:sender.tab.id}).catch(()=>({}))
  ).then(context=>{
    if(context.market_reference)message.payload.market_reference=context.market_reference;
    return localRequest(LOCAL_COLLECT_URL, {...message.payload,background_extension_version:chrome.runtime.getManifest().version});
  }).then(sendResponse).catch(error => sendResponse(failure(error)));

  return true;
});
