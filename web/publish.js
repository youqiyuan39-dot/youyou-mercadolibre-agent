const app=document.querySelector("#app");
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let rows=[],stores=[],storeId="",filter="pending",query="",busy=false,currentPlan=null,pendingDeleteName="",actionNotice="";
async function api(url,options){const r=await fetch(url,options),d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||"请求失败");return d}
const post=(url,body)=>api(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const statusOf=r=>r.publish_legacy_unbound?"historical":r.publish_status||"pending";
const label=s=>({pending:"待发布",pending_confirmation:"待确认",submitting:"提交中",success:"全部成功",partial:"部分成功",failed:"失败",historical:"历史记录待核对"}[s]||s);
const title=r=>{const sites=r.publish_variants?.[0]?.sites||[];return r.publish_status==="success"&&r.publish_variants?.length===1&&sites.length===1&&sites[0].marketplace_title||r.listing_title||r.title||r.name};
const checkedAt=value=>Number.isFinite(Number(value))&&Number(value)>0?new Date(Number(value)*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}):"未回查";
function buyerLink(value){try{const url=new URL(value);return url.protocol==="https:"&&/(^|\.)mercadolibre\.com\.(mx|br|cl|co|ar)$/.test(url.hostname)?url.href:""}catch{return ""}}
function siteResults(r){return (r.publish_variants||[]).map(v=>`<div><b>${esc(v.seller_sku)}</b> ${(v.sites||[]).map(s=>{
 const link=buyerLink(s.marketplace_permalink);
 const details=s.marketplace_title||s.marketplace_description||s.content_check_error||link;
 return `<div>${esc(s.site_id)} ${esc(label(s.status))} ${esc(s.item_id||"")}${s.marketplace_status?" · 平台状态："+esc(s.marketplace_status):""}${s.checked_at?" · 回查："+esc(checkedAt(s.checked_at)):""} ${esc(s.error||s.check_error||"")}</div>${details?`<details><summary>平台实际内容 · 待人工核对</summary><p>平台标题：${esc(s.marketplace_title||"未取得")}</p><p>平台接口卖家价：${s.marketplace_price!=null?esc(s.marketplace_price)+" "+esc(s.marketplace_currency_id):"未取得"}；买家展示价以商品页为准。</p>${link?`<p><a href="${esc(link)}" target="_blank" rel="noopener noreferrer">打开买家商品页</a></p>`:""}<p>平台描述：${esc(s.marketplace_description||"未取得")}</p>${s.content_check_error?`<p class="warning">描述本次未取得：${esc(s.content_check_error)}</p>`:""}</details>`:""}`
 }).join("")}</div>`).join("")}
function issueSummary(values){
 const groups=new Map();
 for(const value of [...new Set(values||[])]){
  const match=String(value).match(/^(?:SKU )?([A-Za-z0-9_-]+)[：:]\s*(.*)$/);
  const reason=(match?match[2]:value).replace(/当前已配置 \d+ 张/,'图片不足').replace(/当前 \d+ 张/,'图片不足');
  const group=groups.get(reason)||{reason,skus:[]};if(match)group.skus.push(match[1]);groups.set(reason,group);
 }
 if(!groups.size)return '';
 return `<details class="issue-summary"><summary>待补资料：${groups.size} 类问题，展开查看</summary><ul>${[...groups.values()].map(g=>`<li>${esc(g.reason)}${g.skus.length?` <details><summary>涉及 ${g.skus.length} 个 SKU</summary>${g.skus.map(esc).join('、')}</details>`:''}</li>`).join('')}</ul></details>`;
}
function pricingReview(data, expanded=false){
 if(data?.state==='STALE')return '<p class="warning">旧预算已失效，请重新运行当前跟价策略；旧金额不能用于发布。</p>';
 if(data?.state==='BLOCKED'&&!(data.rows||[]).length)return `<details><summary>利润与定价待补资料</summary>${issueSummary(data.errors)}<p>补齐对应SKU的成本和价格依据后重新计算；利润门槛与计算口径需单独确认。</p></details>`;

 if(!data||data.state==='NOT_RUN')return '<p class="warning">利润定价尚未运行，请在商品处理完成或到利润与比价核对。</p>';
 return `<details class="pricing-review" ${expanded?"open":""}><summary>AI处理 · 利润与定价核对 <span class="summary-meta">${(data.rows||[]).length} SKU · ${data.requires_budget_review?"待人工核对":"查看预算与假设"}</span></summary>${[...new Set(data.warnings||[])].map(x=>`<p class="warning">${esc(x)}</p>`).join('')}${issueSummary(data.errors)}<table><thead><tr><th>SKU</th><th>基础成本 CNY</th><th>预算净回款 USD</th><th>贡献利润 USD</th><th>${data.method==='FOLLOW_REMOTE_MARKET_PRICE'?'销售收入贡献利润率':'净回款利润率'}</th><th>参考买家售价 ${data.method==='FOLLOW_REMOTE_MARKET_PRICE'?'USD':'MXN'}</th></tr></thead><tbody>${(data.rows||[]).map(r=>`<tr><td>${esc(r.variant||r.sku_id)}<details><summary>成本及假设</summary><p>采购 ${esc(r.purchase_cny)} / 国内运费 ${esc(r.domestic_cny)} / 货代 ${esc(r.forwarder_cny)} / 包材 ${esc(r.packaging_cny)} CNY；广告 ${esc(r.ads_budget_usd)} / 税费预留 ${esc(r.tax_reserve_usd)} USD；退货预留 ${esc(r.returns_reserve_cny)} CNY</p>${(r.assumptions||[]).map(x=>`<p>${esc(x)}</p>`).join('')}</details></td><td>${esc(r.base_cost_cny)}</td><td>${esc(r.net_proceeds_usd)}</td><td>${esc(r.unit_contribution_usd)}</td><td>${esc(r.sales_margin_pct??r.margin_pct)}%</td><td>${esc(r.buyer_price_usd??r.market_assessment?.gross_mxn_reference??'未取得')}</td></tr>`).join('')}</tbody></table><p>${esc(data.limitations)}</p></details>`;
}
function rowGuidance(r){
 const issues=r.publish_issues||[],s=statusOf(r),checks=[['图片|图库|原图','图片','#sourceImages'],['包装|打包|重量|尺寸','包装','#physical'],['属性|BRAND|MATERIAL|型号|类目','属性 / 类目','#attributes'],['利润|定价|净回款|收益|价格|采购','利润 / 定价','#pricing'],['目标站点为空|未选择.*站点|缺少目标站点','站点','#sites']];
 if(r.publish_legacy_unbound)return {tone:'neutral',text:'历史记录待核对',next:'先核对平台已有商品，避免重复创建',links:[]};
 if(['submitting','pending_confirmation'].includes(s))return {tone:'info',text:s==='submitting'?'正在提交':'等待平台回查',next:'等待或回查现有结果；请勿重复提交',links:[]};
 if(s==='success')return {tone:'success',text:'平台已有成功记录',next:'核对平台实际文案、图片与售价',links:[]};
 const links=checks.filter(([pattern])=>issues.some(x=>new RegExp(pattern,'i').test(x))).map(([,text,hash])=>({text,hash}));
 return {tone:issues.length?'warning':s==='failed'?'danger':'info',text:issues.length?'待补资料':s==='failed'?'发布失败':'待核对发布',next:issues.length?'先补齐下列资料，再核对发布':s==='failed'?'查看失败原因，修正后重试':'核对店铺、各 SKU 内容、图片与利润',links};
}
function render(){
 const counts={pending:0,pending_confirmation:0,success:0,partial:0,failed:0,historical:0};
 rows.forEach(r=>{const s=statusOf(r)==="submitting"?"pending_confirmation":statusOf(r);counts[s]=(counts[s]||0)+1});
 const shown=rows.filter(r=>(statusOf(r)==="submitting"?"pending_confirmation":statusOf(r))===filter&&(`${title(r)} ${r.name} ${r.category_id||""}`).toLowerCase().includes(query));
 app.innerHTML=`<header><div><div class="crumb">工作台 / 发布中心</div><h1>发布中心</h1><p>AI 处理完成自动进入这里。选择目标店铺，点击发布后核对内容并确认提交。</p></div></header>
 <section class="steps"><div><b>1</b>商品处理</div><div class="active"><b>2</b>发布中心</div></section>
 <section class="panel top"><div class="tabs">${Object.keys(counts).map(s=>`<button data-tab="${s}" class="${filter===s?"active":""}">${label(s)} ${counts[s]}</button>`).join("")}</div>
 <label>目标店铺 <select id="publishStore" ${busy?"disabled":""}><option value="">请选择目标店铺</option>${stores.map(s=>`<option value="${esc(s.id)}" ${storeId===s.id?"selected":""}>${esc(s.alias||s.id)} · ${esc(s.merchant_id)}</option>`).join("")}</select></label></section>
 <p class="publish-guidance" role="note">${storeId?"点击发布会先展示本次商品、店铺、价格、库存和图片；你确认后才会提交。平台仍可能返回新的校验错误。":"请先选择目标店铺，再点击发布核对商品。"}</p>
 <p id="actionNotice" role="status">${esc(actionNotice)}</p>
 <section class="panel"><div class="toolbar"><h2>${label(filter)}商品</h2><div><input id="search" type="search" aria-label="搜索发布商品" value="${esc(query)}" placeholder="搜索商品"><button id="searchButton">搜索</button><span>${shown.length} 条</span></div></div>
 ${shown.map(r=>{const s=statusOf(r),issue=(r.publish_issues||[]).join("；"),legacy=r.publish_legacy_unbound;
 const guidance=rowGuidance(r);
 const canPrepare=!legacy&&["pending","failed","partial"].includes(s);
 return `<article class="publish-row bound-publish-row"><img src="${esc(r.thumbnail?"/api/image-proxy?url="+encodeURIComponent(r.thumbnail):"")}" alt=""><div class="product"><div class="product-heading"><b>${esc(title(r))}</b><span class="row-status row-status--${guidance.tone}">${esc(guidance.text)}</span></div><small>草稿：${esc(r.name)} · ${esc(r.sku_count||1)} SKU</small>
 <dl><div><dt>站点</dt><dd>${esc((r.sites_to_sell||[]).map(x=>x.site_id||x).join("、")||"未选择")}</dd></div><div><dt>类目</dt><dd>${esc(r.category_id||"未选择")}</dd></div><div><dt>库存</dt><dd>${esc(r.available_quantity??"各 SKU 独立")}</dd></div><div><dt>包装重量</dt><dd>${r.weight_g==null?"未取得":esc(r.weight_g)+" g"}</dd></div></dl>
 <p class="row-description">${legacy?"历史发布记录尚未绑定店铺，需先核对平台，不能重复创建":s==="success"?"平台已上架；请核对买家页文案与售价，利润仍是预算。":esc(r.publish_error||(!issue?"点击发布核对内容":"有资料待补，查看下方汇总"))}</p>${r.publish_store?`<p>发布记录归属：${esc(r.publish_store.alias||r.publish_store.id)} · ${esc(r.publish_store.merchant_id)}</p>`:""}${s==="pending_confirmation"&&r.publish_bound?`<p>自动回查 ${esc(r.publish_auto_refresh_attempts||0)}/3 次；结果仍不明时禁止重复创建，可手动回查。</p>`:""}<div class="row-next"><strong>下一步</strong><span>${esc(guidance.next)}</span>${guidance.links.map(link=>`<a href="/editor?draft=${encodeURIComponent(r.name)}${link.hash}">${esc(link.text)} →</a>`).join('')}</div>${issueSummary(r.publish_issues)}${siteResults(r)}${pricingReview(r.ai_processing_pricing)}</div>
 <div class="actions row-actions"><a class="${(r.publish_issues||[]).length?"edit-primary":"secondary"}" href="/editor?draft=${encodeURIComponent(r.name)}">${(r.publish_issues||[]).length?"补齐资料":"编辑"}</a>${canPrepare?`<button class="${(r.publish_issues||[]).length?"secondary":"publish-primary"}" data-publish="${esc(r.name)}" ${!storeId||busy?"disabled":""} title="${!storeId?"请先选择目标店铺":"点击后核对发布内容"}">${s==="partial"?"继续发布失败站点":"发布"}</button>`:""}
 ${r.publish_bound?`<button class="secondary" data-refresh="${esc(r.name)}" data-store="${esc(r.publish_store.id)}" ${busy?"disabled":""}>回查结果</button>`:""}
 <a class="secondary" href="/editor?draft=${encodeURIComponent(r.name)}#aiImages">AI 生图处理</a>
 <button class="secondary danger" data-delete="${esc(r.name)}" ${busy||["submitting","pending_confirmation"].includes(s)?"disabled":""} title="删除本机商品到回收站，可恢复">删除</button></div></article>`}).join("")||`<div class="empty"><strong>${query?'未找到匹配的商品':'当前分栏没有记录'}</strong><p>${query?'可更换商品名、SKU 或类目关键词。':'选择其他状态查看商品。'}</p></div>`}</section>
 <dialog id="deleteDialog" aria-labelledby="deleteHeading"><h2 id="deleteHeading">删除本机商品</h2><p id="deleteTitle"></p><p>商品将移入本机回收站，可恢复；原始采集、图片和平台商品保留。</p><div class="actions"><button id="cancelDelete" class="secondary">取消</button><button id="confirmDelete" class="secondary danger">删除到回收站</button></div></dialog>
 <dialog id="previewDialog" style="max-width:1050px;width:90%"><button data-close class="dialog-close">关闭</button><h2>核对并确认发布（含利润定价）</h2><div id="previewBody"></div><div id="previewFooter" class="preview-actions"></div></dialog>`;
}
async function load(force=false){
 const d=await api("/api/v3/products"+(storeId?"?publish_store="+encodeURIComponent(storeId):""));
 const next=(d.items||[]).filter(r=>r.in_publish_center);
 if(force||JSON.stringify(next)!==JSON.stringify(rows)||!document.querySelector("#publishStore")){rows=next;render()}
}
async function preview(name){
 busy=true;render();
 try{
  const p=(await post("/api/v3/publish/preview",{name,store_id:storeId})).preview;
  currentPlan=p;
  document.querySelector("#previewBody").innerHTML=`${pricingReview(p.ai_processing_pricing,true)}
   <h3>${esc(p.title)}</h3><p><b>目标店铺：${esc(p.store.alias||p.store.id)} · ${esc(p.store.merchant_id)}</b></p>
   <p class="${p.valid?"preview-ok":"warning"}">${p.valid?"可以提交，请核对商品内容、价格与预算利润":"发现待处理问题，本次不会发布"}</p>
   ${issueSummary(p.issues)}
   <p>${!p.valid?"下表仅供核对；补齐上述问题后重新点击发布。":p.retry_failed_only?"仅提交下表失败站点；已经创建的站点不会重新创建。":"将按下表创建商品。"}</p>
   <details open><summary>标题来源与站点文案</summary><p>提交的商品族名称：${esc(p.title||"未填写")}。本地站点标题仅作建议，不在本次创建请求中；买家端标题与描述须以发布后的平台回读和实页为准。</p>${(p.site_title_suggestions||[]).map(x=>`<p>${esc(x.seller_sku)} · ${esc(x.site_id)} 本地建议：${esc(x.title)}${x.site_id==="MLM"&&x.title?.trim().toLowerCase()===p.title?.trim().toLowerCase()?" · 建议标题仍与商品族名称相同，未做独立西语本地化；发布后须核对平台生成结果。":""}</p>`).join("")||"<p>暂无站点建议标题。</p>"}</details>
   <table><thead><tr><th>SKU</th><th>包装重量</th><th>库存</th><th>站点 / 物流</th><th>计价方式（USD）</th></tr></thead><tbody>
   ${(p.request||[]).flatMap(r=>(r.sites_to_sell||[]).map(s=>`<tr><td>${esc((r.attributes||[]).find(a=>a.id==="SELLER_SKU")?.value_name||"")}</td><td>${esc((r.attributes||[]).find(a=>a.id==="PACKAGE_WEIGHT")?.value_name||"未取得")}</td><td>${esc(r.available_quantity)}</td><td>${esc(s.site_id)} / ${esc(s.logistic_type)}</td><td>${s.price!=null?"售价 "+esc(s.price):"净到账目标 "+esc(s.net_proceeds)+"（不是利润）"}</td></tr>`)).join("")}</tbody></table>
   ${(p.request||[]).map(r=>`<details><summary>核对商品描述</summary><p>${esc(r.description?.plain_text||"未填写")}</p></details>`).join("")}
   ${(p.image_sets||[]).length?p.image_sets.map(set=>`<section class="preview-image-set"><h4>${esc(set.seller_sku)} · 本次提交 ${esc(set.count)} 张图片</h4><p>请核对规格、结构、文字和图片顺序，第一张为首图。</p><div class="preview-gallery">${set.files.map((path,index)=>`<img src="/assets/${esc(path)}" alt="${esc(set.seller_sku)} 第 ${index+1} 张待发布图片" style="max-width:160px;max-height:160px;margin:6px">`).join("")}</div></section>`).join(""):`<div class="preview-gallery">${Object.keys(p.image_hashes||{}).map(path=>`<img src="/assets/${esc(path)}" alt="待发布图片" style="max-width:160px;max-height:160px;margin:6px">`).join("")}</div>`}
   ${(p.capacity||[]).length?`<p>刊登额度：${p.capacity.map(c=>`${esc(c.site_id)} / ${esc(c.logistic_type)} 当前 ${esc(c.total_items)}，上限 ${c.quota===0?"不限":esc(c.quota)}，本次 ${esc(c.requested)}`).join("；")}</p>`:""}
   <p>${esc(p.note)}</p>
   <details><summary>查看完整发布内容与类目检查</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify({request:p.request,category:p.category_validation},null,2))}</pre></details>`;
  document.querySelector("#previewFooter").innerHTML=`${p.valid&&p.ai_processing_pricing?.requires_budget_review?'<label class="confirm-box"><input type="checkbox" id="confirmBudget"> 我已核对同款规格、参考售价和净收益、预算成本及广告税费退货假设，并确认所示利润率和计算口径</label>':''}
   ${p.valid?'<label class="confirm-box"><input type="checkbox" id="confirmPublish"> 我已统一核对这件商品、目标店铺、各 SKU 的站点、价格、库存、描述及上方全部图片，确认真实提交</label><p>图片在此统一确认，无需额外逐图打标。核对结果有效期 15 分钟。商品修改后请重新点击发布。</p>':'<p class="warning">请先处理上方问题，再重新点击“发布”核对。</p>'}<button id="commitPublish" disabled>确认发布到此店铺</button>`;
  document.querySelector("#previewDialog").showModal();
 }finally{busy=false;document.querySelector("#previewDialog")?.addEventListener("close",()=>{if(!busy){currentPlan=null;render()}},{once:true})}
}
document.addEventListener("keydown",e=>{
 if(e.key==='Enter'&&e.target.id==='search'){e.preventDefault();query=e.target.value.trim().toLowerCase();render();}
});
document.addEventListener("change",async e=>{
 try{
  if(e.target.id==="publishStore"){storeId=e.target.value;currentPlan=null;await load(true)}
  if(['confirmPublish','confirmBudget'].includes(e.target.id))document.querySelector('#commitPublish').disabled=!document.querySelector('#confirmPublish')?.checked||(currentPlan?.ai_processing_pricing?.requires_budget_review&&!document.querySelector('#confirmBudget')?.checked)||busy;
 }catch(error){alert(error.message)}
});
document.addEventListener("click",async e=>{
 const b=e.target.closest("button");
 if(!b||b.disabled||busy)return;
 try{
  if(b.hasAttribute("data-close")){if(!busy){document.querySelector("#previewDialog").close();currentPlan=null;render()}return}
  if(b.dataset.tab){filter=b.dataset.tab;render();return}
  if(b.id==="searchButton"){query=document.querySelector("#search").value.trim().toLowerCase();render();return}
  if(b.dataset.publish){await preview(b.dataset.publish);return}
  if(b.id==="commitPublish"){
   if(busy||!currentPlan||!document.querySelector("#confirmPublish")?.checked)return;
   busy=true;b.disabled=true;
   const d=await post("/api/v3/publish/commit",{plan_id:currentPlan.id,confirmed:true,budget_review_confirmed:document.querySelector("#confirmBudget")?.checked===true});
   document.querySelector("#previewDialog").close();currentPlan=null;busy=false;
   alert(`平台结果：${label(d.record.status)}\n${d.record.error||d.warning||"已保存店铺绑定记录"}`);
   filter=d.record.status==="submitting"?"pending_confirmation":d.record.status;await load(true);return;
  }
  if(b.dataset.refresh){busy=true;const d=await post("/api/v3/publish/refresh",{name:b.dataset.refresh,store_id:b.dataset.store});busy=false;alert(`回查结果：${label(d.record.status)}\n${d.record.error||""}`);await load(true);return}
  if(b.dataset.delete){
   const row=rows.find(r=>r.name===b.dataset.delete);
   pendingDeleteName=b.dataset.delete;document.querySelector('#deleteTitle').textContent=title(row||{name:pendingDeleteName});
   document.querySelector('#deleteDialog').showModal();return;
  }
  if(b.id==='cancelDelete'){pendingDeleteName='';document.querySelector('#deleteDialog').close();return}
  if(b.id==='confirmDelete'){
   if(!pendingDeleteName)return;
   busy=true;b.disabled=true;currentPlan=null;
   await post("/api/drafts/batch",{action:"recycle",names:[pendingDeleteName]});
   pendingDeleteName='';document.querySelector('#deleteDialog').close();
   actionNotice="已移入本机回收站，可恢复。";busy=false;await load(true);return;
  }
 }catch(error){busy=false;if(b.id==='confirmDelete'){pendingDeleteName='';document.querySelector('#deleteDialog')?.close();actionNotice=error.message;await load(true);return}alert(error.message);if(b.id==="commitPublish"){b.disabled=true;currentPlan=null;document.querySelector("#previewDialog")?.close()}await load(true)}
});
async function refreshQueue(){
 if(!busy&&!document.querySelector("#previewDialog")?.open&&!document.querySelector("#deleteDialog")?.open){
  try{await load()}catch(error){console.error(error.message)}
 }
 setTimeout(refreshQueue,3000)
}
api("/api/v3/publish/stores").then(async d=>{stores=d.stores||[];const status=await api("/api/store-status").catch(()=>null);const active=status?.store?.active_store_id;if(stores.some(s=>s.id===active))storeId=active;return load(true)}).then(refreshQueue).catch(error=>{app.innerHTML=`<p class="warning">${esc(error.message)}</p>`});
