// User-visible table fields only. A click on the site's 1688 search carries its row.
(() => {
  if(globalThis.__youyouMarketListener)return;globalThis.__youyouMarketListener=true;
  function readRow(row) {
    let headers=[...row.closest('table').querySelectorAll('thead th')].map(x=>x.innerText.trim());
    // Fixed headers are rendered in a different table on the real Zhiying page.
    if(!headers.length)headers=[...document.querySelectorAll('table thead th')].map(x=>x.innerText.trim());
    const cells=[...row.querySelectorAll('td')].map(x=>x.innerText.trim());
    if(headers.length!==cells.length)return null;
    const at=name=>cells[headers.indexOf(name)]||'';
    const amount=text=>{const m=text.match(/([0-9]+(?:\.[0-9]+)?)\s*美元/);return m?m[1]:null;};
    const item=row.innerText.match(/MLM\d{6,20}/)?.[0];
    if(!item||!at('库存').includes('自发货'))return null;
    return {market_item_id:item,site_id:'MLM',fulfillment:'remote',buyer_price_usd:amount(at('价格')),
      net_proceeds_usd:amount(at('净收益价')),observed_at:new Date().toISOString(),
      title:row.querySelector('a')?.innerText||'',category_path:at('子类目'),spec_match:'SOURCE_SELECTED_PENDING_REVIEW'};
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');
    if(!button||!button.innerText.includes('1688同款'))return;
    const row=button.closest('tr');if(!row)return;
    const ref=readRow(row);if(ref)YouyouLocal.send({type:'YOUYOU_MARKET_REFERENCE',market_reference:ref}).catch(()=>{});
  },true);
})();
