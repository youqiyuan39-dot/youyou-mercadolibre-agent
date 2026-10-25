const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function exercise(version, selected = true, allowed = true) {
  let listener;
  let collects = 0;
  let healthCalls = 0;
  const payload = {skus: [{sku_id: 'first'}, {sku_id: 'chosen'}]};
  if (selected) payload.selected_sku_id = 'chosen';
  const context = {
    URL, AbortController, setTimeout, clearTimeout,
    chrome: {tabs: {onUpdated: {addListener() {}}}, runtime: {getManifest:()=>({version:'0.8.7'}),onMessage: {addListener(fn) {listener = fn;}}}},
    fetch: async (url, options) => {
      if (url.endsWith('/api/health')) {
        healthCalls++;
        return {ok: true, json: async () => ({source_intake_version: version})};
      }
      if (url.endsWith('/api/market-reference')) return {ok: true, json: async () => ({})};
      assert.equal(url, 'http://127.0.0.1:8789/api/collect');
      collects++;
      assert.deepEqual(JSON.parse(options.body), {...payload,background_extension_version:'0.8.7'});
      return {ok: true, json: async () => ({ok: true})};
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../collector_extension/background.js'), 'utf8'), context);
  const result = await new Promise(resolve => listener(
    {type: 'YOUYOU_COLLECT', payload},
    {tab: {id: 1, url: allowed ? 'https://detail.1688.com/offer/1075271613391.html' : 'https://example.com/'}},
    resolve,
  ));
  return {result, collects, healthCalls};
}

(async () => {
  const old = await exercise(undefined);
  assert.equal(old.result.ok, false);
  assert.equal(old.collects, 0);
  assert.match(old.result.error, /重开工作台/);
  const current = await exercise('SOURCE_INTAKE_20261018_SINGLE_SKU_V2');
  assert.equal(current.result.ok, true);
  assert.equal(current.collects, 1);
  const legacy = await exercise(undefined, false);
  assert.equal(legacy.result.ok, true);
  assert.equal(legacy.healthCalls, 0);
  const foreign = await exercise('SOURCE_INTAKE_20261018_SINGLE_SKU_V2', true, false);
  assert.equal(foreign.result.ok, false);
  assert.equal(foreign.collects, 0);
  console.log('4 collector scope transport scenarios passed (mock transport; no browser or network calls).');
})().catch(error => {console.error(error); process.exitCode = 1;});
