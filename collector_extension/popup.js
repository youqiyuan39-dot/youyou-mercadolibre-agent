const portInput = document.querySelector('#port');
const tokenInput = document.querySelector('#token');
const collectButton = document.querySelector('#collect');
const statusBox = document.querySelector('#status');

chrome.storage.local.get(['port'], value => {
  if (value.port) portInput.value = value.port;
});

async function collectVisible1688Product() {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const originalScrollY = scrollY;
  const scrollLimit = Math.min(Math.max(0, document.documentElement.scrollHeight - innerHeight), 30000);
  const steps = Math.min(20, Math.max(1, Math.ceil(scrollLimit / Math.max(innerHeight * 0.8, 600))));
  for (let index = 0; index <= steps; index += 1) {
    scrollTo(0, Math.round(scrollLimit * index / steps));
    await new Promise(resolve => setTimeout(resolve, 220));
  }
  scrollTo(0, originalScrollY);
  await new Promise(resolve => setTimeout(resolve, 120));
  const canonicalImageUrl = value => {
    let url = clean(value).replace(/^\/\//, 'https://');
    if (!/^https:\/\//i.test(url)) return '';
    url = url.replace(/_(?:\d+x\d+|q\d+|\.webp).*$/i, '');
    return url;
  };
  const imageUrl = image => canonicalImageUrl(
    image.currentSrc || image.getAttribute('data-src') || image.getAttribute('data-lazy-src') ||
    image.getAttribute('data-original') || image.src
  );
  const contextOf = element => {
    const parts = [];
    let node = element;
    for (let index = 0; node && index < 6; index += 1, node = node.parentElement) {
      parts.push(`${node.id || ''} ${typeof node.className === 'string' ? node.className : ''}`);
    }
    return parts.join(' ').toLowerCase();
  };
  const nearbyTextOf = element => {
    let node = element.parentElement;
    let fallback = '';
    for (let index = 0; node && index < 6; index += 1, node = node.parentElement) {
      const value = clean(node.textContent).slice(0, 500);
      if (value && (!fallback || value.length < fallback.length)) fallback = value;
      if (/(?:库存|可售|进货数量|起批|产品规格|¥|￥)/.test(value) && value.length <= 500) return value;
    }
    return fallback.slice(0, 160);
  };
  const uniqueByUrl = (items, limit) => {
    const seen = new Set();
    return items.filter(item => {
      if (!item.url || seen.has(item.url)) return false;
      seen.add(item.url);
      return true;
    }).slice(0, limit);
  };
  const number = value => {
    const match = clean(value).replace(/,/g, '').match(/\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  };
  const titleCandidates = [
    document.querySelector('meta[property="og:title"]')?.content,
    document.querySelector('meta[name="title"]')?.content,
    document.title.replace(/\s*[-_]\s*阿里巴巴.*$/i, ''),
    ...[...document.querySelectorAll('h1')].map(node => node.textContent)
  ].map(clean).filter(value => value && !/有限公司$|供应商|旺铺/.test(value));
  const title = titleCandidates.sort((a, b) => b.length - a.length)[0] || '';
  const pageText = clean(document.body?.innerText).slice(0, 120000);
  const priceNodes = [...document.querySelectorAll('[class*="price"], [class*="Price"]')]
    .map(node => clean(node.textContent)).filter(text => /[¥￥]\s*\d/.test(text));
  const price = number(priceNodes[0]);
  const selectedVariant = [...document.querySelectorAll('[class*="selected"], [aria-checked="true"]')]
    .map(node => clean(node.textContent || node.getAttribute('title'))).filter(Boolean).slice(0, 12);
  const blockedImage = /logo|avatar|icon|qr|qrcode|sprite|emoji|loading|service|wangwang|-2-tps-|imgextra/i;
  const imageCandidates = [...document.images].map((image, order) => {
    const rect = image.getBoundingClientRect();
    const url = imageUrl(image);
    const context = contextOf(image);
    const label = clean(image.alt || image.title || image.closest('[title]')?.getAttribute('title'));
    const nearText = nearbyTextOf(image);
    return {
      url, order, context, label, nearText,
      top: Math.round(rect.top + scrollY),
      renderedWidth: Math.round(rect.width), renderedHeight: Math.round(rect.height),
      naturalWidth: image.naturalWidth || 0, naturalHeight: image.naturalHeight || 0
    };
  });
  const backgroundCandidates = [...document.querySelectorAll('[style*="background-image"]')].map((element, order) => {
    const rect = element.getBoundingClientRect();
    const match = getComputedStyle(element).backgroundImage.match(/url\(["']?([^"')]+)["']?\)/i);
    return {
      url: canonicalImageUrl(match?.[1]), order: imageCandidates.length + order,
      context: contextOf(element), label: clean(element.title || element.getAttribute('aria-label')),
      nearText: nearbyTextOf(element),
      top: Math.round(rect.top + scrollY), renderedWidth: Math.round(rect.width), renderedHeight: Math.round(rect.height),
      naturalWidth: 0, naturalHeight: 0
    };
  });
  const candidates = [...imageCandidates, ...backgroundCandidates].filter(item => item.url && /alicdn|tbcdn|1688/i.test(item.url) &&
    !blockedImage.test(`${item.url} ${item.context} ${item.label}`) &&
    Math.max(item.naturalWidth, item.naturalHeight, item.renderedWidth, item.renderedHeight) >= 120);

  const ownerCounts = new Map();
  candidates.filter(item => item.top < 1800).forEach(item => {
    const owner = item.url.match(/!!(\d+)-/)?.[1];
    if (owner) ownerCounts.set(owner, (ownerCounts.get(owner) || 0) + 1);
  });
  const dominantOwner = [...ownerCounts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
  const belongsToOffer = item => !dominantOwner || item.url.includes(`!!${dominantOwner}-`);
  const skuPattern = /sku|spec|variant|property|prop-|sale-prop|颜色|规格|型号|库存|可售|进货数量|起批|产品规格/;
  const detailPattern = /detail|description|desc-|rich-text|offer-content|商品详情|详情/;
  const galleryPattern = /gallery|preview|thumb|image-list|pic-list|main-image|vertical-img|carousel/;
  const skuImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && item.top < 2600 && skuPattern.test(`${item.context} ${item.nearText}`)
  ).map(item => ({
    url: item.url,
    variant: item.label || item.nearText,
    sku_id: ''
  })), 40);
  const skuUrls = new Set(skuImages.map(item => item.url));
  const detailImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && !skuUrls.has(item.url) &&
    (detailPattern.test(`${item.context} ${item.nearText}`) ||
      (item.top >= 1500 && Math.max(item.naturalWidth, item.naturalHeight, item.renderedWidth, item.renderedHeight) >= 300))
  ).map(item => ({url: item.url})), 40).map(item => item.url);
  const detailUrls = new Set(detailImages);
  let productImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && !skuUrls.has(item.url) && !detailUrls.has(item.url) &&
    item.top < 1500 && (galleryPattern.test(item.context) || item.naturalWidth >= 300 || item.renderedWidth >= 120)
  ).map(item => ({url: item.url, order: item.order})), 30).map(item => item.url);
  if (!productImages.length) {
    productImages = uniqueByUrl(candidates.filter(item => !detailUrls.has(item.url) && !skuUrls.has(item.url)), 30).map(item => item.url);
  }

  const packageData = {weight_g:null,length_cm:null,width_cm:null,height_cm:null,evidence:''};
  for (const table of document.querySelectorAll('table')) {
    const rows = [...table.querySelectorAll('tr')];
    if (rows.length < 2) continue;
    const headers = [...rows[0].querySelectorAll('th,td')].map(cell => clean(cell.textContent));
    const indexes = {
      length: headers.findIndex(value => /^长/.test(value)),
      width: headers.findIndex(value => /^宽/.test(value)),
      height: headers.findIndex(value => /^高/.test(value)),
      weight: headers.findIndex(value => /重量|件重|毛重|净重/.test(value))
    };
    if (Object.values(indexes).every(index => index < 0)) continue;
    const cells = [...rows[1].querySelectorAll('th,td')].map(cell => clean(cell.textContent));
    packageData.length_cm = indexes.length >= 0 ? number(cells[indexes.length]) : null;
    packageData.width_cm = indexes.width >= 0 ? number(cells[indexes.width]) : null;
    packageData.height_cm = indexes.height >= 0 ? number(cells[indexes.height]) : null;
    packageData.weight_g = indexes.weight >= 0 ? number(cells[indexes.weight]) : null;
    packageData.evidence = 'visible_package_table';
    break;
  }

  const shippingMatch = pageText.match(/(?:运费|快递|邮费)\s*[¥￥]?\s*(\d+(?:\.\d+)?)/);
  const freeShipping = /包邮/.test(pageText);
  const attributes = {};
  for (const row of document.querySelectorAll('table tr, [class*="attribute"] li, [class*="property"] li')) {
    const cells = [...row.querySelectorAll('th,td,span')].map(cell => clean(cell.textContent)).filter(Boolean);
    if (cells.length >= 2 && cells[0].length <= 80) attributes[cells[0]] = cells.slice(1).join(' ').slice(0, 300);
    if (Object.keys(attributes).length >= 80) break;
  }

  return {
    source_url: location.href,
    title,
    selected_variant: [...new Set(selectedVariant)],
    pricing: {amount: price, currency: 'CNY', evidence: price ? 'visible_selected_price' : ''},
    images: productImages,
    product_images: productImages,
    sku_images: skuImages,
    detail_images: detailImages,
    package: packageData,
    domestic_shipping: {amount: freeShipping ? 0 : number(shippingMatch?.[1]), free: freeShipping, evidence: shippingMatch || freeShipping ? 'visible_shipping_text' : ''},
    attributes
  };
}

collectButton.addEventListener('click', async () => {
  collectButton.disabled = true;
  statusBox.textContent = '正在读取当前商品…';
  try {
    const port = String(portInput.value || '8765').trim();
    const token = tokenInput.value.trim();
    if (!/^\d{2,5}$/.test(port) || !token) throw new Error('请填写本机端口和本次连接码');
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !/^https:\/\/detail\.1688\.com\/offer\/\d+\.html/i.test(tab.url || '')) {
      throw new Error('请先打开1688商品详情页');
    }
    const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: collectVisible1688Product});
    const response = await fetch(`http://127.0.0.1:${port}/collect`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Local-Intake-Token': token},
      body: JSON.stringify(result)
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `本机接收失败 ${response.status}`);
    await chrome.storage.local.set({port});
    tokenInput.value = '';
    const counts = value.image_counts || {};
    const imageText = `商品图${counts.product || 0}张、SKU图${counts.sku || 0}张、详情图${counts.detail || 0}张`;
    statusBox.textContent = value.draft_created
      ? `已采集${imageText}，并进入处理队列：${value.next_action}`
      : `已采集${imageText}并保存新证据；原任务未覆盖：${value.next_action}`;
  } catch (error) {
    statusBox.textContent = `失败：${error.message}`;
  } finally {
    collectButton.disabled = false;
  }
});
