'use strict';
async function show(){const d=await chrome.storage.local.get('enabled');document.getElementById('state').textContent=d.enabled?'已启用，仅在新品榜页面工作':'已暂停';}
document.getElementById('enable').onclick=async()=>{await chrome.storage.local.set({enabled:true});show();};
document.getElementById('disable').onclick=async()=>{await chrome.storage.local.set({enabled:false});show();};show();
