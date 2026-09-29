const byId = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const siteNames = {CBT:"CBT 跨境",MLM:"墨西哥",MLB:"巴西",MLC:"智利",MLA:"阿根廷",MCO:"哥伦比亚"};
let current = {};

function render(store) {
  current = store;
  const config = store.oauth_config || {};
  byId("alias").value = "";
  byId("authType").value = store.auth_type || "CBT";
  byId("sidebarAlias").textContent = store.alias || "Mercado Libre Store";
  byId("sidebarStatus").textContent = store.status_text || "尚未授权";
  byId("clientId").value = "";
  byId("clientSecret").value = "";
  byId("clientId").placeholder = config.has_client_id ? "已保存，留空不覆盖" : "填写开发者应用 Client ID";
  byId("clientSecret").placeholder = config.has_client_secret ? "已加密保存，留空不覆盖" : "填写 Client Secret";
  byId("redirectUri").value = config.redirect_uri || "";
  const connected = store.status === "CONNECTED";
  const saved = store.token_saved;
  byId("notice").className = `notice ${connected ? "ok" : "warn"}`;
  byId("notice").textContent = connected ? "店铺连接已通过美客多官方 API 实时验证。" : (store.status_text || "尚未授权");
  const missing = config.missing || [];
  byId("configHint").textContent = missing.length
    ? `OAuth 配置缺少：${missing.join("、")}。先在上方填写并加密保存。`
    : "OAuth 配置字段完整；点击授权会在新标签打开美客多官方页面。";
  byId("authorize").disabled = missing.length > 0;
  const stores = (store.stores || []).length ? store.stores : [store];
  byId("storeRows").innerHTML = stores.map(row => {
    const rowConnected = row.status === "CONNECTED";
    const account = row.nickname
      ? `${esc(row.nickname)}<small>用户 ID：${esc(row.merchant_id || "-")}</small>`
      : (row.token_saved ? "等待实时校验" : "尚未连接");
    const active = row.id === store.active_store_id;
    return `<tr><td>${esc(row.alias || "Mercado Libre Store")}${active ? " <small>当前发布店</small>" : ""}</td><td>${account}</td><td>${esc(siteNames[row.auth_type] || row.auth_type || "CBT")}</td><td>${esc(row.connected_at || "-")}</td><td><span class="badge ${rowConnected ? "ok" : "warn"}">${esc(row.status_text || "-")}</span></td><td><button class="reauthorize" data-store-id="${esc(row.id || "legacy")}" data-alias="${esc(row.alias || "")}" data-auth-type="${esc(row.auth_type || "CBT")}" ${missing.length ? "disabled" : ""}>重新授权</button> <button class="activate" data-store-id="${esc(row.id || "legacy")}" ${active || !row.token_saved ? "disabled" : ""}>设为发布店</button></td></tr>`;
  }).join("");
  byId("marketplaces").innerHTML = (store.marketplaces || []).length
    ? store.marketplaces.map(row => `<div class="market"><strong>${esc(siteNames[row.site_id] || row.site_id)}</strong><small>${esc(row.site_id)} · 物流 ${esc(row.logistic_type || "待配置")}</small></div>`).join("")
    : "<div class='hint'>实时验证成功后显示账号已开通的站点与物流类型。</div>";
  document.querySelectorAll(".reauthorize").forEach(button => button.onclick = () => startAuth({
    alias: button.dataset.alias, authType: button.dataset.authType, storeId: button.dataset.storeId,
  }));
  document.querySelectorAll(".activate").forEach(button => button.onclick = () => activateStore(button.dataset.storeId));
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作失败");
  return data;
}

async function load(live = false) {
  byId("refresh").disabled = true;
  try {
    const data = await request(`/api/store-status${live ? "?live=1" : ""}`);
    render(data.store);
  } catch (error) {
    byId("notice").className = "notice warn";
    byId("notice").textContent = error.message;
  } finally {
    byId("refresh").disabled = false;
  }
}

async function saveOauth() {
  byId("saveOauth").disabled = true;
  try {
    await request("/api/store-oauth-config", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({client_id:byId("clientId").value, client_secret:byId("clientSecret").value, redirect_uri:byId("redirectUri").value}),
    });
    byId("notice").className = "notice ok";
    byId("notice").textContent = "OAuth 配置已用 DPAPI 加密保存。现在可以开始授权。";
    await load(false);
  } catch (error) {
    byId("notice").className = "notice warn";
    byId("notice").textContent = error.message;
  } finally {
    byId("saveOauth").disabled = false;
  }
}

async function saveAlias() {
  try {
    const data = await request("/api/store-preferences", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({alias:byId("rowAlias").value, auth_type:current.auth_type || "CBT"}),
    });
    render(data.store);
  } catch (error) { alert(error.message); }
}

async function startAuth(existing = null) {
  const alias = existing?.alias || byId("alias").value;
  const authType = existing?.authType || byId("authType").value;
  try {
    const data = await request("/api/store-authorization-url", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({alias, auth_type:authType, store_id:existing?.storeId || ""}),
    });
    window.open(data.url, "_blank", "noopener");
    byId("notice").className = "notice";
    byId("notice").textContent = "授权页已打开。完成后回到这里点击“刷新连接状态”。";
  } catch (error) { alert(error.message); }
}

async function activateStore(storeId) {
  try {
    const data = await request("/api/store-active", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({store_id:storeId})});
    render(data.store);
  } catch (error) { alert(error.message); }
}

byId("refresh").onclick = () => load(true);
byId("saveOauth").onclick = saveOauth;
byId("authorize").onclick = startAuth;
load(false);
