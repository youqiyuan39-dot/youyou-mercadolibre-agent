'use strict';
// No credentials, page scripts, network interception or third-party requests.
(()=>{
 let busy=false;
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const normalize=s=>(s||'').replace(/\s/g,'');
 const visible=e=>!!(e&&e.getClientRects().length);
 const rows=()=>[...document.querySelectorAll('tbody tr')].filter(r=>r.querySelectorAll('td').length===14);
 const loading=()=>[...document.querySelectorAll('.ant-spin-spinning,.el-loading-mask')].some(visible);
 const set=(e,v)=>{if(!e)throw Error('页面控件已变化');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,String(v));e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new Event('blur',{bubbles:true}));};
 const radio=(name,v)=>{const e=document.querySelector(`input[type=radio][name="${name}"][value="${v}"]`);if(!e)throw Error('页面筛选控件变化：'+name);if(!e.checked)e.click();};
 const checked=name=>document.querySelector(`input[type=radio][name="${name}"]:checked`)?.value;
 const send=msg=>chrome.runtime.sendMessage(msg);
 async function execute(req){
  if(location.href!==req.source_url)throw Error('请打开智赢新品榜；采集器不会接管其他页面');
  if(req.site_id!=='MLM')throw Error('当前采集器仅支持墨西哥');
  const mex=[...document.querySelectorAll('label')].find(e=>normalize(e.innerText)==='墨西哥');
  if(!mex?.querySelector('input[type=radio]')?.checked)throw Error('当前站点不是墨西哥，请切换后重新开始');
  const form=document.querySelector('form');if(!form)throw Error('登录失效或查询表单未加载');
  const reset=[...form.querySelectorAll('button')].find(e=>normalize(e.innerText)==='重置');
  if(!reset)throw Error('未识别重置入口');reset.click();await sleep(400);
  const fields=[...form.querySelectorAll('input[type=text]')].filter(e=>!e.placeholder);
  if(fields.length<3)throw Error('关键词控件结构变化');
  set(fields[0],req.query);set(fields[1],'');set(fields[2],'');
  const days=[15,30,60,90,180,365].find(n=>n>=req.listed_days)||365;
  radio('UpTime',days);radio('StockType','1');radio('Status','1');
  // Source-side sales controls may filter cumulative data; leave them unrestricted.
  for(const row of form.querySelectorAll('.zying-label-wrap')){
   const all=[...row.querySelectorAll('span')].find(e=>normalize(e.innerText)==='不限');if(all)all.click();
  }
  await sleep(300);
  const button=[...form.querySelectorAll('button')].find(e=>normalize(e.innerText)==='搜索');
  if(!button)throw Error('未识别搜索入口');
  const table=document.querySelector('.ant-table-wrapper');if(!table)throw Error('未识别商品表');
  let mutated=false,sawLoading=false,lastChange=Date.now();
  const observer=new MutationObserver(changes=>{if(changes.some(c=>c.type==='childList'||c.type==='characterData')){mutated=true;lastChange=Date.now();}});
  observer.observe(table,{subtree:true,childList:true,characterData:true});button.click();
  const began=Date.now();let confirmed=false;
  try{
   while(Date.now()-began<25000){
    if(loading())sawLoading=true;
    const empty=[...document.querySelectorAll('.ant-empty-description')].some(visible);
    if(Date.now()-began>1200&&!loading()&&(mutated||sawLoading)&&Date.now()-lastChange>700&&(rows().length||empty)){confirmed=true;break;}
    await sleep(200);
   }
  }finally{observer.disconnect();}
  if(!confirmed)throw Error('查询刷新或加载完成未确认；不把旧表格交作新结果');
  if(fields[0].value!==req.query||checked('StockType')!=='1'||checked('Status')!=='1'||checked('UpTime')!==String(days))throw Error('页面实际条件与任务不符');
  const captured=rows().map(r=>({cells:[...r.querySelectorAll('td')].map(c=>c.innerText),links:[...r.querySelectorAll('a[href]')].filter(a=>a.href.startsWith('https://articulo.mercadolibre.com.mx/')||a.href.startsWith('https://www.mercadolibre.com.mx/')).map(a=>({href:a.href,text:a.innerText})),image_url:r.querySelector('img')?.src||''}));
  const snapshot={url:location.href,query:req.query,captured_at:new Date().toISOString(),source_note:'智赢新品榜当前页；程序只读查询，未翻页、不代表全市场。',
   applied_filters:{query:fields[0].value,site_id:'MLM',listed_days:days,warehouse:'自发货',status:'在售'},
   page_state:{loading:false,row_count:captured.length,refresh_confirmed:true,refresh_basis:{dom_mutation:mutated,loading_seen:sawLoading}},rows:captured};
  return {task_id:req.task_id,request_id:req.id,outcome:'READ_OK',snapshot};
 }
 async function tick(){
  if(busy||location.hash!=='#/goods/newlist')return;
  busy=true;let req;
  try{const d=await send({type:'CLAIM'});if(!d?.ok||!d.request)return;req=d.request;
   let result;try{result=await execute(req);}catch(e){result={task_id:req.task_id,request_id:req.id,outcome:'PAGE_CHANGED'};console.warn('悠悠选品：'+e.message);}
   const received=await send({type:'RECEIVE',value:result});if(!received?.ok)console.warn('悠悠选品：'+(received?.error||'本次未接收'));
  }catch(e){console.warn('悠悠选品连接失败：'+e.message);}finally{busy=false;}
 }
 setInterval(tick,4000);tick();
})();
