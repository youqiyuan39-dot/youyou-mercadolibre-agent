const sites = [
  ["MLB", "巴西"], ["MLM", "墨西哥"], ["MLC", "智利"],
  ["MLA", "阿根廷"], ["MCO", "哥伦比亚"],
];

const attributeNamesZh = {
  BRAND: "品牌",
  MODEL: "型号",
  PACKAGE_HEIGHT: "包装高度",
  PACKAGE_WIDTH: "包装宽度",
  PACKAGE_LENGTH: "包装长度",
  PACKAGE_WEIGHT: "包装重量",
  ITEM_CONDITION: "商品状态",
  SELLER_SKU: "卖家 SKU",
  GTIN: "商品条码",
};
const categoryNamesZh = {
  "Home, Furniture and Garden": "家居、家具与园艺",
  "Gardens & Outdoors": "花园与户外",
  "Gardening and Accesories": "园艺及配件",
  "Garden Tools": "园林工具",
  "Replacement Parts": "替换配件",
  "Carburetors": "化油器",
  "Rebobinadoras para Papel": "纸张复卷机",
  "Tools": "工具",
  "Vehicle Accessories": "车辆配件",
  "Vehicle Tools": "车辆工具",
  "Measuring": "测量工具",
  "Industries and Offices": "工业与办公",
  "Industrial Tools": "工业工具",
  "Motorcycle Replacement Parts": "摩托车替换配件",
  "Heavy Line Replacement Parts": "重型车辆替换配件",
  "Car & Truck Replacement Parts": "汽车与卡车替换配件",
  "Machinery Replacement Parts": "机械替换配件",
  "Engine": "发动机",
  "Engines & Parts": "发动机及配件",
  "Performance": "性能升级",
  "Carburetion": "化油系统",
  "Carburetor Gaskets": "化油器垫片",
  "Carburetors & Parts": "化油器及配件",
  "Gaskets": "垫片",
  "Farming": "农业机械",
  "Nautical Parts": "船舶配件",
};
const categoryNameZh = (name) => categoryNamesZh[text(name).trim()] || text(name);
const categoryPathText = (rows, separator = " > ") => (rows || []).map(row => categoryNameZh(row.name)).join(separator);
const categoryBilingualPath = (rows) => {
  const original = (rows || []).map(row => text(row.name)).join(" > ");
  const chinese = categoryPathText(rows);
  return chinese && chinese !== original ? `${chinese}（${original}）` : original;
};

let selectedName = "";
let currentDraft = null;
let gallery = [];
let sourceGallery = [];
let detailGallery = [];
let defaultGallery = [];
let imagePrompts = [];
let imageJob = null;
let skuImageJob = null;
let collectedSkus = [];
let skuDetails = [];
let skuListingGalleries = [];
let currentSkuId = "";
let imagePollTimer = null;
let currentCategory = null;
let categoryHistory = [];
let sitePricing = {};

const byId = (id) => document.getElementById(id);
const text = (value) => value == null ? "" : String(value);
const numberOrBlank = (value) => value == null ? "" : value;
const pendingNoGtin = "NO_GTIN_PENDING_CATEGORY_CHECK";
const imageKey = (item) => item.local_file || item.remote_url || JSON.stringify(item);
const blockedSourceImage = (item) => /logo|avatar|icon|qrcode|-2-tps-|imgextra/i.test(`${item.remote_url || ""} ${item.role || ""}`);
const displayedImageUrl = (item) => item.local_file
  ? `/assets/${item.local_file.replace(/^assets\//, "")}`
  : `/api/image-proxy?url=${encodeURIComponent(item.remote_url || "")}`;

function openImagePreview(url, label = "图片预览") {
  if (!url) return;
  byId("imagePreviewTitle").textContent = label;
  const image = byId("imagePreviewFull");
  image.src = url;
  image.alt = label;
  byId("imagePreviewDialog").showModal();
}

async function uploadDataUrl(dataUrl, name = "edited.png") {
  return api("/api/upload-image", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name, data_url: dataUrl}),
  });
}

async function editImage(item, operation) {
  const source = displayedImageUrl(item);
  const image = new Image();
  image.src = source;
  await new Promise((resolve, reject) => {
    image.onload = resolve;
    image.onerror = () => reject(new Error("图片读取失败，暂时不能编辑"));
  });
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (operation === "square") {
    const side = Math.min(image.naturalWidth, image.naturalHeight);
    canvas.width = side; canvas.height = side;
    context.drawImage(image, (image.naturalWidth - side) / 2, (image.naturalHeight - side) / 2, side, side, 0, 0, side, side);
  } else {
    canvas.width = image.naturalHeight; canvas.height = image.naturalWidth;
    context.translate(canvas.width / 2, canvas.height / 2);
    context.rotate(Math.PI / 2);
    context.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);
  }
  const result = await uploadDataUrl(canvas.toDataURL("image/png"), `youyou-${operation}.png`);
  return {role: item.role, local_file: result.local_file, source: "local_edit", derived_from: imageKey(item), review_status: "UNREVIEWED"};
}

function downloadImage(item, index) {
  const link = document.createElement("a");
  link.href = displayedImageUrl(item);
  link.download = `悠悠图片-${index + 1}.jpg`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}
const attributeNameZh = (attribute) => attributeNamesZh[attribute.id] || attribute.name || attribute.id;
const isSelfFulfilled = () => currentDraft?.evidence?.market_check?.fulfillment === "自发货";

function updatePricing() {
  const value = (id) => Number(byId(id).value || 0);
  const purchase = value("purchaseCost");
  const domestic = value("domesticShipping");
  const rate = value("exchangeRate");
  const usdCosts = value("packagingCost") + value("otherCost");
  const base = rate > 0 ? (purchase + domestic) / rate : 0;
  const target = value("targetNet");
  byId("estimatedRemainder").value = target > 0 && rate > 0 ? (target - base - usdCosts).toFixed(2) : "资料未齐";
  renderSitePricing();
}

function renderPricingIntelligence(analysis = {}) {
  const box = byId("pricingIntelligence");
  const guidance = analysis.profit_guidance || currentDraft?.pricing_plan?.profit_guidance || {};
  const lines = [];
  const costLabels = {
    purchase_cost_cny: "采购价",
    domestic_shipping_cny: "1688 国内运费",
    exchange_rate_cny_per_usd: "汇率",
    packaging_cost_usd: "包装成本",
    cross_border_freight_usd: "跨境运费",
    other_cost_usd: "其他成本",
  };
  lines.push(`竞品状态：${analysis.status || "尚未查询"}`);
  if (analysis.error) lines.push(`本次未取得：${analysis.error}`);
  for (const site of analysis.sites || []) {
    const band = site.price_band_local;
    lines.push(band
      ? `${site.site_id}｜${site.sample_count} 个样本｜竞品价格 P25 ${band.p25} / 中位数 ${band.median} / P75 ${band.p75} ${band.currency_id}`
      : `${site.site_id}｜没有取得可比新品价格`);
  }
  const shipping = analysis.shipping_guidance || {};
  if (shipping.billable_weight_kg != null) lines.push(`物流计费基础：实重 ${shipping.actual_weight_kg ?? "-"} kg / 体积重 ${shipping.volumetric_weight_kg ?? "-"} kg / 当前计费重 ${shipping.billable_weight_kg} kg`);
  for (const site of shipping.sites || []) lines.push(`${site.site_id} 物流：${site.status === "PLATFORM_INCLUDED_IN_LISTING_PRICE" ? "由美客多计入买家售价" : (site.shipping_cost_usd != null ? `${site.shipping_cost_usd} USD` : "待官方报价")}`);
  if (guidance.recommended_net_proceeds_range_usd) {
    lines.push(`建议卖家净回款区间：${guidance.recommended_net_proceeds_range_usd[0]}–${guidance.recommended_net_proceeds_range_usd[1]} USD`);
    lines.push(`对应单位贡献利润：${guidance.unit_contribution_range_usd[0]}–${guidance.unit_contribution_range_usd[1]} USD，可接受利润率 10%–30%，优先靠近 30%`);
    if (guidance.current_target) lines.push(`当前 ${guidance.current_target.net_proceeds_usd} USD：贡献利润 ${guidance.current_target.unit_contribution_usd} USD / ${guidance.current_target.margin_pct}%（${guidance.current_target.assessment === "NEAR_30_PERCENT_TARGET" ? "接近 30% 目标" : guidance.current_target.assessment === "ACCEPTABLE" ? "处于可接受区间" : guidance.current_target.assessment === "BELOW_ACCEPTABLE" ? "低于 10%" : "高于 30% 偏好线"}）`);
  } else {
    lines.push(`利润区间未生成：仍缺 ${(guidance.missing_inputs || []).map(key => costLabels[key] || key).join("、") || "核心成本"}`);
  }
  for (const assumption of guidance.assumptions || []) lines.push(`估算说明：${assumption}`);
  lines.push(guidance.formula || "单位贡献利润=卖家净回款-全部变动成本；贡献利润率=单位贡献利润/卖家净回款");
  box.innerHTML = "";
  lines.forEach(value => { const row = document.createElement("div"); row.textContent = value; box.appendChild(row); });
}

function renderAiResult(record) {
  const aiBox = byId("aiBox");
  if (!record) { aiBox.className = "empty small"; aiBox.textContent = "尚未生成 AI 提案。"; return; }
  const proposal = record.proposal || record;
  const methodology = proposal.methodology || {
    language: "英文作为 CBT 统一底稿；已选站点另生成西班牙语或巴西葡萄牙语标题覆盖",
    title_basis: ["产品类型在前", "只加入已证实的型号、规格、数量或用途", "删除厂家直发、重复词和夸张词，限制 60 字符"],
    description_basis: ["说明商品与用途", "分点列出已证实或图片可见的部件和规格", "包装信息独立标注", "给出购买前核对项；未证实内容只进入待确认问题"],
  };
  aiBox.className = "evidence";
  aiBox.innerHTML = "";
  const rows = [
    ["生成标题", proposal.title || "未生成"],
    ["生成描述", proposal.description || "未生成"],
    ["语言依据", methodology.language],
    ["标题方法", (methodology.title_basis || []).join("；")],
    ["描述方法", (methodology.description_basis || []).join("；")],
    ["待确认", (proposal.unresolved_questions || []).join("；") || "无"],
  ];
  rows.forEach(([key,value]) => { const row=document.createElement("div"); const strong=document.createElement("strong"); const pre=document.createElement("pre"); strong.textContent=key; pre.textContent=value; row.append(strong,pre); aiBox.appendChild(row); });
}

function updatePhysicalMetrics() {
  const weight = Number(byId("weight").value || 0) / 1000;
  const length = Number(byId("length").value || 0);
  const width = Number(byId("width").value || 0);
  const height = Number(byId("height").value || 0);
  const volumetric = length && width && height ? length * width * height / 6000 : 0;
  const billable = Math.max(weight, volumetric);
  byId("actualWeight").textContent = weight > 0 ? `${weight.toFixed(3)} kg` : "-";
  byId("volumetricWeight").textContent = volumetric > 0 ? `${volumetric.toFixed(3)} kg` : "-";
  byId("billableWeight").textContent = billable > 0 ? `${billable.toFixed(3)} kg` : "-";
}

function applyCurrentSku(skuId) {
  const sku = skuDetails.find(row => String(row.sku_id || "") === String(skuId || "")) || collectedSkus.find(row => String(row.sku_id || "") === String(skuId || ""));
  if (!sku) return;
  currentSkuId = String(sku.sku_id || "");
  if (sku.purchase_cost_cny ?? sku.price) byId("purchaseCost").value = numberOrBlank(sku.purchase_cost_cny ?? sku.price);
  byId("targetNet").value = numberOrBlank(sku.target_net_proceeds_usd);
  if (Array.isArray(sku.site_pricing)) sitePricing = Object.fromEntries(sku.site_pricing.filter(row => row?.site_id).map(row => [row.site_id, {...row}]));
  if (sku.available_quantity ?? sku.inventory) byId("quantity").value = numberOrBlank(Math.floor(Number(sku.available_quantity ?? sku.inventory)));
  if (sku.seller_sku) byId("sellerSku").value = text(sku.seller_sku);
  const pack = sku.package || {};
  if (pack.weight_g != null) byId("weight").value = numberOrBlank(pack.weight_g);
  if (pack.length_cm != null) byId("length").value = numberOrBlank(pack.length_cm);
  if (pack.width_cm != null) byId("width").value = numberOrBlank(pack.width_cm);
  if (pack.height_cm != null) byId("height").value = numberOrBlank(pack.height_cm);
  byId("physicalStatus").value = "SUPPLIER_PACKAGING_INFO";
  updatePricing();
  updatePhysicalMetrics();
}

function syncCurrentSkuSitePricing() {
  const sku = skuDetails.find(row => text(row.sku_id) === currentSkuId);
  const rows = [...byId("sitePricingRows").querySelectorAll("tr[data-site]")].map(tr => ({
    site_id: tr.dataset.site,
    price: tr.querySelector('[data-site-field="price"]').value,
    net_proceeds: tr.querySelector('[data-site-field="net_proceeds"]').value,
    shipping_cost_usd: tr.querySelector('[data-site-field="shipping_cost_usd"]').value,
    logistic_type: tr.querySelector('[data-site-field="logistic_type"]').value,
    listing_type_id: tr.querySelector('[data-site-field="listing_type_id"]').value,
    title: tr.querySelector('[data-site-field="title"]').value,
  }));
  if (sku && rows.length) sku.site_pricing = rows;
}

function skuInput(label, field, value, type = "text") { return `<label>${label}<input data-sku-field="${field}" type="${type}" value="${escAttr(value ?? "")}"></label>`; }
function escAttr(value) { return text(value).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;"); }
function selectedSiteIds() {
  return [...byId("siteChecks")?.querySelectorAll("input:checked") || []].map(node => node.value);
}

function ensureSkuSitePricing(sku) {
  const existing = Object.fromEntries((sku.site_pricing || []).filter(row => row?.site_id).map(row => [text(row.site_id), {...row}]));
  sku.site_pricing = selectedSiteIds().map(siteId => {
    const row = existing[siteId] || {site_id: siteId};
    if (!row.logistic_type) row.logistic_type = "remote";
    if (!row.listing_type_id) row.listing_type_id = "gold_special";
    if (!row.title) row.title = byId("title")?.value || "";
    return row;
  });
  return sku.site_pricing;
}

function renderSkuPricingMatrix() {
  const root = byId("skuPricingMatrix");
  if (!root) return;
  const sites = selectedSiteIds();
  if (!skuDetails.length || !sites.length) {
    root.innerHTML = `<div class="empty small">${skuDetails.length ? "请先选择至少一个目标站点。" : "当前商品没有多 SKU 明细。"}</div>`;
    return;
  }
  const table = document.createElement("table");
  table.className = "sku-pricing-matrix";
  table.innerHTML = `<thead><tr><th>SKU / 规格</th>${sites.map(site => `<th>${escAttr(site)}</th>`).join("")}</tr></thead><tbody></tbody>`;
  const body = table.querySelector("tbody");
  skuDetails.forEach((sku, index) => {
    const rows = ensureSkuSitePricing(sku);
    const tr = document.createElement("tr");
    const name = sku.variant || `SKU ${index + 1}`;
    tr.innerHTML = `<td class="matrix-sku"><strong>${escAttr(name)}</strong><small>${escAttr(sku.seller_sku || sku.sku_id || "")}</small></td>${sites.map(siteId => {
      const row = rows.find(item => text(item.site_id) === siteId) || {};
      const remote = text(row.logistic_type || "remote").toLowerCase() === "remote";
      return `<td class="matrix-site" data-matrix-site="${escAttr(siteId)}">
        <label>${remote ? "净回款 (USD)" : "刊登价 (USD)"}
          <input type="number" min="0" step="0.01" data-matrix-amount="${remote ? "net_proceeds" : "price"}" value="${escAttr(remote ? (row.net_proceeds ?? "") : (row.price ?? ""))}">
        </label>
        <small>${remote ? "买家售价由平台计算" : "直接刊登售价"}</small>
        <select data-matrix-listing-type><option value="gold_special">经典（gold_special）</option><option value="gold_pro">高级（gold_pro）</option><option value="free">免费（free）</option></select>
      </td>`;
    }).join("")}`;
    sites.forEach(siteId => {
      const cell = tr.querySelector(`[data-matrix-site="${siteId}"]`);
      const row = rows.find(item => text(item.site_id) === siteId);
      const amount = cell.querySelector("[data-matrix-amount]");
      const listingType = cell.querySelector("[data-matrix-listing-type]");
      listingType.value = row.listing_type_id || "gold_special";
      const updateRow = () => {
        row[amount.dataset.matrixAmount] = amount.value;
        if (amount.value) row[amount.dataset.matrixAmount === "net_proceeds" ? "price" : "net_proceeds"] = "";
        row.listing_type_id = listingType.value;
        if (text(sku.sku_id) === currentSkuId) {
          sitePricing[siteId] = {...row};
          const detailRow = byId("sitePricingRows")?.querySelector(`tr[data-site="${siteId}"]`);
          if (detailRow) {
            detailRow.querySelector('[data-site-field="price"]').value = row.price ?? "";
            detailRow.querySelector('[data-site-field="net_proceeds"]').value = row.net_proceeds ?? "";
            detailRow.querySelector('[data-site-field="listing_type_id"]').value = row.listing_type_id;
            detailRow.querySelector("[data-site-amount-label]").textContent = row.net_proceeds
              ? `${row.net_proceeds} USD 净回款（矩阵）`
              : row.price ? `${row.price} USD 刊登价（矩阵）` : "尚未在矩阵填写";
          }
        }
      };
      amount.addEventListener("input", updateRow);
      listingType.addEventListener("change", updateRow);
    });
    body.appendChild(tr);
  });
  root.innerHTML = "";
  root.appendChild(table);
}

function renderSkuCards() {
  const root = byId("skuCards");
  root.innerHTML = "";
  skuDetails.forEach((sku, index) => {
    const pack = sku.package || {};
    const card = document.createElement("article");
    card.className = "sku-card-detail";
    card.dataset.skuId = text(sku.sku_id);
    card.innerHTML = `<div class="sku-card-head"><label><input type="radio" name="pricingSku" ${text(sku.sku_id)===currentSkuId?"checked":""}> 当前核价 SKU</label><strong></strong><small></small><span class="sku-source-images"></span></div><div class="sku-card-grid">${skuInput("卖家 SKU", "seller_sku", sku.seller_sku)}${skuInput("库存", "available_quantity", sku.available_quantity, "number")}${skuInput("采购价 (CNY)", "purchase_cost_cny", sku.purchase_cost_cny, "number")}${skuInput("包裹重量 (g)", "weight_g", pack.weight_g, "number")}${skuInput("长 (cm)", "length_cm", pack.length_cm, "number")}${skuInput("宽 (cm)", "width_cm", pack.width_cm, "number")}${skuInput("高 (cm)", "height_cm", pack.height_cm, "number")}${skuInput("条码", "gtin", sku.gtin)}</div><div class="sku-card-foot"><span>组合属性：${escAttr((sku.variation_attributes||[]).map(x=>`${x.name}=${x.value}`).join("；") || sku.variant || "未取得")}</span><span>${text(sku.barcode_type||"NO_GTIN")}</span></div><div class="sku-card-images" data-sku-images></div>`;
    card.querySelector("strong").textContent = sku.variant || `SKU ${index + 1}`;
    card.querySelector("small").textContent = `货源 SKU：${sku.sku_id}`;
    card.querySelector(".sku-source-images").textContent = `专属图 ${sku.source_image_count || 0} 张`;
    card.querySelector('input[type="radio"]').addEventListener("change", () => { syncCurrentSkuSitePricing(); currentSkuId = text(sku.sku_id); byId("currentSku").value = currentSkuId; applyCurrentSku(currentSkuId); renderSkuCards(); });
    card.querySelectorAll("[data-sku-field]").forEach(input => input.addEventListener("input", () => {
      const key = input.dataset.skuField;
      if (["weight_g", "length_cm", "width_cm", "height_cm"].includes(key)) sku.package = {...(sku.package||{}), [key]: input.value}; else sku[key] = input.value;
      if (text(sku.sku_id) === currentSkuId) applyCurrentSku(currentSkuId);
    }));
    root.appendChild(card);
  });
  renderSkuPricingMatrix();
  renderSkuListingEditor();
}

function renderSitePricing() {
  const root = byId("sitePricingRows");
  if (!root) return;
  [...root.querySelectorAll("tr[data-site]")].forEach(tr => {
    const siteId = tr.dataset.site;
    sitePricing[siteId] = {
      site_id: siteId,
      price: tr.querySelector('[data-site-field="price"]').value,
      net_proceeds: tr.querySelector('[data-site-field="net_proceeds"]').value,
      shipping_cost_usd: tr.querySelector('[data-site-field="shipping_cost_usd"]').value,
      logistic_type: tr.querySelector('[data-site-field="logistic_type"]').value,
      listing_type_id: tr.querySelector('[data-site-field="listing_type_id"]').value,
      title: tr.querySelector('[data-site-field="title"]').value,
    };
  });
  const selected = selectedSiteIds();
  root.innerHTML = "";
  selected.forEach(id => {
    const saved = sitePricing[id] || {site_id: id};
    if (!saved.net_proceeds && byId("targetNet")?.value) saved.net_proceeds = byId("targetNet").value;
    if (!saved.logistic_type) saved.logistic_type = "remote";
    if (!saved.listing_type_id) saved.listing_type_id = "gold_special";
    if (!saved.title) saved.title = byId("title")?.value || "";
    sitePricing[id] = saved;
    const tr = document.createElement("tr");
    tr.dataset.site = id;
    tr.innerHTML = `<td></td><td><span data-site-amount-label></span><input type="hidden" data-site-field="price"><input type="hidden" data-site-field="net_proceeds"><input type="hidden" data-site-field="listing_type_id"></td><td><input type="number" min="0" step="0.01" data-site-field="shipping_cost_usd"></td><td><input data-site-field="logistic_type"></td><td><input data-site-field="title" maxlength="300"></td>`;
    tr.children[0].textContent = id;
    for (const key of ["price", "net_proceeds", "shipping_cost_usd", "logistic_type", "listing_type_id", "title"]) {
      tr.querySelector(`[data-site-field="${key}"]`).value = saved[key] ?? "";
    }
    tr.querySelector("[data-site-amount-label]").textContent = saved.net_proceeds
      ? `${saved.net_proceeds} USD 净回款（矩阵）`
      : saved.price ? `${saved.price} USD 刊登价（矩阵）` : "尚未在矩阵填写";
    tr.querySelectorAll("[data-site-field]").forEach(input => input.addEventListener(input.tagName === "SELECT" ? "change" : "input", () => {
      syncCurrentSkuSitePricing();
      renderSkuPricingMatrix();
    }));
    root.appendChild(tr);
  });
  if (!selected.length) root.innerHTML = '<tr><td colspan="5">尚未选择站点</td></tr>';
}

function showMessage(message, kind = "ok") {
  const box = byId("message");
  box.textContent = message;
  box.className = `message ${kind}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || "操作失败");
  return data;
}

function stageLabel(stage) {
  return {
    BLOCKED_EVIDENCE: "缺资料",
    CAPTURE_READY: "采集资料待确认",
    READY_FOR_AI: "可运行完整 AI 流程",
    AI_PROCESSING: "完整 AI 流程处理中",
    AI_PAUSED: "完整 AI 流程已暂停",
    AI_FAILED: "完整 AI 流程失败",
    READY_FOR_HUMAN_REVIEW: "待人工审核",
    READY_TO_PUBLISH: "发布前等待授权",
    READ_ERROR: "草稿读取失败",
  }[stage] || stage || "未知";
}

async function loadList() {
  const data = await api("/api/drafts");
  const list = byId("draftList");
  list.innerHTML = "";
  data.items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "draft-card";
    button.dataset.name = item.name;
    button.dataset.stage = item.stage || "";
    button.innerHTML = `<strong></strong><span></span><small></small>`;
    button.querySelector("strong").textContent = item.title;
    button.querySelector("span").textContent = stageLabel(item.stage);
    button.querySelector("small").textContent = item.errors?.length ? `补齐证据：${item.errors.join("；")}` : (item.next_action || "打开处理");
    button.addEventListener("click", () => loadDraft(item.name));
    list.appendChild(button);
  });
  const requested = new URLSearchParams(location.search).get("draft");
  const initial = requested && data.items.some(item => item.name === requested) ? requested : data.items[0]?.name;
  if (initial && !selectedName) await loadDraft(initial);
}

function attributeRow(attribute = {}, metadata = {}) {
  const tr = document.createElement("article");
  tr.className = "attribute-row";
  tr.dataset.required = metadata.required ? "1" : "0";
  tr.dataset.source = attribute.source || metadata.source || "unresolved";
  tr.innerHTML = `
    <div class="attribute-heading"><strong data-label></strong><span data-required></span></div>
    <small data-id></small>
    <input data-field="id" class="attribute-id" placeholder="例如 BRAND">
    <div class="attribute-values"><input data-field="value_name" placeholder="填写属性值"><input data-field="value_id" placeholder="值 ID（可留空）"></div>
    <div class="attribute-footer"><small data-source></small><button type="button" class="danger">移除</button></div>`;
  ["id", "value_id", "value_name"].forEach((key) => {
    tr.querySelector(`[data-field="${key}"]`).value = text(attribute[key]);
  });
  const refreshLabel = () => {
    const id = tr.querySelector('[data-field="id"]').value.trim().toUpperCase();
    tr.querySelector("[data-label]").textContent = metadata.name ? `${attributeNamesZh[id] || metadata.name}` : (attributeNamesZh[id] || "自定义属性");
    tr.querySelector("[data-id]").textContent = id;
  };
  tr.querySelector("[data-required]").textContent = metadata.required ? "必填" : "可选";
  tr.querySelector("[data-required]").className = metadata.required ? "required-badge" : "optional-badge";
  tr.querySelector("[data-source]").textContent = tr.dataset.source;
  tr.querySelector('[data-field="id"]').addEventListener("input", refreshLabel);
  refreshLabel();
  tr.querySelector("button").addEventListener("click", () => tr.remove());
  return tr;
}

function currentAttributeMap() {
  const result = new Map();
  [...byId("attributeRows").querySelectorAll(".attribute-row")].forEach((tr) => {
    const id = tr.querySelector('[data-field="id"]').value.trim();
    const valueName = tr.querySelector('[data-field="value_name"]').value.trim();
    const valueId = tr.querySelector('[data-field="value_id"]').value.trim();
    if (id) result.set(id, {filled: Boolean(valueName || valueId), tr});
  });
  return result;
}

function renderCategory(category) {
  currentCategory = category;
  const path = categoryBilingualPath(category.path_from_root || []);
  byId("categoryPath").value = path || categoryNameZh(category.name) || "";
  const live = category.query_source === "MERCADOLIBRE_OFFICIAL_API_LIVE";
  byId("categoryInfo").textContent = `${categoryNameZh(category.name) || category.category_id}｜${path || "无路径"}｜${category.is_leaf ? "末级类目" : "非末级类目"}｜${live ? "刚从官方查询" : "使用官方缓存"}`;
  const existing = new Map(editorAttributes().map(row => [row.id, row]));
  const physical = {
    PACKAGE_HEIGHT: byId("height").value ? `${byId("height").value} cm` : "",
    PACKAGE_WIDTH: byId("width").value ? `${byId("width").value} cm` : "",
    PACKAGE_LENGTH: byId("length").value ? `${byId("length").value} cm` : "",
    PACKAGE_WEIGHT: byId("weight").value ? `${Number(byId("weight").value) / 1000} kg` : "",
    HEIGHT: byId("height").value ? `${byId("height").value} cm` : "",
    WIDTH: byId("width").value ? `${byId("width").value} cm` : "",
    LENGTH: byId("length").value ? `${byId("length").value} cm` : "",
    WEIGHT: byId("weight").value ? `${Number(byId("weight").value) / 1000} kg` : "",
    SELLER_PACKAGE_HEIGHT: byId("height").value ? `${byId("height").value} cm` : "",
    SELLER_PACKAGE_WIDTH: byId("width").value ? `${byId("width").value} cm` : "",
    SELLER_PACKAGE_LENGTH: byId("length").value ? `${byId("length").value} cm` : "",
    SELLER_PACKAGE_WEIGHT: byId("weight").value ? `${Number(byId("weight").value) / 1000} kg` : "",
  };
  const requiredIds = new Set((category.required_attributes || []).map(row => row.id));
  const all = category.all_attributes || category.required_attributes || [];
  byId("attributeRows").innerHTML = "";
  all.forEach(attribute => {
    const old = existing.get(attribute.id) || {id: attribute.id, value_id: "", value_name: "", source: "unresolved"};
    if (!old.value_name && !old.value_id && physical[attribute.id]) {
      old.value_name = physical[attribute.id]; old.source = "physical_specification";
    }
    if (attribute.id === "BRAND" && isSelfFulfilled() && !old.value_name && !old.value_id) {
      old.value_name = "Generic"; old.source = "user_default · rule:BRAND";
    }
    byId("attributeRows").appendChild(attributeRow(old, {name: attribute.name, required: requiredIds.has(attribute.id)}));
  });
  const brand = currentAttributeMap().get("BRAND");
  if (isSelfFulfilled() && brand && !brand.filled) {
    brand.tr.querySelector('[data-field="value_name"]').value = "Generic";
  }
  const refreshed = currentAttributeMap();
  byId("requiredSummary").innerHTML = "";
  const optional = all.filter(row => !requiredIds.has(row.id));
  const requiredFilled = (category.required_attributes || []).filter(row => refreshed.get(row.id)?.filled).length;
  const optionalFilled = optional.filter(row => refreshed.get(row.id)?.filled).length;
  const totals = document.createElement("strong");
  totals.textContent = `必填 ${requiredFilled}/${requiredIds.size}｜可选 ${optionalFilled}/${optional.length}`;
  byId("requiredSummary").appendChild(totals);
  (category.required_attributes || []).forEach((attribute) => {
    const badge = document.createElement("span");
    const item = refreshed.get(attribute.id);
    badge.textContent = `${attributeNameZh(attribute)}：${item?.filled ? "已填" : "待填"}`;
    badge.classList.toggle("filled", item?.filled === true);
    byId("requiredSummary").appendChild(badge);
  });
  if (!(category.required_attributes || []).length) {
    byId("requiredSummary").textContent = "官方未返回必填属性。";
  }
  const skuPublishHint = byId("skuPublishHint");
  const skuCount = byId("currentSku")?.options.length || 0;
  if (skuPublishHint && skuCount > 1) {
    const maxVariations = Number(category.settings?.max_variations_allowed || 0);
    skuPublishHint.textContent = maxVariations >= skuCount
      ? `类目支持多 SKU（最多 ${maxVariations} 个），但当前版本尚未构造多 SKU 发布请求；发布会被拦截，避免其余 SKU 丢失。`
      : "当前类目不支持多 SKU 发布；请保留一个当前核价 SKU 后作为单 SKU 商品发布。";
  }
}

async function queryCategory(automatic = false) {
  const categoryId = byId("categoryId").value.trim().toUpperCase();
  if (!categoryId) {
    byId("categoryInfo").textContent = "请先填写类目 ID。";
    return;
  }
  const button = byId("queryCategory");
  button.disabled = true;
  byId("categoryInfo").textContent = automatic ? "正在自动读取官方类目……" : "正在查询美客多官方类目……";
  try {
    const data = await api(`/api/categories/${encodeURIComponent(categoryId)}`);
    renderCategory(data.category);
  } catch (error) {
    byId("categoryInfo").textContent = `本次未取得：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function loadCategoryLevel(parentId = "") {
  const site = byId("categorySite").value;
  const status = byId("categoryTreeStatus");
  const list = byId("categoryTreeList");
  status.textContent = "正在读取美客多官方分类……";
  list.innerHTML = "";
  try {
    // CBT root-category fallback needs a marketplace-readable query.  The
    // supplier title is often Chinese, while the saved listing title is the
    // English catalogue title that Mercado Libre can classify.
    const phrase = byId("title").value || currentDraft?.listing_title || currentDraft?.payload?.title || "";
    const data = await api(`/api/category-tree?site_id=${encodeURIComponent(site)}&parent_id=${encodeURIComponent(parentId)}&q=${encodeURIComponent(phrase)}`);
    const path = data.path_from_root || [];
    categoryHistory = path.slice(0, -1);
    byId("categoryBreadcrumb").textContent = path.length ? categoryPathText(path, " / ") : (data.suggested ? `${site} 官方推荐末级分类` : `${site} 根目录`);
    if (path.length) {
      const back = document.createElement("button");
      back.type = "button"; back.className = "category-back"; back.textContent = "← 返回上一级";
      back.onclick = () => loadCategoryLevel(path.length > 1 ? path[path.length - 2].id : "");
      list.appendChild(back);
    }
    if (data.is_leaf) {
      byId("categoryId").value = parentId;
      byId("categoryPath").value = categoryBilingualPath(path);
      byId("categoryDialog").close();
      await queryCategory(false);
      return;
    }
    (data.children || []).forEach(row => {
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<strong></strong><span>进入下一级 ›</span>`;
      button.querySelector("strong").textContent = categoryNameZh(row.name);
      if (row.selectable) {
        button.querySelector("span").textContent = categoryBilingualPath(row.path_from_root || []) || "选择此分类";
        button.onclick = async () => {
          byId("categoryId").value = row.id;
          byId("categoryPath").value = categoryBilingualPath(row.path_from_root || []);
          byId("categoryDialog").close();
          await queryCategory(false);
        };
      } else {
        button.onclick = () => loadCategoryLevel(row.id);
      }
      list.appendChild(button);
    });
    status.textContent = data.children?.length ? `共 ${data.children.length} 个分类` : "该分类没有下级分类";
  } catch (error) {
    status.textContent = `本次未取得：${error.message}`;
  }
}

function editorAttributes() {
  return [...byId("attributeRows").querySelectorAll(".attribute-row")].map(tr => ({
    id: tr.querySelector('[data-field="id"]').value.trim(),
    value_id: tr.querySelector('[data-field="value_id"]').value.trim(),
    value_name: tr.querySelector('[data-field="value_name"]').value.trim(),
    source: tr.dataset.source || "unresolved",
  })).filter(row => row.id);
}

async function discoverCategoryAndAttributes(useAi = false) {
  if (!selectedName) return;
  if (useAi && !confirm("将调用必填属性补全模型并消耗少量文本额度。AI 只根据采集证据填写，结果仍需人工审核。继续吗？")) return;
  const button = byId(useAi ? "fillCategoryAttributes" : "discoverCategory");
  button.disabled = true;
  byId("categoryInfo").textContent = useAi ? "正在查询官方类目并回填属性……" : "正在读取美客多官方类目和属性……";
  try {
    const endpoint = useAi ? "complete-attributes" : "discover-category";
    const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/${endpoint}`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        confirmed: useAi,
        site_id: byId("categorySite").value,
        title: byId("title").value,
        category_id: byId("categoryId").value,
        attributes: editorAttributes(),
      }),
    });
    byId("categoryId").value = data.selected.category_id;
    renderCategory(data.selected);
    if (useAi) {
      const rows = currentAttributeMap();
      (data.attributes || []).forEach(attribute => {
        const target = rows.get(attribute.id)?.tr;
        if (!target || rows.get(attribute.id)?.filled) return;
        target.querySelector('[data-field="value_id"]').value = text(attribute.value_id);
        target.querySelector('[data-field="value_name"]').value = text(attribute.value_name);
        target.dataset.source = "ai_mapping";
        target.querySelector("[data-source]").textContent = "ai_mapping";
      });
      renderCategory(data.selected);
      showMessage(`AI 属性建议已回填；${(data.unresolved || []).length} 项仍缺明确证据，请人工确认后保存。`, "info");
    } else {
      const alternatives = (data.candidates || []).slice(1, 4).map(row => `${categoryNameZh(row.category_name || row.domain_name || row.category_id)} (${row.category_id})`).join("；");
      if (alternatives) byId("categoryInfo").textContent += `｜其他候选：${alternatives}`;
      showMessage("已读取官方类目和属性，尚未保存。", "info");
    }
  } catch (error) {
    byId("categoryInfo").textContent = `本次未取得：${error.message}`;
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderGallery() {
  const root = byId("gallery");
  root.innerHTML = "";
  byId("imageCount").textContent = `${gallery.length} 张`;
  gallery.forEach((item, index) => {
    const card = document.createElement("article");
    const source = displayedImageUrl(item);
    card.className = "image-card";
    card.draggable = true;
    card.innerHTML = `<div class="image-preview"><img alt="商品图"><b data-sequence></b><em data-origin></em></div><strong></strong><span></span><small class="drag-tip">拖动调整顺序</small><div><button type="button" data-main title="设为首图">☆</button><button type="button" data-up title="左移">←</button><button type="button" data-down title="右移">→</button><button type="button" data-rotate>旋转</button><button type="button" data-square>裁方</button><button type="button" data-download>下载</button><button type="button" data-remove class="danger" title="移出">×</button></div>`;
    card.querySelector("img").src = source || "";
    card.querySelector("[data-sequence]").textContent = index + 1;
    card.querySelector("[data-origin]").textContent = item.source === "ai_generated" ? "AI 生成图" : item.source === "local_upload" || item.source === "local_edit" ? "本地图" : "采集原图";
    card.querySelector("img").addEventListener("error", () => {
      card.querySelector("span").textContent = "图片读取失败，可恢复或重新采集";
      card.querySelector("span").classList.add("image-error");
    });
    card.querySelector("strong").textContent = index === 0 ? "首图" : `第 ${index + 1} 张`;
    card.querySelector("span").textContent = item.role || "source";
    const up = card.querySelector("[data-up]");
    const down = card.querySelector("[data-down]");
    const main = card.querySelector("[data-main]");
    const remove = card.querySelector("[data-remove]");
    main.disabled = index === 0;
    up.disabled = index === 0;
    down.disabled = index === gallery.length - 1;
    main.addEventListener("click", () => { gallery.unshift(gallery.splice(index, 1)[0]); renderGallery(); renderSourceGallery(); });
    up.addEventListener("click", () => { [gallery[index - 1], gallery[index]] = [gallery[index], gallery[index - 1]]; renderGallery(); });
    down.addEventListener("click", () => { [gallery[index], gallery[index + 1]] = [gallery[index + 1], gallery[index]]; renderGallery(); });
    remove.addEventListener("click", () => { gallery.splice(index, 1); renderGallery(); renderSourceGallery(); });
    card.querySelector("[data-download]").addEventListener("click", () => downloadImage(item, index));
    card.querySelector("[data-rotate]").addEventListener("click", async () => {
      try { gallery[index] = await editImage(item, "rotate"); renderGallery(); renderSourceGallery(); }
      catch (error) { showMessage(error.message, "error"); }
    });
    card.querySelector("[data-square]").addEventListener("click", async () => {
      try { gallery[index] = await editImage(item, "square"); renderGallery(); renderSourceGallery(); }
      catch (error) { showMessage(error.message, "error"); }
    });
    card.addEventListener("dragstart", event => {
      event.dataTransfer.setData("text/plain", String(index));
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", event => event.preventDefault());
    card.addEventListener("drop", event => {
      event.preventDefault();
      const from = Number(event.dataTransfer.getData("text/plain"));
      if (!Number.isInteger(from) || from === index || from < 0 || from >= gallery.length) return;
      const [moved] = gallery.splice(from, 1);
      gallery.splice(index, 0, moved);
      renderGallery(); renderSourceGallery();
    });
    root.appendChild(card);
  });
  if (!gallery.length) root.innerHTML = '<div class="empty small">暂无图片</div>';
}

function initialSkuListingGalleries() {
  const review = currentDraft?.evidence?.image_review || {};
  if ((review.sku_listing_galleries || []).length) return review.sku_listing_galleries.map(row => ({...row, gallery:(row.gallery||[]).map(image=>({...image}))}));
  const groups = review.sku_source_galleries || [];
  return skuDetails.map((sku, index) => {
    const source = groups.find(group => text(group.sku_id) === text(sku.sku_id))?.gallery || [];
    const seen = new Set();
    const images = [...source, ...gallery].filter(image => { const key=imageKey(image); if (!key || seen.has(key)) return false; seen.add(key); return true; }).slice(0, 8);
    return {sku_id:text(sku.sku_id), variant:text(sku.variant || `规格 ${index+1}`), gallery:images};
  });
}

function renderSkuListingEditor() {
  if (skuListingGalleries.length < 2) return;
  skuListingGalleries.forEach((sku, skuIndex) => {
    const section = byId("skuCards").querySelector(`[data-sku-id="${CSS.escape(text(sku.sku_id))}"] [data-sku-images]`);
    if (!section) return;
    section.innerHTML = `<div class="sku-card-image-head"><strong></strong><small></small></div><p class="tip">每个 SKU 使用“专属首图 + 专属后续图 + 公共图”。可单独设首图、排序、旋转、裁方或移除；保存商品时一并保存。</p><div class="gallery"></div>`;
    section.querySelector("strong").textContent = sku.variant || sku.sku_id;
    section.querySelector("small").textContent = `SKU ${sku.sku_id} · ${sku.gallery.length} 张`;
    const galleryRoot = section.querySelector(".gallery");
    sku.gallery.forEach((item, imageIndex) => {
      const card = document.createElement("article");
      card.className = "image-card"; card.draggable = true;
      card.innerHTML = `<div class="image-preview"><img alt="SKU 图片"><b></b><em></em></div><strong></strong><small>拖动调整顺序</small><div><button type="button" data-main>☆</button><button type="button" data-up>←</button><button type="button" data-down>→</button><button type="button" data-rotate>旋转</button><button type="button" data-square>裁方</button><button type="button" data-download>下载</button><button type="button" data-remove class="danger">×</button></div>`;
      card.querySelector("img").src = displayedImageUrl(item); card.querySelector("b").textContent = imageIndex + 1;
      card.querySelector("em").textContent = item.source === "sku_ai_generated" ? "SKU AI 图" : item.source === "sku" || item.image_type === "sku" ? "SKU 专属图" : "通用图片";
      card.querySelector("strong").textContent = imageIndex === 0 ? "首图" : `第 ${imageIndex + 1} 张`;
      const rerender = () => renderSkuListingEditor();
      card.querySelector("[data-main]").disabled = imageIndex === 0; card.querySelector("[data-up]").disabled = imageIndex === 0; card.querySelector("[data-down]").disabled = imageIndex === sku.gallery.length - 1;
      card.querySelector("[data-main]").onclick = () => { sku.gallery.unshift(sku.gallery.splice(imageIndex, 1)[0]); rerender(); };
      card.querySelector("[data-up]").onclick = () => { [sku.gallery[imageIndex-1],sku.gallery[imageIndex]]=[sku.gallery[imageIndex],sku.gallery[imageIndex-1]]; rerender(); };
      card.querySelector("[data-down]").onclick = () => { [sku.gallery[imageIndex],sku.gallery[imageIndex+1]]=[sku.gallery[imageIndex+1],sku.gallery[imageIndex]]; rerender(); };
      card.querySelector("[data-remove]").onclick = () => { if(sku.gallery.length>1){sku.gallery.splice(imageIndex,1);rerender();} else showMessage("每个 SKU 至少保留 1 张图片。", "error"); };
      card.querySelector("[data-download]").onclick = () => downloadImage(item, imageIndex);
      card.querySelector("[data-rotate]").onclick = async () => { try { sku.gallery[imageIndex]=await editImage(item,"rotate");rerender(); } catch(error){showMessage(error.message,"error");} };
      card.querySelector("[data-square]").onclick = async () => { try { sku.gallery[imageIndex]=await editImage(item,"square");rerender(); } catch(error){showMessage(error.message,"error");} };
      card.ondragstart = event => event.dataTransfer.setData("text/plain", String(imageIndex)); card.ondragover = event => event.preventDefault(); card.ondrop = event => { event.preventDefault(); const from=Number(event.dataTransfer.getData("text/plain")); if(Number.isInteger(from)&&from!==imageIndex&&from>=0&&from<sku.gallery.length){const [moved]=sku.gallery.splice(from,1);sku.gallery.splice(imageIndex,0,moved);rerender();} };
      galleryRoot.appendChild(card);
    });
  });
}

function renderSourceGallery() {
  const root = byId("sourceGallery");
  root.innerHTML = "";
  sourceGallery.filter(item => !blockedSourceImage(item)).forEach((item, index) => {
    const card = document.createElement("article");
    const source = displayedImageUrl(item);
    const selected = gallery.some((row) => imageKey(row) === imageKey(item));
    card.className = "image-card";
    card.innerHTML = `<img alt="采集原图"><strong></strong><label><input type="checkbox">用于上架</label><div><button type="button" data-left>←</button><button type="button" data-right>→</button><button type="button" data-download>下载</button><button type="button" data-hide class="danger">删除</button></div>`;
    card.querySelector("img").src = source || "";
    const typeLabel = item.image_type === "sku" ? "SKU 图片" : "商品原图";
    card.querySelector("strong").textContent = `${typeLabel} ${index + 1}${item.variant ? `｜${item.variant}` : ""}`;
    const checkbox = card.querySelector("input");
    checkbox.checked = selected;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && !gallery.some((row) => imageKey(row) === imageKey(item))) gallery.push({...item});
      if (!checkbox.checked) gallery = gallery.filter((row) => imageKey(row) !== imageKey(item));
      renderGallery();
    });
    card.querySelector("[data-left]").disabled = index === 0;
    card.querySelector("[data-right]").disabled = index === sourceGallery.length - 1;
    card.querySelector("[data-left]").addEventListener("click", () => {
      [sourceGallery[index - 1], sourceGallery[index]] = [sourceGallery[index], sourceGallery[index - 1]]; renderSourceGallery();
    });
    card.querySelector("[data-right]").addEventListener("click", () => {
      [sourceGallery[index], sourceGallery[index + 1]] = [sourceGallery[index + 1], sourceGallery[index]]; renderSourceGallery();
    });
    card.querySelector("[data-download]").addEventListener("click", () => downloadImage(item, index));
    card.querySelector("[data-hide]").addEventListener("click", () => {
      sourceGallery = sourceGallery.filter(row => imageKey(row) !== imageKey(item));
      gallery = gallery.filter(row => imageKey(row) !== imageKey(item));
      renderSourceGallery(); renderGallery();
    });
    root.appendChild(card);
  });
  if (!sourceGallery.length) root.innerHTML = '<div class="empty small">暂无采集原图</div>';
}

function renderDetailGallery() {
  const root = byId("detailGallery");
  root.innerHTML = "";
  detailGallery.forEach((item, index) => {
    const card = document.createElement("article");
    const source = displayedImageUrl(item);
    card.className = "image-card";
    card.innerHTML = `<img alt="详情图片"><strong></strong><span>仅作证据</span>`;
    card.querySelector("img").src = source || "";
    card.querySelector("strong").textContent = `详情图 ${index + 1}`;
    root.appendChild(card);
  });
  if (!detailGallery.length) root.innerHTML = '<div class="empty small">暂无详情图片</div>';
}

function renderAiImageModule() {
  const promptRoot = byId("imagePromptRows");
  const galleryRoot = byId("aiGeneratedGallery");
  promptRoot.innerHTML = "";
  imagePrompts.forEach((item, index) => {
    const block = document.createElement("label");
    block.className = "ai-prompt-card";
    block.innerHTML = `<strong></strong><small></small><textarea rows="7" data-ai-prompt="${index}"></textarea>`;
    block.querySelector("strong").textContent = `${index + 1}. ${item.label}`;
    block.querySelector("small").textContent = item.language === "none" ? "画面必须无文字" : item.language === "es" ? "画面文字仅西班牙语" : "画面文字仅葡萄牙语";
    block.querySelector("textarea").value = item.prompt || "";
    promptRoot.appendChild(block);
  });
  const items = imageJob?.items || [];
  galleryRoot.innerHTML = "";
  items.forEach((item, index) => {
    const card = document.createElement("article");
    card.className = "image-card";
    const ready = item.status === "done" && item.local_file;
    card.innerHTML = ready
      ? `<img alt="AI 生成图"><strong></strong><span></span><div><button type="button" data-use-ai-image>加入上架区</button><button type="button" data-download-ai-image>下载</button></div>`
      : `<div class="ai-image-placeholder"></div><strong></strong><span></span>`;
    card.querySelector("strong").textContent = item.label || `生成图 ${index + 1}`;
    card.querySelector("span").textContent = item.status === "failed" ? `失败：${item.error || imageJob.error || "未知错误"}` : item.status === "done" ? "生成完成，等待人工选择" : item.status === "generating" ? "生成中…" : "排队中…";
    if (ready) {
      const generated = {role: `ai_${item.kind || index + 1}`, local_file: item.local_file, source: "ai_generated", review_status: "UNREVIEWED"};
      card.querySelector("img").src = displayedImageUrl(generated);
      card.querySelector("[data-use-ai-image]").addEventListener("click", () => {
        if (!gallery.some(row => imageKey(row) === imageKey(generated))) gallery.push(generated);
        renderGallery();
        showMessage("AI 图片已加入上架区；保存新版本后生效。", "info");
      });
      card.querySelector("[data-download-ai-image]").addEventListener("click", () => downloadImage(generated, index));
    }
    galleryRoot.appendChild(card);
  });
  if (!items.length) galleryRoot.innerHTML = '<div class="empty small">尚未生成 AI 图片。先检查提示词和语言，再提交任务。</div>';
  const status = imageJob?.status || "not_started";
  byId("aiImageStatus").textContent = {not_started:"尚未生成",queued:"已排队",generating:"生成中",done:"已完成",failed:"生成失败"}[status] || status;
  byId("generateAiImages").disabled = ["queued", "generating"].includes(status);
}

function renderSkuImageModule() {
  const module = byId("skuImageModule"), root = byId("skuGeneratedGallery");
  module.classList.toggle("hidden", collectedSkus.length < 2);
  if (collectedSkus.length < 2) return;
  const sourceGroups = currentDraft?.evidence?.image_review?.sku_source_galleries || [];
  const sourceCount = sourceGroups.reduce((total, group) => total + (group.gallery || []).length, 0);
  byId("generateSkuImages").textContent = `生成 ${collectedSkus.length} 张 SKU 专属图`;
  const status = skuImageJob?.status || "not_started";
  byId("skuImageStatus").textContent = status === "done" ? "全部生成完成。每个 SKU 的专属图会作为该规格首图，通用图库随后追加。" : status === "failed" ? `生成失败：${skuImageJob.error || "查看 AI 日志"}` : ["queued","generating"].includes(status) ? `处理中：${(skuImageJob.items||[]).filter(x=>x.status==="done").length}/${collectedSkus.length}` : "尚未生成；提交前会再次显示预计消耗张数。";
  byId("generateSkuImages").disabled = ["queued","generating"].includes(status);
  root.innerHTML = sourceGroups.length ? `<div class="tip">已采集 ${sourceGroups.length} 个 SKU 的 ${sourceCount} 张专属原图；每个规格的首图和后续图会一并保留，审核页可逐 SKU 调整。</div>` : "";
  (skuImageJob?.items || []).forEach(item=>{const card=document.createElement("article");card.className="image-card";const ready=item.status==="done"&&item.local_file;card.innerHTML=ready?`<img alt="SKU 专属图"><strong></strong><span></span><div><button type="button" data-download>下载</button></div>`:`<div class="ai-image-placeholder"></div><strong></strong><span></span>`;card.querySelector("strong").textContent=item.label||item.variant||item.sku_id;card.querySelector("span").textContent=item.status==="failed"?`失败：${item.error||skuImageJob.error||"未知错误"}`:item.status==="done"?"SKU 专属首图":"排队中…";if(ready){const generated={role:`sku_ai_${item.sku_id}`,local_file:item.local_file,source:"sku_ai_generated",sku_id:item.sku_id,variant:item.variant,review_status:"UNREVIEWED"};card.querySelector("img").src=displayedImageUrl(generated);card.querySelector("[data-download]").onclick=()=>downloadImage(generated,0)}root.appendChild(card)});
  if (!(skuImageJob?.items || []).length && !sourceGroups.length) root.innerHTML='<div class="empty small">尚未采集到 SKU 专属图。重新用“多 SKU 采集”抓取后会在这里显示。</div>';
}

async function refreshPromptLanguage() {
  if (!selectedName) return;
  const language = byId("imageLanguage").value;
  const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/image-prompts`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({language}),
  });
  imagePrompts = data.prompts || [];
  const resolved = data.resolved_language || language;
  byId("languageGateText").textContent = resolved === "none" ? "主图与营销图均不允许文字" : resolved === "es" ? `${language === "auto" ? "已自动匹配：" : ""}主图无字，营销图只允许西班牙语` : `${language === "auto" ? "已自动匹配：" : ""}主图无字，营销图只允许葡萄牙语`;
  renderAiImageModule();
}

async function loadDraft(name) {
  const data = await api(`/api/drafts/${encodeURIComponent(name)}`);
  selectedName = name;
  currentDraft = data.draft;
  imagePrompts = data.image_prompts || [];
  imageJob = data.image_job || null;
  skuImageJob = data.sku_image_job || null;
  collectedSkus = data.collected_skus || [];
  skuDetails = data.sku_details?.length ? data.sku_details : collectedSkus.map((sku, index) => ({...sku, seller_sku: `${text(currentDraft?.payload?.seller_sku)||"AUTO"}-${index+1}`, available_quantity: sku.inventory ?? 1000, purchase_cost_cny: sku.price, package: sku.package || {}, variation_attributes:[{name:"货源规格",value:sku.variant||""}]}));
  currentSkuId = String((data.draft?.evidence || {}).current_pricing_sku_id || "");
  document.querySelectorAll(".draft-card").forEach((node) => node.classList.toggle("active", node.dataset.name === name));
  byId("emptyState").classList.add("hidden");
  byId("editor").classList.remove("hidden");
  byId("detailNav").classList.remove("hidden");
  byId("saveButton").disabled = false;
  byId("pageTitle").textContent = currentDraft.payload?.family_name || currentDraft.payload?.title || name;

  const payload = currentDraft.payload || {};
  const evidence = currentDraft.evidence || {};
  const latest = data.latest_capture || null;
  const titleLocked = currentDraft.edit_metadata?.field_locks?.title === true;
  const newerTitle = text(latest?.title).trim();
  const currentTitle = text(payload.title).trim();
  byId("title").value = currentTitle;
  byId("familyName").value = text(payload.family_name);
  const latestBox = byId("latestCapture");
  latestBox.classList.toggle("hidden", !latest || newerTitle === currentTitle);
  if (latest && newerTitle !== currentTitle) {
    latestBox.innerHTML = `<strong>发现更新的采集标题：</strong><span></span><button type="button">采用这个标题</button><small></small>`;
    latestBox.querySelector("span").textContent = newerTitle || "未取得";
    latestBox.querySelector("small").textContent = titleLocked ? "标题已锁定，需要手动采用。" : "仅作货源标题参考；不会覆盖 AI 或人工优化标题。";
    latestBox.querySelector("button").addEventListener("click", () => {
      byId("title").value = newerTitle;
      byId("familyName").value = newerTitle;
      byId("pageTitle").textContent = newerTitle;
    });
  }
  byId("description").value = text(payload.description);
  byId("categoryId").value = text(payload.category_id);
  const categoryPrefix = ["CBT", "MLM", "MLB", "MLC", "MLA", "MCO"].find(prefix => text(payload.category_id).startsWith(prefix));
  byId("categorySite").value = categoryPrefix || "CBT";
  byId("quantity").value = numberOrBlank(payload.available_quantity);
  byId("sellerSku").value = text(payload.seller_sku);
  byId("currency").value = text(payload.currency_id || "USD");
  const currentBarcodeEvidence = text(evidence.gtin_or_exemption).trim();
  const useNoGtin = !currentBarcodeEvidence && payload.catalog_listing !== true;
  byId("barcodeType").value = useNoGtin ? "NO_GTIN" : text(payload.barcode_type || "GTIN");
  byId("buyingMode").value = text(payload.buying_mode || "buy_it_now");
  byId("condition").value = text(payload.condition || "new");
  byId("catalogListing").checked = payload.catalog_listing === true;
  byId("warrantyType").value = text(payload.warranty_type || "No warranty");
  byId("warrantyTypeValueId").value = text(payload.warranty_type_value_id || "6150835");
  byId("warrantyTime").value = text(payload.warranty_time);
  byId("gtin").value = useNoGtin ? pendingNoGtin : currentBarcodeEvidence;
  byId("compatibility").value = text(evidence.compatibility_evidence);
  byId("imagesReviewed").checked = evidence.images_human_reviewed === true;

  const pricing = currentDraft.pricing_plan || {};
  byId("purchaseCost").value = numberOrBlank(pricing.purchase_cost_cny ?? evidence.purchase_cost?.amount);
  byId("domesticShipping").value = numberOrBlank(pricing.domestic_shipping_cny ?? latest?.domestic_shipping?.amount);
  byId("exchangeRate").value = numberOrBlank(pricing.exchange_rate_cny_per_usd);
  byId("packagingCost").value = numberOrBlank(pricing.packaging_cost_usd);
  byId("crossBorderFreight").value = numberOrBlank(pricing.cross_border_freight_usd);
  byId("otherCost").value = numberOrBlank(pricing.other_cost_usd);
  byId("targetNet").value = numberOrBlank(pricing.target_net_proceeds_usd);
  updatePricing();
  renderPricingIntelligence(evidence.market_pricing_analysis || {});

  const latestPackage = latest?.package || {};
  const weight = evidence.packed_weight || (latestPackage.weight_g != null ? {value: latestPackage.weight_g, status: "SUPPLIER_PACKAGING_INFO"} : {});
  const dimensions = evidence.packed_dimensions || (latestPackage.length_cm != null ? {length: latestPackage.length_cm, width: latestPackage.width_cm, height: latestPackage.height_cm, status: "SUPPLIER_PACKAGING_INFO"} : {});
  byId("weight").value = numberOrBlank(weight.value);
  byId("length").value = numberOrBlank(dimensions.length);
  byId("width").value = numberOrBlank(dimensions.width);
  byId("height").value = numberOrBlank(dimensions.height);
  byId("physicalStatus").value = weight.status || dimensions.status || "HUMAN_EDITED_UNVERIFIED";
  byId("physicalConfidence").value = evidence.shipping_weight_calculation?.confidence || "UNVERIFIED";
  updatePhysicalMetrics();
  const locks = currentDraft.edit_metadata?.field_locks || {};
  byId("lockWeight").checked = locks.weight === true;
  byId("lockDimensions").checked = locks.dimensions === true;
  byId("lockTitle").checked = locks.title === true;
  byId("lockImages").checked = locks.images === true;

  byId("siteChecks").innerHTML = "";
  sitePricing = Object.fromEntries((payload.sites_to_sell || []).map(raw => {
    const row = typeof raw === "string" ? {site_id: raw} : {...raw};
    return [row.site_id, row];
  }).filter(([id]) => id));
  const savedPricingSku = skuDetails.find(row => text(row.sku_id) === currentSkuId) || skuDetails[0];
  if (savedPricingSku) {
    if (!Array.isArray(savedPricingSku.site_pricing) && Object.keys(sitePricing).length) savedPricingSku.site_pricing = Object.values(sitePricing).map(row => ({...row}));
    if (savedPricingSku.target_net_proceeds_usd == null && pricing.target_net_proceeds_usd != null) savedPricingSku.target_net_proceeds_usd = pricing.target_net_proceeds_usd;
  }
  sites.forEach(([id, label]) => {
    const node = document.createElement("label");
    node.innerHTML = `<input type="checkbox" value="${id}">${label} ${id}`;
    node.querySelector("input").checked = Boolean(sitePricing[id]);
    node.querySelector("input").addEventListener("change", () => {
      syncCurrentSkuSitePricing();
      renderSitePricing();
      skuDetails.forEach(ensureSkuSitePricing);
      renderSkuPricingMatrix();
    });
    byId("siteChecks").appendChild(node);
  });
  renderSitePricing();

  byId("attributeRows").innerHTML = "";
  const attributeSources = currentDraft.edit_metadata?.attribute_sources || {};
  (payload.attributes || []).forEach((row) => byId("attributeRows").appendChild(attributeRow({...row, source: attributeSources[row.id] || "saved_value"})));
  const latestStructured = latest?.extraction_source === "1688_structured_page_state";
  const latestProducts = (latest?.product_images || []).map((url, index) => ({
    role: index === 0 ? "latest_main" : `latest_${index + 1}`, remote_url: url,
    image_type: "product", review_status: "UNREVIEWED", source: "latest_capture"
  }));
  const latestProductUrls = new Set(latestProducts.map(item => item.remote_url));
  const latestSkus = (latest?.sku_images || []).filter(item => !latestProductUrls.has(item.url)).map((item, index) => ({
    role: `latest_sku_${index + 1}`, remote_url: item.url, image_type: "sku",
    variant: text(item.variant).length <= 80 ? item.variant || "" : "", sku_id: item.sku_id || "", review_status: "UNREVIEWED", source: "latest_capture"
  }));
  const latestDetails = (latest?.detail_images || []).map((url, index) => ({
    role: `latest_detail_${index + 1}`, remote_url: url, image_type: "detail",
    review_status: "EVIDENCE_ONLY", source: "latest_capture"
  }));
  sourceGallery = (latestStructured && latestProducts.length ? [...latestProducts, ...latestSkus] : [...(evidence.image_review?.source_gallery || evidence.image_review?.gallery || [])]).filter(item => !blockedSourceImage(item));
  detailGallery = latestStructured ? latestDetails : [...(evidence.image_review?.detail_gallery || [])];
  gallery = latestStructured && !(evidence.image_review?.generated_gallery || []).length && !currentDraft.edit_metadata?.field_locks?.images
    ? latestProducts.slice(0, 3)
    : [...(evidence.image_review?.gallery || [])].filter(item => !blockedSourceImage(item));
  if (!currentDraft.edit_metadata?.field_locks?.images && !(evidence.image_review?.generated_gallery || []).length) gallery = gallery.slice(0, 3);
  defaultGallery = gallery.map((item) => ({...item}));
  skuListingGalleries = initialSkuListingGalleries();
  renderSourceGallery();
  renderDetailGallery();
  renderGallery();
  byId("imageLanguage").value = imageJob?.language || "auto";
  renderAiImageModule();
  renderSkuImageModule();
  if (imagePollTimer) clearTimeout(imagePollTimer);
  if (["queued", "generating"].includes(imageJob?.status) || ["queued", "generating"].includes(skuImageJob?.status)) imagePollTimer = setTimeout(() => loadDraft(selectedName), 4000);

  renderAiResult(data.ai_proposal);

  let errors = [...(data.validation.errors || [])];
  if (useNoGtin) errors = errors.filter(value => !value.includes("真实条码"));
  if (latest?.package?.weight_g != null) errors = errors.filter(value => !value.includes("打包重量"));
  if (["length_cm", "width_cm", "height_cm"].every(key => latest?.package?.[key] != null)) errors = errors.filter(value => !value.includes("打包尺寸"));
  const warnings = data.validation.warnings || [];
  byId("stage").textContent = stageLabel(data.workflow?.stage);
  byId("errorCount").textContent = errors.length;
  byId("warningCount").textContent = warnings.length;
  byId("validationText").textContent = [...errors, ...warnings].join("；") || data.workflow?.next_action || "基础资料已齐。";

  const facts = [
    ["货源链接", evidence.supplier_url],
    ["采购成本", evidence.purchase_cost ? `${evidence.purchase_cost.amount} ${evidence.purchase_cost.currency}` : "未提供"],
    ["货源商品编号", evidence.source_product_id],
    ["原始证据文件", evidence.source_capture || evidence.source_file],
    ["来源页信息", evidence.supplier_page ? JSON.stringify(evidence.supplier_page, null, 2) : "未提供"],
  ];
  const source = latest || evidence.supplier_page || {};
  const summaryValues = [
    ["采集标题", source.title || currentTitle || "未提供"],
    ["采集价", source.pricing?.amount != null ? `${source.pricing.amount} CNY` : `${evidence.purchase_cost?.amount ?? "未提供"}`],
    ["国内运费", source.domestic_shipping?.amount != null ? `${source.domestic_shipping.amount} CNY` : "未取得"],
    ["采集方式", source.extraction_source === "1688_structured_page_state" ? "页面结构化数据" : "页面文字备用方式"],
  ];
  byId("sourceSummary").innerHTML = "";
  summaryValues.forEach(([key, value]) => {
    const div = document.createElement("div");
    div.innerHTML = `<span></span><strong></strong>`;
    div.querySelector("span").textContent = key;
    div.querySelector("strong").textContent = value;
    byId("sourceSummary").appendChild(div);
  });
  const skuRows = source.skus?.length ? source.skus : (evidence.supplier_page?.skus || []);
  const collectionMode = source.collection_mode || evidence.supplier_page?.collection_mode || "single";
  const showSkuSection = collectionMode === "multi" || skuRows.length > 1;
  byId("skuSection").classList.toggle("hidden", !showSkuSection);
  const skuSelect = byId("currentSku");
  skuSelect.innerHTML = "";
  skuRows.forEach((sku, index) => {
    const option = document.createElement("option");
    option.value = String(sku.sku_id || index);
    option.textContent = `${sku.variant || `SKU ${index + 1}`}｜${sku.price ?? "价格未取得"} CNY`;
    skuSelect.appendChild(option);
  });
  if (showSkuSection && skuRows.length) {
    const resolved = skuRows.some(row => String(row.sku_id || "") === currentSkuId) ? currentSkuId : String(skuRows[0].sku_id || 0);
    skuSelect.value = resolved;
    applyCurrentSku(resolved);
  }
  byId("skuCards").innerHTML = "";
  renderSkuCards();
  if (showSkuSection && !byId("skuCards").children.length) {
    byId("skuCards").innerHTML = '<div class="empty small">选择了多 SKU 采集，但本次未取得规格明细。请确认商品确有多个规格后再采集。</div>';
  }
  byId("evidenceBox").innerHTML = "";
  facts.forEach(([key, value]) => {
    const row = document.createElement("div");
    row.innerHTML = "<strong></strong><pre></pre>";
    row.querySelector("strong").textContent = key;
    row.querySelector("pre").textContent = text(value || "未提供");
    byId("evidenceBox").appendChild(row);
  });
  showMessage("已读取当前商品草稿。点击保存后会备份旧稿并保留本页选项。", "info");
  await queryCategory(true);
}

function collectChanges() {
  syncCurrentSkuSitePricing();
  const attributes = [...byId("attributeRows").querySelectorAll(".attribute-row")].map((tr) => ({
    id: tr.querySelector('[data-field="id"]').value,
    value_id: tr.querySelector('[data-field="value_id"]').value,
    value_name: tr.querySelector('[data-field="value_name"]').value,
    source: tr.dataset.source || "unresolved",
  }));
  return {
    payload: {
      title: byId("title").value,
      family_name: byId("familyName").value,
      description: byId("description").value,
      category_id: byId("categoryId").value,
      available_quantity: byId("quantity").value,
      seller_sku: byId("sellerSku").value,
      currency_id: byId("currency").value,
      barcode_type: byId("barcodeType").value,
      buying_mode: byId("buyingMode").value,
      condition: byId("condition").value,
      item_condition_value_id: "2230284",
      catalog_listing: byId("catalogListing").checked,
      warranty_type: byId("warrantyType").value,
      warranty_type_value_id: byId("warrantyTypeValueId").value,
      warranty_time: byId("warrantyTime").value,
      sites_to_sell: [...byId("sitePricingRows").querySelectorAll("tr[data-site]")].map(tr => ({
        site_id: tr.dataset.site,
        price: tr.querySelector('[data-site-field="price"]').value,
        net_proceeds: tr.querySelector('[data-site-field="net_proceeds"]').value,
        shipping_cost_usd: tr.querySelector('[data-site-field="shipping_cost_usd"]').value,
        logistic_type: tr.querySelector('[data-site-field="logistic_type"]').value,
        listing_type_id: tr.querySelector('[data-site-field="listing_type_id"]').value,
        title: tr.querySelector('[data-site-field="title"]').value,
      })),
      attributes,
    },
    physical: {
      weight_g: byId("weight").value,
      length_cm: byId("length").value,
      width_cm: byId("width").value,
      height_cm: byId("height").value,
      status: byId("physicalStatus").value,
      confidence: byId("physicalConfidence").value,
    },
    pricing: {
      purchase_cost_cny: byId("purchaseCost").value,
      domestic_shipping_cny: byId("domesticShipping").value,
      exchange_rate_cny_per_usd: byId("exchangeRate").value,
      packaging_cost_usd: byId("packagingCost").value,
      cross_border_freight_usd: byId("crossBorderFreight").value,
      other_cost_usd: byId("otherCost").value,
      target_net_proceeds_usd: byId("targetNet").value,
    },
    gtin_or_exemption: byId("gtin").value,
    compatibility_evidence: byId("compatibility").value,
    current_pricing_sku_id: currentSkuId,
    sku_details: skuDetails,
    sku_listing_galleries: skuListingGalleries,
    images_human_reviewed: byId("imagesReviewed").checked,
    gallery,
    field_locks: {
      weight: byId("lockWeight").checked,
      dimensions: byId("lockDimensions").checked,
      title: byId("lockTitle").checked,
      images: byId("lockImages").checked,
    },
  };
}

byId("addAttribute").addEventListener("click", () => byId("attributeRows").appendChild(attributeRow()));
byId("currentSku").addEventListener("change", event => { syncCurrentSkuSitePricing(); applyCurrentSku(event.target.value); renderSkuCards(); });
byId("imageLanguage").addEventListener("change", () => refreshPromptLanguage().catch(error => showMessage(error.message, "error")));
byId("generateAiImages").addEventListener("click", async () => {
  if (!selectedName) return;
  const prompts = imagePrompts.map((item, index) => ({...item, prompt: byId("imagePromptRows").querySelector(`[data-ai-prompt="${index}"]`)?.value || item.prompt}));
  byId("generateAiImages").disabled = true;
  try {
    const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/generate-images`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({language: byId("imageLanguage").value, prompts}),
    });
    imageJob = data.job;
    renderAiImageModule();
    showMessage("AI 生图任务已提交；页面会自动刷新状态。", "info");
    imagePollTimer = setTimeout(() => loadDraft(selectedName), 4000);
  } catch (error) {
    showMessage(error.message, "error");
    byId("generateAiImages").disabled = false;
  }
});
byId("generateSkuImages").addEventListener("click", async () => {
  if (!selectedName || !collectedSkus.length) return;
  if (!confirm(`将调用生图 API 生成 ${collectedSkus.length} 张 SKU 专属图，会消耗对应额度。继续吗？`)) return;
  byId("generateSkuImages").disabled = true;
  try {
    const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/generate-sku-images`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmed:true})});
    skuImageJob=data.job;renderSkuImageModule();showMessage(`已提交 ${collectedSkus.length} 张 SKU 专属图。`,"info");imagePollTimer=setTimeout(()=>loadDraft(selectedName),4000);
  } catch(error) { showMessage(error.message,"error");byId("generateSkuImages").disabled=false; }
});
byId("queryCategory").addEventListener("click", () => queryCategory(false));
byId("chooseCategory").addEventListener("click", () => { byId("categoryDialog").showModal(); loadCategoryLevel(""); });
byId("closeCategoryDialog").addEventListener("click", () => byId("categoryDialog").close());
byId("closeImagePreview").addEventListener("click", () => byId("imagePreviewDialog").close());
byId("imagePreviewDialog").addEventListener("click", event => { if (event.target === byId("imagePreviewDialog")) byId("imagePreviewDialog").close(); });
document.addEventListener("click", event => {
  const image = event.target.closest(".image-card img");
  if (image) openImagePreview(image.currentSrc || image.src, image.alt || "图片预览");
});
byId("discoverCategory").addEventListener("click", () => discoverCategoryAndAttributes(false));
byId("fillCategoryAttributes").addEventListener("click", () => discoverCategoryAndAttributes(true));
byId("refreshCompetitors").addEventListener("click", async () => {
  if (!selectedName) return;
  byId("refreshCompetitors").disabled = true;
  try {
    const changes = collectChanges();
    const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/competitor-pricing`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({payload: changes.payload, pricing: changes.pricing}),
    });
    const cnyRate = data.analysis.exchange_rates?.CNY_USD?.rate;
    if (cnyRate && !byId("exchangeRate").value) { byId("exchangeRate").value = (1 / Number(cnyRate)).toFixed(6); updatePricing(); }
    renderPricingIntelligence(data.analysis);
    showMessage(data.analysis.status === "COMPETITOR_DATA_READY" ? "已取得竞品样本并更新利润区间。" : "竞品数据本次未取得，已保留明确原因。", "info");
  } catch (error) { showMessage(error.message, "error"); }
  finally { byId("refreshCompetitors").disabled = false; }
});
byId("restoreImages").addEventListener("click", () => { gallery = defaultGallery.map((item) => ({...item})); renderGallery(); renderSourceGallery(); });
byId("uploadImages").addEventListener("click", () => byId("uploadInput").click());
byId("uploadInput").addEventListener("change", async event => {
  const files = [...event.target.files].slice(0, Math.max(0, 20 - gallery.length));
  for (const file of files) {
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("读取本地图片失败"));
        reader.readAsDataURL(file);
      });
      const result = await uploadDataUrl(dataUrl, file.name);
      gallery.push({role: "local_upload", local_file: result.local_file, source: "local_upload", review_status: "UNREVIEWED"});
    } catch (error) {
      showMessage(error.message, "error");
    }
  }
  event.target.value = "";
  renderGallery();
});
byId("addLinkImage").addEventListener("click", () => {
  const input = byId("linkImageUrl");
  try {
    const url = new URL(input.value.trim());
    if (url.protocol !== "https:") throw new Error();
    const item = {role: "linked", remote_url: url.toString(), source: "linked", review_status: "UNREVIEWED"};
    if (!gallery.some((row) => imageKey(row) === imageKey(item))) gallery.push(item);
    input.value = "";
    renderGallery();
  } catch (_error) {
    showMessage("图片链接必须是完整的 HTTPS 地址。", "error");
  }
});
["purchaseCost", "domesticShipping", "exchangeRate", "packagingCost", "crossBorderFreight", "otherCost"].forEach((id) => byId(id).addEventListener("input", updatePricing));
byId("targetNet").addEventListener("input", () => {
  updatePricing();
});
["weight", "length", "width", "height"].forEach((id) => byId(id).addEventListener("input", updatePhysicalMetrics));
byId("barcodeType").addEventListener("change", () => {
  const noGtin = byId("barcodeType").value === "NO_GTIN";
  if (noGtin) {
    byId("catalogListing").checked = false;
    if (!byId("gtin").value.trim()) byId("gtin").value = pendingNoGtin;
  } else if (byId("gtin").value.trim() === pendingNoGtin) {
    byId("gtin").value = "";
  }
});
byId("saveButton").addEventListener("click", async () => {
  if (!selectedName) return;
  byId("saveButton").disabled = true;
  try {
    const changes = collectChanges();
    const data = await api(`/api/drafts/${encodeURIComponent(selectedName)}/save-version`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(changes),
    });
    if (skuListingGalleries.length > 1) {
      await api(`/api/drafts/${encodeURIComponent(selectedName)}/review-sku-images`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({sku_galleries: skuListingGalleries}),
      });
    }
    const missing = data.validation.errors.length;
    showMessage(`已保存到当前商品，并备份修改前草稿。${missing ? `仍缺 ${missing} 项资料。` : "基础校验通过。"}`);
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    byId("saveButton").disabled = false;
  }
});

loadList().catch((error) => showMessage(error.message, "error"));
