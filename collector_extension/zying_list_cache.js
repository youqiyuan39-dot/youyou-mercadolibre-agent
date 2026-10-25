(function(root){
 const KEY='youyouZyingListV1',TTL=7*86400000,MAX=2000;
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
 function read(scope,now=Date.now()){
  const out=[];
  for(const table of scope.querySelectorAll('table')){
   const headScope=table.querySelector('th')?table:(table.closest('.el-table')||table);
   const heads=[...headScope.querySelectorAll('th')].map(x=>clean(x.innerText??x.textContent));
   for(const tr of table.querySelectorAll('tr')){
    const cells=[...tr.querySelectorAll('td')];if(!cells.length)continue;
    const txt=cells.map(c=>clean(c.innerText??c.textContent)).join(' '),ids=[...new Set(txt.match(/\bMLM\d{6,20}\b/g)||[])];if(ids.length!==1)continue;
    const record={sku_id:ids[0],cached_at:new Date(now).toISOString()};
    cells.forEach((cell,i)=>{const h=heads[i]||'',v=clean(cell.innerText??cell.textContent);
     if(/子类目|类目路径/.test(h))record.category_path=v.slice(0,600);
     if(/售价|价格|净收银价|净收益/.test(h)){
      const field=/净收银|净收益/.test(h)?'net_proceeds':'price';let text=v;
      if(!/MXN|USD|US\$|MX\$|美元|比索/i.test(v)&&/MXN|USD|当地货币/i.test(h))text=(/当地货币/.test(h)?'MXN':h.match(/MXN|USD/i)[0])+' '+v;
      record[field]=[record[field],text].filter(Boolean).join(' / ').slice(0,160);
     }
    });if(record.category_path||record.price||record.net_proceeds)out.push(record);
   }
  }return out.slice(0,MAX);
 }
 function bounded(old,rows,now=Date.now()){
  const merged={};for(const row of [...Object.values(old||{}),...rows]){
   if(!/^MLM\d{6,20}$/.test(row?.sku_id))continue;
   const age=now-Date.parse(row.cached_at);if(!Number.isFinite(age)||age<0||age>TTL)continue;
   merged[row.sku_id]=Object.fromEntries(['sku_id','cached_at','category_path','price','net_proceeds'].filter(k=>row[k]!=null).map(k=>[k,String(row[k]).slice(0,k==='category_path'?600:160)]));
  }return Object.fromEntries(Object.values(merged).sort((a,b)=>Date.parse(b.cached_at)-Date.parse(a.cached_at)).slice(0,MAX).map(r=>[r.sku_id,r]));
 }
 async function capture(scope){const rows=read(scope);const prior=(await chrome.storage.local.get(KEY))[KEY];const next=bounded(prior,rows);await chrome.storage.local.set({[KEY]:next});return rows.length;}
 async function merge(payload){const prior=(await chrome.storage.local.get(KEY))[KEY]||{},row=bounded(prior,[])[payload.sku_id];if(!row)return payload;
  payload.list_cache={cached_at:row.cached_at,sku_id:row.sku_id};
  for(const k of ['category_path','price','net_proceeds']){
   if(!row[k])continue;
   // Preserve current detail MXN; supplement only explicitly displayed USD.
   if(k==='price'&&payload.fields.price){const usd=row[k].match(/(?:USD|US\$|美元)\s*([\d.]+)|([\d.]+)\s*(?:USD|US\$|美元)/i);if(usd&&!/USD|US\$|美元/i.test(payload.fields.price)){payload.fields.price+=' / USD '+(usd[1]||usd[2]);payload.field_origin.price_usd='zying_list_cache';}}
   else if(!payload.fields[k]){payload.fields[k]=row[k];payload.field_origin[k]='zying_list_cache';}
  }return payload;
 }
 const api={read,bounded,capture,merge,KEY,TTL,MAX};if(typeof module==='object')module.exports=api;else root.YouyouZyingList=root.YouyouZyingList || api;
})(globalThis);
