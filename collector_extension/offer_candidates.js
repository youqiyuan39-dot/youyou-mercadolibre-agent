// Explicit user gesture only. Product content stays in this document until local POST.
(()=>{
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

 if(globalThis.__youyouOfferCandidates)return;globalThis.__youyouOfferCandidates=true;
 const {panel,input:sku,button,status:note}=YouyouCapturePanel.section('offers','采集1688同款候选');
 note.textContent='仅当前页，最多40条；请核对MLM编号。';
 button.addEventListener('click',async()=>{
  const id=sku.value.trim().toUpperCase();if(!/^MLM\d{6,20}$/.test(id)){note.textContent='请填写明确的 MLM 商品编号';return;}
  button.disabled=true;
  try{
   const gate=await YouyouLocal.send({type:'YOUYOU_OFFER_RESERVE'});
   if(!gate?.ok)throw Error(gate?.error||'限频检查失败');
   const result=YouyouOfferDom.read(document,location.href);
   const payload={...result,market_item_id:id,site_id:'MLM',source_url:location.origin+location.pathname,
    captured_at:gate.captured_at,rate_limit:gate.rate_limit,page_count:1};
   note.textContent=result.status==='BLOCKED_ON_LOGIN'?'遇到登录/验证码，已停止解析，请主人手动处理。':'正在保存到本机…';
   const response=await YouyouLocal.send({type:'YOUYOU_OFFER_CANDIDATES_1688',payload});
   if(!response?.ok)throw Error(response?.error||'本机未保存');
   note.textContent=(result.status==='BLOCKED_ON_LOGIN'?'BLOCKED_ON_LOGIN：请主人手动完成登录/验证；':'')+(response.message||response.status);
  }catch(e){note.textContent=e.message;}finally{button.disabled=false;}
 });
})();
