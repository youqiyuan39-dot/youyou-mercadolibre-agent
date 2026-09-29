(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();

  function extractStructuredProduct() {
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
      let packInfo = ctx.productPackInfo?.fields?.pieceWeightScale;
      if (typeof packInfo === 'string') packInfo = JSON.parse(packInfo);
      const packRows = packInfo?.pieceWeightScaleInfo || [];
      const weightBySku = new Map(packRows.filter(row => row?.skuId).map(row => [String(row.skuId), row]));
      const canonicalImage = value => clean(value).replace(/^\/\//, 'https://');
      const imageSets = [];
      const walkImages = (value, path = []) => {
        if (typeof value === 'string' && /^(?:https?:)?\/\//i.test(value) && /alicdn|tbcdn|1688/i.test(value)) {
          const variant = clean(path.join(' / ')).slice(0, 300);
          const last = imageSets[imageSets.length - 1];
          if (last && last.variant === variant) {
            if (!last.images.includes(canonicalImage(value))) last.images.push(canonicalImage(value));
          } else {
            imageSets.push({variant, sku_id: '', images: [canonicalImage(value)]});
          }
        } else if (value && typeof value === 'object') {
          Object.entries(value).forEach(([key, child]) => walkImages(child, [...path, clean(key)]));
        }
      };
      walkImages(trade.skuImageMap || {});
      const skus = skuRows.map(row => {
        const parts = typeof row.specAttrs === 'string'
          ? row.specAttrs.replace(/&gt;/g, '>').split('>').map(clean).filter(Boolean)
          : [];
        const pack = weightBySku.get(String(row.skuId || '')) || null;
        return {
          sku_id: clean(row.skuId), spec_id: clean(row.specId), variant: parts.join(' / '),
          price: Number(row.discountPrice || row.price || 0) || null,
          inventory: Number(row.canBookCount ?? row.amountOnSale ?? 0),
          package: pack ? {
            weight_g: Number(pack.weight) || null, length_cm: Number(pack.length) || null,
            width_cm: Number(pack.width) || null, height_cm: Number(pack.height) || null
          } : null
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
        skus, skuImageSets: imageSets, packageRows: packRows, descriptionUrl,
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
    } catch (error) {
      return {error: String(error?.message || error)};
    }
  }

  document.addEventListener('youyou:extract-structured', () => {
    document.documentElement.setAttribute('data-youyou-structured', JSON.stringify(extractStructuredProduct()));
  });
})();
