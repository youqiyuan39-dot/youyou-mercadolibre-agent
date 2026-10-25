// Click-only visible DOM intake. No page internals, storage, network interception or remote fetch.
(() => {
  if (typeof YouyouCapturePanel === 'undefined') {
    if (!document.getElementById('youyou-component-missing')) {
      const notice = document.createElement('div');
      notice.id = 'youyou-component-missing';
      notice.setAttribute('role', 'alert');
      notice.textContent = '扩展组件未加载，请重载扩展并刷新页面';
      notice.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:10000;background:#fff;color:#a00;padding:12px;border:1px solid #a00';
      (document.body || document.documentElement).append(notice);
    }
    return;
  }

  if(globalThis.__youyouZyingDetail) return;
  globalThis.__youyouZyingDetail = true;
  const {panel,input,button,status,group}=YouyouCapturePanel.section('detail','采集智赢详情');
  status.textContent='仅当前单商品详情；缺项进入补填表';
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && !panel.contains(el);
  const safeUrl = value => {const u=new URL(value);u.search='';u.hash=u.hash.split('?')[0];return u.href;};
  const cacheButton=document.createElement('button');cacheButton.textContent='缓存当前列表类目/价格';group.append(cacheButton);
  cacheButton.onclick=async()=>{try{status.textContent='已缓存 '+await YouyouZyingList.capture(document)+' 行（7天有效，最多2000条）';}catch(e){status.textContent=e.message;}};
  const diagnosticButton=document.createElement('button');diagnosticButton.textContent='导出计算器区 DOM 片段';group.append(diagnosticButton);
  diagnosticButton.onclick=async()=>{try{
    const scope=document.querySelector('main')||document.body;
    const snapshot=YouyouZyingDetail.domSnapshot(scope,visible);
    const result=await YouyouLocal.send({type:'YOUYOU_ZYING_DOM_SNAPSHOT',payload:{sku_id:input.value.trim().toUpperCase(),site_id:'MLM',source_url:safeUrl(location.href),captured_at:new Date().toISOString(),snapshot}});
    if(!result?.ok)throw Error(result?.error||'本次未存档');status.textContent='诊断已存档：'+result.payload_archive;
  }catch(e){status.textContent=e.message;}};
  button.onclick = async () => {
    button.disabled = true;
    try {
      const payload = await YouyouZyingDetail.waitForSnapshot(() => {
        // Reacquire SPA roots on every attempt; a calculator can sit outside a modal.
        const scope = document.querySelector('main') || document.body;
        const text = scope.innerText.replace(panel.innerText,'');
        const ids = text.match(/\bML[A-Z]\d{6,20}\b/gi) || [];
        const entries = YouyouZyingDetail.readDOM(scope,visible);
        const selectedImage = YouyouZyingDetail.mainImage(scope,document,visible);
        const value = YouyouZyingDetail.parse({skuId:input.value,visibleIds:ids,entries,
          sourceUrl:safeUrl(location.href),capturedAt:new Date().toISOString(),
          mainImage:selectedImage?safeUrl(selectedImage):''});
        value.extension_version = chrome.runtime.getManifest().version;
        value.diagnostics = YouyouZyingDetail.diagnostics(scope,entries,value,visible);
        return value;
      });
      await YouyouZyingList.merge(payload);
      const warnings = Object.entries(payload.diagnostics.fields).filter(([,v])=>!v.hit).map(([k,v])=>k+'：'+v.reason).join('；');
      status.textContent = warnings || '关键字段已读取，正在保存';
      if(new TextEncoder().encode(JSON.stringify({operation:'zying_detail',detail:payload})).length>32768) throw Error('详情数据超过32KB，本次未提交');
      const result = await YouyouLocal.send({type:'YOUYOU_ZYING_DETAIL',payload});
      if(!result?.ok) throw Error(result?.error || '本地接收失败');
      status.textContent = `已保存 ${result.name}；${result.unshippable?'不可发（超过15kg）':result.status}；未自动入队；${warnings}`;
    } catch(error) {status.textContent = /Extension context invalidated/i.test(error.message)?'[content→background] '+error.message+'；扩展已更新请刷新本页':error.message;}
    finally {button.disabled = false;}
  };
})();
