// Shared, default-collapsed panel. Only user gestures start collection.
(()=>{
 if(globalThis.YouyouCapturePanel)return;
 let panel,body,sku,sections;
 const registry = new Map();
 function ensure(){
  if(panel)return;
  panel=document.createElement('section');panel.id='youyou-capture-panel';panel.setAttribute('aria-label','悠悠采集');
  panel.style.cssText='position:fixed;right:16px;bottom:16px;z-index:10000;background:#fff;color:#222;border:1px solid #777;border-radius:8px;font:14px/1.5 sans-serif;max-width:calc(100vw - 32px)';
  const toggle=document.createElement('button');toggle.textContent='悠悠采集 · 展开';toggle.setAttribute('aria-expanded','false');toggle.style.cssText='min-height:36px;padding:6px 12px;cursor:pointer';
  body=document.createElement('div');body.hidden=true;body.style.cssText='width:300px;max-width:calc(100vw - 58px);padding:12px;max-height:60vh;overflow:auto';
  sku=document.createElement('input');sku.placeholder='MLM 商品编号';sku.setAttribute('aria-label','MLM 商品编号');sku.maxLength=23;sku.style.cssText='box-sizing:border-box;width:100%;min-height:36px;margin-bottom:10px';
  sections=document.createElement('div');body.append(sku,sections);panel.append(toggle,body);document.body.append(panel);
  toggle.onclick=()=>{body.hidden=!body.hidden;toggle.textContent=body.hidden?'悠悠采集 · 展开':'悠悠采集 · 收起';toggle.setAttribute('aria-expanded',String(!body.hidden));};
 }
 globalThis.YouyouCapturePanel={section(key,title){ensure();if(registry.has(key))return registry.get(key);const group=document.createElement('div');group.dataset.capture=key;group.style.cssText='margin:8px 0';
  const button=document.createElement('button');button.textContent=title;button.style.cssText='min-height:36px;width:100%;cursor:pointer';
  const result=document.createElement('details');result.open=true;const summary=document.createElement('summary');summary.textContent=title+'结果';
  const status=document.createElement('div');status.setAttribute('role','status');status.style.cssText='max-height:160px;overflow:auto;overflow-wrap:anywhere;white-space:pre-wrap';result.append(summary,status);group.append(button,result);sections.append(group);
  const resultApi = {panel,input:sku,button,status,group};registry.set(key,resultApi);return resultApi;}};
})();
