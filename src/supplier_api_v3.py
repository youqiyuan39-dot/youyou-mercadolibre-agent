"""Credential-free product.get response adapter, not an HTTP client.

Only explicit SKU/attribute bindings are imported. Unknown quote units and
product-level shipping stay evidence, never per-SKU cost or packaging facts.
"""
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from .source_intake import IntakeError, _contains_forbidden_key, normalize_collection

VERSION = '1688_RESPONSE_ADAPTER_20261018_V1'


def identifier(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', str(value or '')):
        raise IntakeError(label + '为空或格式无效')
    return str(value)


def images(value):
    if isinstance(value, str):
        if value.lstrip().startswith('['):
            try:value = json.loads(value)
            except ValueError:value = []
        else:value = [value]
    if not isinstance(value, list):return []
    result = []
    for url in value:
        if not isinstance(url, str):continue
        p = urlparse(url)
        if p.scheme == 'https' and p.hostname and not p.username and not p.password and not p.query and not p.fragment:
            if url not in result:result.append(url)
    return result


def adapt_product_get(response, product_id, *, observed_at, evidence_kind='UNVERIFIED_RESPONSE', quote_basis=None):
    if not isinstance(response, dict) or _contains_forbidden_key([response, quote_basis]):
        raise IntakeError('响应包含禁止字段或不是业务JSON；未保存内容')
    if ('success' in response and response['success'] is not True) or response.get('error_code') or response.get('errMsg'):
        raise IntakeError('业务响应明确报错，不能导入为商品成功结果')
    product = response.get('productInfo')
    if not isinstance(product, dict):
        raise IntakeError('本次未取得已核验结构的productInfo；需核对真实响应封装')
    product_id = identifier(product_id, '货源商品编号')
    if not product_id.isdigit() or str(product.get('productID')) != product_id:
        raise IntakeError('返回商品编号与请求商品不一致')
    try:
        observed = datetime.fromisoformat(str(observed_at).replace('Z','+00:00'))
        if observed.tzinfo is None:raise ValueError('timezone missing')
    except ValueError:
        raise IntakeError('接口观察时间需为带时区的ISO时间')
    raw_skus = product.get('skuInfos')
    if not isinstance(raw_skus, list) or not 1 <= len(raw_skus) <= 500:
        raise IntakeError('SKU列表为空或超出上限，不能猜造无规格单品')
    basis = quote_basis or {}
    if not isinstance(basis,dict):raise IntakeError('采购报价依据结构无效')
    priced = (evidence_kind == 'LIVE_OFFICIAL_API' and basis.get('currency') == 'CNY'
              and basis.get('scope') == 'PER_PROCUREMENT_UNIT' and basis.get('unit')
              and basis.get('source') and basis.get('confirmed') is True)
    skus, groups, ids, specs, quotes = [], [], set(), set(), []
    for raw in raw_skus:
        if not isinstance(raw, dict):raise IntakeError('SKU项格式无效')
        sid = identifier(raw.get('skuId'), '采购SKU编号')
        spec = identifier(raw.get('specId'), '采购specId')
        if sid in ids or spec in specs:raise IntakeError('重复SKU或specId，无法唯一绑定')
        ids.add(sid);specs.add(spec)
        attrs = raw.get('attributes') or []
        if not isinstance(attrs, list):raise IntakeError('SKU属性结构无效')
        variant, urls = [], []
        for attr in attrs:
            if not isinstance(attr, dict):raise IntakeError('SKU属性项格式无效')
            name = attr.get('attributeDisplayName') or attr.get('attributeName') or attr.get('attributeID')
            val = attr.get('customValueName') or attr.get('attributeValue')
            if val is not None:variant.append(str(name or '') + ':' + str(val))
            urls.extend(images(attr.get('skuImageUrl')))
        if not variant:raise IntakeError('SKU没有可核对规格，不能只凭数组顺序绑定')
        skus.append({'sku_id': sid, 'spec_id': spec, 'variant': ' / '.join(variant),
                     'price': raw.get('price') if priced and not raw.get('priceRange') else None,
                     'inventory': raw.get('amountOnSale')})
        if urls:groups.append({'sku_id': sid, 'variant': ' / '.join(variant), 'images': list(dict.fromkeys(urls))})
        quotes.append({'sku_id': sid, 'price': raw.get('price'), 'priceRange': raw.get('priceRange'),
                       'retailPrice': raw.get('retailPrice'), 'consignPrice': raw.get('consignPrice'),
                       'takeSamplePrice': raw.get('takeSamplePrice')})
    attrs = {}
    product_attrs=product.get('attributes') or []
    picture=product.get('image') or {}
    if not isinstance(product_attrs,list) or not isinstance(picture,dict):
        raise IntakeError('商品属性或图片封装发生变化，未静默丢弃字段')
    for attr in product_attrs:
        if isinstance(attr, dict) and attr.get('attributeName') and attr.get('value') is not None:
            key = str(attr['attributeName'])
            if key in attrs and attrs[key] != str(attr['value']):
                raise IntakeError('同名商品属性存在冲突，需核对原始定义')
            attrs[key] = str(attr['value'])
    payload = {'source': '1688', 'source_url': f'https://detail.1688.com/offer/{product_id}.html',
               'title': product.get('subject'), 'description': product.get('description'),
               'attributes': attrs, 'skus': skus, 'sku_image_sets': groups,
               'product_images': images(picture.get('images')),
               'collection_mode': 'multi' if len(skus) > 1 else 'single',
               'extraction_method': 'OFFICIAL_API_PRODUCT_GET'}
    capture = normalize_collection(payload)
    # Retained in the immutable intake, not fed into geometric fields or AI facts.
    capture['api_evidence'] = {'version': VERSION, 'evidence_kind': evidence_kind, 'observed_at': str(observed_at),
                               'response_sha256': hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest(),
                               'shipping_scope': 'PRODUCT_LEVEL_UNITS_AND_SEMANTICS_UNVERIFIED',
                               'shippingInfo': product.get('shippingInfo'), 'saleInfo': product.get('saleInfo'),
                               'sku_quotes': quotes, 'quote_basis': basis if priced else None,
                               'intelligentInfo_ignored': bool(product.get('intelligentInfo'))}
    return {'capture': capture, 'audit': {'state': 'SCHEMA_ADAPTED_NOT_BUSINESS_VERIFIED',
            'sku_count': len(skus), 'bound_image_groups': len(groups), 'priced_sku_count': sum(s['price'] is not None for s in capture['skus']),
            'packaging_imported': False, 'real_network_call_performed': False,
            'missing': ['逐SKU包装及单位语义', '订单数量、起订量与实际采购费用', '市场与采购销售单位对应关系']}}


def import_product_get(root, response, product_id, *, observed_at, quote_basis=None,
                       evidence_kind='UNVERIFIED_RESPONSE', confirmed=False):
    """Explicit local import reuses intake deduplication, locks, and task queue."""
    if confirmed is not True or evidence_kind != 'LIVE_OFFICIAL_API':
        raise IntakeError('仅可在明确确认后导入真实官方业务响应，文档/测试示例不能进入正式商品队列')
    adapted = adapt_product_get(response, product_id, observed_at=observed_at,
                                evidence_kind=evidence_kind, quote_basis=quote_basis)
    from .source_intake import save_collection_and_enqueue
    return {'audit': adapted['audit'], 'intake': save_collection_and_enqueue(adapted['capture'], root)}
