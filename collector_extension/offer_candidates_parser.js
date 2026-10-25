// Pure current-DOM reader. No fetch, scripts, embedded state, clicks or pagination.
(function(g){
 const VERSION='ZYING_OFFER_DOM_SLICE8';
 const text=(v,n=300)=>String(v||'').trim().replace(/\s+/g,' ').slice(0,n);
 const visible=e=>!e.closest('[hidden]') && (!e.getBoundingClientRect || !!(e.getBoundingClientRect().width&&e.getBoundingClientRect().height));
 function safeUrl(raw,base){if(!raw)return '';try{const u=new URL(raw,base);if(u.protocol!=='https:'||u.username||u.password)return '';return (u.origin+u.pathname).slice(0,1000);}catch(_){return '';}}
 function read(doc,base){
  const body=doc.body||doc;
  const own=doc.querySelector('#youyou-capture-panel')||doc.querySelector('#youyou-offer-candidates');
  const pageText=(body.innerText||'').replace(own?.innerText||'','');
  const blocked=[...doc.querySelectorAll('.nc_iconfont,.baxia,.nc-container,#baxia-dialog-content,input[type="password"]')].some(visible)
   || /请登录|滑动验证|拖动滑块|安全验证|验证码/.test(pageText);
  if(blocked)return {status:'BLOCKED_ON_LOGIN',candidates:[],parser_version:VERSION};
  const candidates=[],seen=new Set();
  for(const a of [...doc.querySelectorAll('a')].slice(0,2000)){
   if(!visible(a))continue;
   const link=safeUrl(a.getAttribute('href'),base),m=link.match(/^https:\/\/detail\.1688\.com\/offer\/(\d{6,20})\.html$/);
   if(!m||seen.has(m[1]))continue;
   const card=a.closest('.offer-card')||a.closest('.offer-item')||a.closest('.search-offer-wrapper')||a.closest('li')||a;
   const field=s=>text(card.querySelector(s)?.innerText);
   const img=card.querySelector('img');
   candidates.push({offer_id:m[1],offer_url:link,title:field('.title,.offer-title')||text(a.getAttribute('title')||a.innerText),
    price:text(field('.price,.offer-price'),80),currency:'CNY',spec_summary:field('.spec,.spec-summary'),
    min_order:text(field('.moq,.min-order'),80),image_url:safeUrl(img?.getAttribute('src')||img?.getAttribute('data-src'),base),
    shop_name:text(field('.shop,.shop-name'),120)});
   seen.add(m[1]);if(candidates.length===40)break;
  }
  return {status:'READY',candidates,parser_version:VERSION};
 }
 const api={read,VERSION};if(typeof module!=='undefined')module.exports=api;g.YouyouOfferDom=g.YouyouOfferDom || api;
})(globalThis);
