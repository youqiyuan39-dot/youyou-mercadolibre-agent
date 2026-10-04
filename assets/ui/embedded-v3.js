(() => {
  const translations = {
    REAL_PROFIT_UNVERIFIED:'真实利润待核实',
    BENCHMARK_REFERENCE_NOT_REAL_PROFIT_VERIFIED:'对标价格参考，真实利润待核实',
    NO_GTIN_PENDING_CATEGORY_CHECK:'无条码依据待确认',
    USER_INPUT_UNVERIFIED:'人工填写，尚未核实',
    AI_ESTIMATED:'AI估算，尚未核实'
  };
  let store=null,queue=null;
  const routes=['/','/review','/publish'];
  function decorateSteps() {
    document.querySelectorAll('.steps>div').forEach((item,index)=>{
      const link=document.createElement('a'); link.href=routes[index]||'/'; link.className=item.className;
      while(item.firstChild)link.appendChild(item.firstChild);
      item.replaceWith(link);
    });
  }
  function translateLabels() {
    document.querySelectorAll('.meta strong,.meta span,.badge,.fact span').forEach(el=>{
      const value=el.textContent.trim(); if(translations[value]){el.title=value;el.textContent=translations[value];}
    });
  }
  function repairLocalThumbnails() {
    document.querySelectorAll('img[src^="/api/image-proxy?"]').forEach(img=>{
      const value=new URL(img.getAttribute('src'),location.origin).searchParams.get('url')||'';
      if(value&&!value.includes('..')&&!value.includes('://')&&/^(?:\/?assets\/)?[a-zA-Z0-9_./-]+\.(png|jpe?g|webp)$/i.test(value))img.src='/assets/'+value.replace(/^\/?assets\//,'');
    });
  }
  function reviewEvidence() {
    if(!queue)return;
    document.querySelectorAll('.review-card').forEach(card=>{
      if(card.querySelector('.review-checklist'))return;
      const link=card.querySelector('a[href*="draft="]');
      const name=link&&new URL(link.getAttribute('href'),location.origin).searchParams.get('draft');
      const row=queue.find(r=>r.name===name);
      if(!row||!row.publish_issues?.length)return;
      const note=document.createElement('small');note.className='review-checklist';
      note.textContent='需核实 '+row.publish_issues.length+' 项：'+row.publish_issues.slice(0,2).join('；');
      note.title=row.publish_issues.join('\n');
      const source=card.querySelector('.source');if(source)source.parentElement.appendChild(note);
    });
    const name=new URLSearchParams(location.search).get('draft');
    const row=queue.find(r=>r.name===name);
    if(row?.review_ready===false){
      document.querySelectorAll('button[data-approve]').forEach(b=>{b.disabled=true;b.title=(row.publish_issues||[]).join('；')||'审核资料仍需补齐';});
      if(!document.querySelector('#embeddedReviewIssues')){
        const note=document.createElement('div');note.id='embeddedReviewIssues';note.className='embedded-note';
        note.textContent='审核前需核实：'+(row.publish_issues||['商品资料']).join('；');
        const main=document.querySelector('main');if(main)main.prepend(note);
      }
    }
  }
  function storeDates() {
    if(!location.pathname.endsWith('/stores'))return;
    document.querySelectorAll('#storeRows td:nth-child(4)').forEach(cell=>{
      const text=cell.textContent.trim();if(!/^\d{4}-\d\d-\d\dT/.test(text))return;
      const date=new Date(text);if(Number.isNaN(date.getTime()))return;
      cell.title=text;cell.textContent=date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
    });
  }
  function reviewSearch() {
    const cards=[...document.querySelectorAll('.review-card')];
    if(!cards.length||document.querySelector('#embeddedReviewSearch'))return;
    const panel=cards[0].parentElement;
    const originalCount=panel.querySelector('.panel-head>span');
    if(originalCount)originalCount.dataset.embeddedHidden='true';
    const form=document.createElement('form');form.className='review-search';form.id='embeddedReviewSearch';
    form.innerHTML='<input type="search" aria-label="搜索待审核商品" placeholder="搜索标题、链接或SKU"><button type="submit" class="btn">搜索</button><button type="button" class="btn" data-reset>重置</button><small data-count></small>';
    panel.insertBefore(form,cards[0]);
    const pagination=document.createElement('div');pagination.className='review-pagination';
    pagination.innerHTML='<span data-page></span><button type="button" class="btn" data-prev>上一页</button><button type="button" class="btn" data-next>下一页</button>';
    panel.appendChild(pagination);
    const empty=document.createElement('div');empty.className='review-empty';empty.textContent='没有匹配的待审核商品';panel.appendChild(empty);
    let page=1,filtered=cards;
    function render() {
      const pages=Math.max(1,Math.ceil(filtered.length/20));page=Math.min(page,pages);
      for(const card of cards)card.classList.toggle('review-search-hidden',!filtered.includes(card));
      filtered.forEach((card,index)=>card.classList.toggle('review-page-hidden',index<(page-1)*20||index>=page*20));
      form.querySelector('[data-count]').textContent=filtered.length+' 条结果';
      pagination.querySelector('[data-page]').textContent='第 '+page+' / '+pages+' 页';
      pagination.querySelector('[data-prev]').disabled=page===1;
      pagination.querySelector('[data-next]').disabled=page===pages;
      empty.hidden=filtered.length>0;
    }
    form.onsubmit=e=>{e.preventDefault();const q=form.querySelector('input').value.trim().toLowerCase();filtered=cards.filter(c=>c.textContent.toLowerCase().includes(q));page=1;render();};
    form.querySelector('[data-reset]').onclick=()=>{form.querySelector('input').value='';filtered=cards;page=1;render();};
    pagination.querySelector('[data-prev]').onclick=()=>{page--;render();};
    pagination.querySelector('[data-next]').onclick=()=>{page++;render();};render();
  }
  function publishTarget() {
    if(!location.pathname.endsWith('/publish')||!store)return;
    const alias=store.alias||'未选择店铺';
    const target=document.querySelector('.target');
    if(target&&!target.dataset.v3Target){
      target.dataset.v3Target='true'; target.replaceChildren();
      const label=document.createElement('strong');label.textContent='当前预览店铺：'+alias;target.appendChild(label);
      const hint=document.createElement('small');hint.textContent='历史发布结果尚未逐条核对所属店铺，不代表当前店铺的发布记录。';target.appendChild(hint);
    }
    const select=document.querySelector('.panel.top select');
    if(select&&!select.dataset.v3Target){select.dataset.v3Target='true';select.replaceChildren(new Option(alias,store.store_id||''));select.disabled=true;select.title='请到店铺管理切换店铺';}
    const header=document.querySelector('main>header p');
    if(header&&header.textContent!=='本版提供商品校验与预览，正式发布暂未开放。')header.textContent='本版提供商品校验与预览，正式发布暂未开放。';
    document.querySelectorAll('#batchPublish,button[data-publish],button[data-retry]').forEach(button=>{button.disabled=true;button.title='正式发布暂未开放，请先完成发布目标绑定验收';});
  }
  function update() {decorateSteps();translateLabels();repairLocalThumbnails();reviewSearch();publishTarget();reviewEvidence();storeDates();}
  // DOM-only changes; no prices, credentials, approvals or external data are changed.
  const observer=new MutationObserver(()=>{observer.disconnect();update();observer.observe(document.querySelector('main')||document.body,{subtree:true,childList:true});});
  update();observer.observe(document.querySelector('main')||document.body,{subtree:true,childList:true});
  fetch('/api/store-status').then(r=>r.json()).then(data=>{store=data.store;update();}).catch(()=>{});
  if(location.pathname.endsWith('/review'))fetch('/api/drafts').then(r=>r.json()).then(data=>{queue=data.items||[];update();}).catch(()=>{});
})();
