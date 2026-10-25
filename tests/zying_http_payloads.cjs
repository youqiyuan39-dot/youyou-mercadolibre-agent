// Shared fixture -> production parser -> HTTP payloads. Never fetch any URL.
const fs = require('node:fs');
const path = require('node:path');
const parser = require('../collector_extension/zying_detail_parser.js');
const minidom = require('./zying_minidom.cjs');
const html = fs.readFileSync(path.join(__dirname, 'fixtures/zying_detail_page.html'), 'utf8');
function parse(markup, sourceUrl='https://meli.zying.net/#/goods/MLM6281697608') {
  const doc = minidom(markup), scope = doc.querySelector('main');
  const visible = el => !el.closest('[hidden]');
  return parser.parse({skuId:'MLM6281697608', visibleIds:['MLM6281697608'],
    sourceUrl, capturedAt:'2026-10-21T06:00:00.000Z',
    entries:parser.readDOM(scope, visible), mainImage:parser.mainImage(scope, doc, visible)});
}
const normal = parse(html);
// Long URLs stay inside the parser's 1000-character cap. All calculator inputs
// and all three currencies are populated in the maximum reasonable fixture.
const imagePrefix = 'https://example.invalid/';
const image = imagePrefix + 'i'.repeat(1000-imagePrefix.length-4) + '.jpg';
const source = 'https://meli.zying.net/' + 'p'.repeat(950) + '#/goods/MLM6281697608';
const maximum = parse(html
  .replace('Escape Do Silencioso TEST_DATA', '长'.repeat(300))
  .replace('https://example.invalid/product-main.jpg', image)
  .replace('data-live="" value="999"', 'data-live="10.25" value="999"')
  .replace('<option selected></option>', '<option selected>工具 &gt; 汽车配件</option>')
  .replace('id="net-profit">-', 'id="net-profit">MXN 61.143 / USD 3.2 / CNY 22')
  .replace('id="margin">-', 'id="margin">15%')
  .replace('<span>净收益价：</span><span>-', '<span>净收益价：</span><span>MXN 300 / USD 16'), source);
// Structural UTF-8 envelope, deliberately not a valid product: every bounded
// string uses a three-byte character. This covers long optional fields too.
const envelope = JSON.parse(JSON.stringify(maximum));
for (const key of Object.keys(envelope.fields)) envelope.fields[key] = '长'.repeat(300);
for (const value of Object.values(envelope.reference)) value.value = '长'.repeat(80);
for (const value of Object.values(envelope.zying_profit_reference.inputs)) value.raw = '长'.repeat(80);
for (const value of Object.values(envelope.zying_profit_reference.results)) value.raw = '长'.repeat(200);
envelope.price_observations = Array.from({length:4}, () => ({value:'长'.repeat(80), label:'长'.repeat(40), field_origin:'zying_calculator_prefill'}));
module.exports = {normal, maximum, envelope};
if (require.main === module) process.stdout.write(JSON.stringify(module.exports));
