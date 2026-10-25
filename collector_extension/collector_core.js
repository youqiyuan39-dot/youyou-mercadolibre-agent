globalThis.collectVisible1688Product = globalThis.collectVisible1688Product || async function collectVisible1688Product() {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  // Prefer 1688's structured page state. DOM text is only a fallback because
  // supplier names and recommendation cards can look like product data.
  const contextData = (() => {
    document.dispatchEvent(new CustomEvent('youyou:extract-structured'));
    const bridged = document.documentElement.getAttribute('data-youyou-structured');
    if (bridged) {
      try {
        const value = JSON.parse(bridged);
        if (value && !value.error) return value;
      } catch (_error) {}
    }
    try {
      const ctx = window.context?.result?.data;
      const root = ctx?.Root?.fields?.dataJson;
      if (!ctx || !root) return null;
      const productTitle = ctx.productTitle?.fields || {};
      const priceModel = ctx.mainPrice?.fields?.priceModel || {};
      const finalPrice = ctx.mainPrice?.fields?.finalPriceModel || {};
      const trade = finalPrice.tradeWithoutPromotion || {};
      const skuRows = Array.isArray(trade.skuMapOriginal) ? trade.skuMapOriginal : Object.values(trade.skuMapOriginal || {});
      const gallery = ctx.gallery?.fields?.offerImgList || [];
      const descriptionUrl = clean(ctx.description?.fields?.detailUrl);
      const skuImageMap = trade.skuImageMap || {};
      const canonical = value => clean(value).replace(/^\/\//, 'https://');
      const skuImageSets = [];
      const walkSkuImages = (value, path = []) => {
        if (typeof value === 'string' && /^(?:https?:)?\/\//i.test(value) && /alicdn|tbcdn|1688/i.test(value)) {
          const variant = clean(path.join(' / ')).slice(0, 300);
          const last = skuImageSets[skuImageSets.length - 1];
          if (last && last.variant === variant) {
            if (!last.images.includes(canonical(value))) last.images.push(canonical(value));
          } else {
            skuImageSets.push({variant, sku_id: '', images: [canonical(value)]});
          }
        } else if (value && typeof value === 'object') {
          Object.entries(value).forEach(([key, child]) => walkSkuImages(child, [...path, clean(key)]));
        }
      };
      walkSkuImages(skuImageMap);
      let packInfo = ctx.productPackInfo?.fields?.pieceWeightScale;
      if (typeof packInfo === 'string') packInfo = JSON.parse(packInfo);
      const packRows = YouyouPackaging.rows(packInfo);
      const weightBySku = new Map(packRows.filter(row => row?.skuId).map(row => [String(row.skuId), row]));
      const skus = skuRows.map(row => {
        const parts = typeof row.specAttrs === 'string'
          ? row.specAttrs.replace(/&gt;/g, '>').split('>').map(clean).filter(Boolean)
          : [];
        const pack = YouyouPackaging.match(row, packRows, skuRows.length);
        return {
          sku_id: clean(row.skuId), spec_id: clean(row.specId),
          variant: parts.join(' / '), price: Number(row.discountPrice || row.price || 0) || null,
          inventory: Number(row.canBookCount ?? row.amountOnSale ?? 0),
          package: pack
        };
      });
      const orderParam = root.orderParamModel?.orderParam || {};
      const temp = root.tempModel || {};
      const sellerModel = root.frontSellerMemberModel || {};
      let shopInfo = productTitle.shopInfo || {};
      if (typeof shopInfo === 'string') { try { shopInfo = JSON.parse(shopInfo); } catch (_error) { shopInfo = {}; } }
      const services = ctx.mainServices?.fields?.guaranteeList || [];
      return {
        title: clean(productTitle.title || root.tempModel?.offerTitle),
        productImages: gallery.map(item => typeof item === 'string' ? item : (item?.fullPathImageURI || item?.imageURI || item?.url)).filter(Boolean),
        pricingTiers: (priceModel.currentPrices || priceModel.originalPrices || []).map(row => ({
          min_quantity: Number(row.beginAmount) || null, price: Number(row.price) || null
        })),
        minimumOrder: Number(root.orderParamModel?.beginNum || orderParam.beginNum) || null,
        skus,
        skuImageSets,
        packageRows: packRows,
        descriptionUrl,
        unit: clean(temp.offerUnit),
        category: {top_category_id: clean(temp.topCategoryId), post_category_id: clean(temp.postCategoryId)},
        sales: {total_sold: Number(temp.saledCount) || null, display_sale_num: Number(productTitle.saleNum) || null, label: clean(productTitle.saleCountDate)},
        offerFlags: root.offerSign || {},
        crossBorder: root.offerCrossBorderModel || {},
        guarantees: services.map(row => clean(row?.serviceName)).filter(Boolean),
        buyerProtection: ctx.shippingServices?.fields?.buyerProtectionModel || [],
        seller: {
          company_name: clean(temp.companyName || shopInfo.companyName),
          auth_company_name: clean(shopInfo.authCompanyName),
          login_id: clean(temp.sellerLoginId || sellerModel.frontSellerLoginId),
          member_id: clean(temp.sellerMemberId || sellerModel.frontSellerMemberId),
          shop_url: clean(temp.winportUrl), seller_type: clean(sellerModel.sellerType),
          service_score: Number(shopInfo.sellerSlrServiceScore) || null,
          buyer_repeat_rate: clean(shopInfo.byrRepeatRate3m)
        }
      };
    } catch (_error) {
      return null;
    }
  })();
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
  const shortVariant = value => {
    const result = clean(value);
    return result && result.length <= 80 ? result : '';
  };
  const titleCandidates = [
    document.querySelector('meta[property="og:title"]')?.content,
    document.querySelector('meta[name="title"]')?.content,
    document.title.replace(/\s*[-_]\s*阿里巴巴.*$/i, ''),
    ...[...document.querySelectorAll('h1')].map(node => node.textContent)
  ].map(clean).filter(value => value && !/有限公司$|供应商|旺铺/.test(value));
  const title = contextData?.title || titleCandidates.sort((a, b) => b.length - a.length)[0] || '';
  const pageText = clean(document.body?.innerText).slice(0, 120000);
  const priceNodes = [...document.querySelectorAll('[class*="price"], [class*="Price"]')]
    .map(node => clean(node.textContent)).filter(text => /[¥￥]\s*\d/.test(text));
  const structuredPrices = (contextData?.pricingTiers || []).map(row => row.price).filter(value => value > 0);
  const price = structuredPrices[0] || number(priceNodes[0]);
  const selectedVariant = [...document.querySelectorAll('[class*="selected"], [aria-checked="true"]')]
    .map(node => clean(node.textContent || node.getAttribute('title'))).filter(Boolean).slice(0, 12);
  const blockedImage = /logo|avatar|icon|qr|qrcode|sprite|emoji|loading|service|wangwang|-2-tps-|imgextra/i;
  const imageCandidates = [...document.images].map((image, order) => {
    const rect = image.getBoundingClientRect();
    const url = imageUrl(image);
    const context = contextOf(image);
    const label = YouyouPackaging.domVariantLabel(image,contextData?.skus||[]) || clean(image.alt || image.title || image.closest('[title]')?.getAttribute('title'));
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
  const allImageCandidates = [...imageCandidates, ...backgroundCandidates].filter(item => item.url && /alicdn|tbcdn|1688/i.test(item.url) &&
    !blockedImage.test(`${item.url} ${item.context} ${item.label}`) &&
    Math.max(item.naturalWidth, item.naturalHeight, item.renderedWidth, item.renderedHeight) >= 24);
  const candidates=allImageCandidates.filter(item=>Math.max(item.naturalWidth,item.naturalHeight,item.renderedWidth,item.renderedHeight)>=120);

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
  let skuImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && item.top < 2600 && skuPattern.test(`${item.context} ${item.nearText}`)
  ).map(item => ({
    url: item.url,
    variant: item.label || item.nearText,
    sku_id: ''
  })), 40);
  const structuredSkuImageSets = (contextData?.skuImageSets || []).map(item => ({
    sku_id: clean(item.sku_id), variant: clean(item.variant).slice(0, 300),
    images: uniqueByUrl((item.images || []).map(url => ({url: canonicalImageUrl(url)})), 8).map(row => row.url)
  })).filter(item => item.images.length);
  // skuImageMap often uses specification text as its key and omits skuId.
  // Attach a set only where its visible specification is part of the SKU row;
  // unmatchable sets remain evidence but are never guessed onto another SKU.
  const visibleSkuSets=allImageCandidates.filter(item=>belongsToOffer(item)&&skuPattern.test(item.context)&&item.label).map(item=>({variant:item.label,images:[item.url]}));
  const mappedSkuImageSets = YouyouPackaging.bindImages(contextData?.skus || [], [...structuredSkuImageSets,...visibleSkuSets]);
  const resolvedSkuImageSets = mappedSkuImageSets.length ? mappedSkuImageSets : structuredSkuImageSets;
  const structuredSkuImages = uniqueByUrl(resolvedSkuImageSets.flatMap(item => item.images.map(url => ({
    url, variant: shortVariant(item.variant), sku_id: item.sku_id
  }))), 100);
  if (structuredSkuImages.length) skuImages = structuredSkuImages;
  const skuUrls = new Set(skuImages.map(item => item.url));
  let detailImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && !skuUrls.has(item.url) &&
    (detailPattern.test(`${item.context} ${item.nearText}`) ||
      (item.top >= 1500 && Math.max(item.naturalWidth, item.naturalHeight, item.renderedWidth, item.renderedHeight) >= 300))
  ).map(item => ({url: item.url})), 40).map(item => item.url);
  if (contextData?.descriptionUrl) {
    try {
      const remote = await chrome.runtime.sendMessage({type: 'YOUYOU_DETAIL_IMAGES', url: contextData.descriptionUrl});
      const structuredDetails = uniqueByUrl((remote?.images || []).map(url => ({url: canonicalImageUrl(url)})), 40).map(item => item.url);
      if (structuredDetails.length) detailImages = structuredDetails;
    } catch (_error) {}
  }
  const detailUrls = new Set(detailImages);
  let productImages = uniqueByUrl(candidates.filter(item =>
    belongsToOffer(item) && !skuUrls.has(item.url) && !detailUrls.has(item.url) &&
    item.top < 1500 && (galleryPattern.test(item.context) || item.naturalWidth >= 300 || item.renderedWidth >= 120)
  ).map(item => ({url: item.url, order: item.order})), 30).map(item => item.url);
  if (!productImages.length) {
    productImages = uniqueByUrl(candidates.filter(item => !detailUrls.has(item.url) && !skuUrls.has(item.url)), 30).map(item => item.url);
  }
  const structuredImages = uniqueByUrl((contextData?.productImages || []).map(url => ({url: canonicalImageUrl(url)})), 30).map(item => item.url);
  if (structuredImages.length) productImages = structuredImages;
  // Some 1688 pages expose their ordinary gallery through skuImageMap as well.
  // A URL already present in the product gallery is not evidence of a SKU image.
  const productUrls = new Set(productImages);
  const skuImageSets = resolvedSkuImageSets;

  const sourceSkus = contextData?.skus || [];
  const tablePackages = YouyouPackaging.domTables(document.querySelectorAll('table'), sourceSkus);
  for (const sku of sourceSkus) {
    const fallback=tablePackages.packages.get(String(sku.sku_id));
    if (fallback) sku.package = Object.fromEntries(Object.entries({...fallback,...(sku.package||{})}).map(([key,value])=>[key,value??fallback[key]??null]));
  }
  const packageRows=[...(contextData?.packageRows||[]).map(row=>({sku_id:clean(row.skuId||row.sku_id),spec_id:clean(row.specId||row.spec_id),package:YouyouPackaging.pack(row),
    table_title:'包装信息 / 商品件重尺',source_method:'STRUCTURED_PRODUCT_PACK_INFO',dimension_type:'PACKAGING',weight_type:'PACKED_WEIGHT'})),...tablePackages.records].slice(0,500);
  const singleOfferPackage=!sourceSkus.length&&contextData?.offerFlags?.isSkuOffer===false?YouyouPackaging.singlePackage(packageRows):null;
  const packageData = sourceSkus.length===1 && sourceSkus[0].package ? {...sourceSkus[0].package} : singleOfferPackage||{weight_g:null,length_cm:null,width_cm:null,height_cm:null,evidence:''};

  const shippingMatch = pageText.match(/(?:运费|快递|邮费)\s*[:：]?\s*[¥￥]\s*(\d+(?:\.\d+)?)/) ||
    pageText.match(/(?:运费|快递|邮费)\s*[:：]?\s*(\d+(?:\.\d+)?)/);
  const freeShipping = !shippingMatch && /(?:运费|快递|邮费)\s*[:：]?\s*(?:包邮|免运费)/.test(pageText);
  const attributes = YouyouPackaging.attributeRows(document.querySelectorAll('table tr, [class*="attribute"] li, [class*="property"] li'));

  return {
    source_url: location.href,
    title,
    selected_variant: [...new Set(selectedVariant)],
    pricing: {amount: price, currency: 'CNY', evidence: price ? 'visible_selected_price' : ''},
    pricing_tiers: contextData?.pricingTiers || [],
    minimum_order_quantity: contextData?.minimumOrder,
    images: productImages,
    product_images: productImages,
    // sku_images is retained for older servers. sku_image_sets preserves the
    // complete image sequence for each source specification.
    sku_images: skuImages,
    // Preserve exact observed labels even if the current binding parser fails.
    // These are public product DOM records, never browser session state.
    sku_image_evidence: allImageCandidates.filter(item=>belongsToOffer(item)&&skuPattern.test(item.context)&&item.label).map(item=>({url:item.url,variant:item.label})).slice(0,200),
    sku_image_sets: skuImageSets.length ? skuImageSets : skuImages.map(item => ({sku_id:item.sku_id,variant:item.variant,images:[item.url]})),
    skus: contextData?.skus || [],
    package_rows:packageRows,
    unassigned_package_rows:tablePackages.unassigned,
    capture_diagnostics:{collector_version:'0.8.4',sku_count:sourceSkus.length,bound_sku_count:new Set(skuImageSets.filter(s=>s.sku_id).map(s=>s.sku_id)).size,
      complete_package_count:sourceSkus.filter(s=>['length_cm','width_cm','height_cm'].every(k=>s.package?.[k]>0)).length,
      packaging_rows_observed:(contextData?.packageRows||[]).length,visible_packaging_rows:tablePackages.records.length,unassigned_package_rows:tablePackages.unassigned.length},
    detail_images: detailImages,
    package: packageData,
    domestic_shipping: {amount: freeShipping ? 0 : number(shippingMatch?.[1]), free: freeShipping, evidence: shippingMatch || freeShipping ? 'visible_shipping_text' : ''},
    attributes,
    seller: contextData?.seller || {},
    unit: contextData?.unit || '',
    category: contextData?.category || {},
    sales: contextData?.sales || {},
    offer_flags: contextData?.offerFlags || {},
    cross_border: contextData?.crossBorder || {},
    guarantees: contextData?.guarantees || [],
    buyer_protection: contextData?.buyerProtection || [],
    extraction_source: contextData ? '1688_structured_page_state' : 'visible_dom_fallback',
    description_url: contextData?.descriptionUrl || ''
  };
};
