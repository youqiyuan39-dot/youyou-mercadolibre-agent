'use strict';
const BASE='http://127.0.0.1:8790/api/v3/selection/discovery/';
chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
 if(!sender.tab?.url?.startsWith('https://meli.zying.net/'))return false;
 if(!['CLAIM','RECEIVE'].includes(msg?.type))return false;
 (async()=>{
  const config=await chrome.storage.local.get('enabled');
  if(msg.type==='RECEIVE'&&!config.enabled)throw Error('采集已暂停；本次结果未提交');
  const value=msg.type==='CLAIM'?{enabled:config.enabled===true,execution:'EXTENSION'}:{...msg.value,execution:'EXTENSION'};
  const response=await fetch(BASE+(msg.type==='CLAIM'?'claim':'receive'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value),signal:AbortSignal.timeout(15000)});
  const data=await response.json();if(!response.ok)throw Error(data.error||'本机工作台未接收');return data;
 })().then(reply).catch(e=>reply({ok:false,error:e.message}));return true;
});
