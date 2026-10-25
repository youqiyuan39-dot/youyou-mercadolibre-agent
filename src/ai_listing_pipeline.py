"""AI-assisted listing preparation with local evidence and approval guards.

The model can propose copy, but it cannot publish, change prices, or mark a
draft as human-approved. API keys are read from the configured environment
variable and are never written to disk or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .validate_listing import validate
except ImportError:  # Direct script execution.
    from validate_listing import validate


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "model_gateway.openai.json"
EXAMPLES_FILE = ROOT / "data" / "learning" / "approved_examples.jsonl"


class AIListingError(RuntimeError):
    pass


COPY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "family_name": {"type": "string", "maxLength": 60},
        "title": {"type": "string", "maxLength": 60},
        "description": {"type": "string", "minLength": 1, "maxLength": 2000},
        "site_titles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "site_id": {"type": "string", "enum": ["MLM", "MLC", "MLB", "MLA", "MCO"]},
                    "title": {"type": "string", "maxLength": 60},
                    "language": {"type": "string", "enum": ["es", "pt-BR"]},
                },
                "required": ["site_id", "title", "language"],
            },
            "maxItems": 5,
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string", "maxLength": 160},
            "maxItems": 8,
        },
        "claims_used": {
            "type": "array",
            "items": {"type": "string", "maxLength": 160},
            "maxItems": 12,
        },
        "unresolved_questions": {
            "type": "array",
            "items": {"type": "string", "maxLength": 160},
            "maxItems": 12,
        },
        "methodology": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title_basis": {"type": "array", "items": {"type": "string", "maxLength": 180}, "maxItems": 6},
                "description_basis": {"type": "array", "items": {"type": "string", "maxLength": 180}, "maxItems": 8},
                "language": {"type": "string", "maxLength": 80},
            },
            "required": ["title_basis", "description_basis", "language"],
        },
    },
    "required": [
        "family_name",
        "title",
        "description",
        "site_titles",
        "key_facts",
        "claims_used",
        "unresolved_questions",
        "methodology",
    ],
}


LISTING_INSTRUCTIONS = """Create a marketplace-ready Mercado Libre listing from the supplied evidence.
The canonical family_name, title and description MUST be natural English because they are the
shared CBT source copy. Mercado Libre may localize that shared copy. For every selected target
site, also return a concise site_titles override: Spanish for MLM/MLC/MLA/MCO and Brazilian
Portuguese for MLB. Return an empty site_titles array when no target site is selected.

Use only facts explicitly present in the JSON or clearly visible in the attached product images.
Never invent brand, compatibility, GTIN, certification, performance, package contents, material,
dimensions, quantity or sales claims. Put uncertainty only in unresolved_questions; never expose
internal evidence language such as "the source says", "the source mentions", "supplier information",
"la información de origen" or "not confirmed" in customer-facing title or description.

Title rules: lead with the exact product type; then add verified model, size, kit contents or use.
Remove supplier boilerplate, factory-direct wording, repetition and superlatives. Use useful search
terms naturally. Keep family_name and every title at 60 characters or fewer.

Description rules: write useful customer copy of at least 250 characters with short paragraphs and
bullets. Follow this order: (1) what the product is and its intended maintenance/use; (2) "Confirmed
product details:" with only verified or visibly confirmed contents, model/size/material facts; (3)
package weight and dimensions under "Package information:" when present; (4) a practical pre-purchase
check for model, dimensions, connectors or included parts. Packaging facts are not product dimensions.
Do not copy 1688 price disclaimers, supplier identity or wholesale language. Do not pad the description
with generic claims. Record the evidence basis and language strategy in methodology.
Omit the entire Package information section when package facts are unknown. Never say that a
fact is absent, unavailable or unspecified in customer copy. Put those gaps in unresolved_questions.
Translate actual words in each site title, not just the language label. Preserve the exact product
identity across languages. An adjustable wrench is not a screwdriver. Before returning JSON,
check each site's actual title language and product type.
最终检查（必须执行）：
1. 主标题和描述用英文；MLM 等西语站点标题必须实际写西班牙语，MLB 写巴西葡语。
2. 缺少包装重量或尺寸时，直接省略整个 Package information 段。顾客描述中不要提“没提供、未确认、没说明”。这些只进 unresolved_questions。
3. 活动扳手的术语是 adjustable wrench / llave ajustable / chave inglesa 或 chave ajustável；chave de fenda 是螺丝刀，不能混用。
4. 不要根据商品类型补造材质、性能、品牌、兼容范围；描述只组织已知事实和购买前的核对建议。
Return only the requested JSON for human review."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_approved_examples(path: Path = EXAMPLES_FILE, limit: int = 5) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("human_approved") is True:
            rows.append(value)
    return rows[-limit:]


def _attribute_map(draft: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in (draft.get("payload") or {}).get("attributes") or []:
        if not isinstance(row, dict):
            continue
        value = row.get("value_name")
        if row.get("id") and value not in (None, ""):
            result[str(row["id"])] = str(value)
    return result


def _source_capture(evidence: dict[str, Any]) -> dict[str, Any]:
    """Read the immutable 1688 capture referenced by the draft, when available."""
    relative = str(evidence.get("source_capture") or "").strip().replace("/", os.sep)
    if not relative:
        return {}
    path = (ROOT / relative).resolve()
    if ROOT.resolve() not in path.parents or not path.is_file():
        return {}
    try:
        value = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _site_ids(payload: dict[str, Any]) -> list[str]:
    allowed = {"MLM", "MLC", "MLB", "MLA", "MCO"}
    result: list[str] = []
    for row in payload.get("sites_to_sell") or []:
        site = row.get("site_id") if isinstance(row, dict) else row
        site = str(site or "").upper()
        if site in allowed and site not in result:
            result.append(site)
    return result


def _input_images(draft: dict[str, Any], limit: int = 4) -> list[str]:
    review = ((draft.get("evidence") or {}).get("image_review") or {})
    urls: list[str] = []
    for pool in (review.get("source_gallery") or [], review.get("detail_gallery") or []):
        for row in pool:
            url = str(row.get("remote_url") or "") if isinstance(row, dict) else ""
            if url.startswith("https://") and url not in urls:
                urls.append(url)
                if len(urls) >= limit:
                    return urls
    return urls


def sanitized_product_context(draft: dict[str, Any]) -> dict[str, Any]:
    payload = draft.get("payload") or {}
    evidence = draft.get("evidence") or {}
    supplier = evidence.get("supplier_page") or {}
    capture = _source_capture(evidence)
    packaging = {
        "packed_weight": evidence.get("packed_weight"),
        "packed_dimensions": evidence.get("packed_dimensions"),
    }
    image_review = evidence.get("image_review") or {}
    return {
        "category_id": payload.get("category_id"),
        "current_title": payload.get("title"),
        "current_family_name": payload.get("family_name"),
        "target_sites": _site_ids(payload),
        "selected_variant": supplier.get("selected_variant"),
        "supplier_title": capture.get("title") or evidence.get("source_title") or supplier.get("title"),
        "supplier_description": capture.get("description") or supplier.get("description"),
        "supplier_attributes": capture.get("attributes") or supplier.get("attributes") or {},
        "supplier_skus": capture.get("skus") or supplier.get("skus") or [],
        "material_listed": supplier.get("material_listed"),
        "product_weight": supplier.get("product_weight"),
        "application_listed": supplier.get("application_listed"),
        "attributes": _attribute_map(draft),
        "packaging": packaging,
        "compatibility_evidence": evidence.get("compatibility_evidence") or None,
        "gtin_or_exemption": evidence.get("gtin_or_exemption") or None,
        "image_count": len(image_review.get("gallery") or []),
        "approved_examples": load_approved_examples(),
    }


def compact_copy_context(context):
    """Keep product evidence/provenance, omit pricing and merchant telemetry from copy writing."""
    import copy
    c = copy.deepcopy(context)
    def sku(row):
        return {k: v for k, v in row.items() if k not in (
            'autopricing_by_site', 'site_pricing', 'target_net_proceeds_usd',
            'purchase_cost_cny', 'purchase_cost_source', 'inventory_source', 'available_quantity', 'supplier_facts', 'user_attribute_policy')}
    c['supplier_skus'] = [sku(r) for r in c.get('supplier_skus') or [] if isinstance(r, dict)]
    if isinstance(c.get('selected_sku'), dict): c['selected_sku'] = sku(c['selected_sku'])
    attrs = c.get('supplier_attributes') or {}
    if isinstance(attrs, dict):
        c['supplier_attributes'] = {k:v for k,v in attrs.items() if not any(t in str(k) for t in (
            '下游平台', '销售地区', '专供货源', '商家代发', '代发数量', '代发商家榜', '揽收率', '留货率', '品质达标', '铺货分销', '加工定制', '产地'))}
        c['supplier_attributes'] = {k:v for k,v in c['supplier_attributes'].items() if str(k) not in ('生产企业',) and not (str(k) in ('品牌','brand') and re.search(r'[\u3400-\u9fff]',str(v)))}
    c['copy_scope'] = 'Product facts only. Omitted pricing and merchant telemetry are not product claims.'
    return c


def material_evidence_present(context):
    if context.get('material_listed'): return True
    def explicit(attrs):
        if isinstance(attrs, dict):
            return any(re.search(r'材质|材料|material', str(k), re.I) and str(v).strip().lower() not in ('', 'none', 'unknown', '未知', '未确认') for k,v in attrs.items())
        if isinstance(attrs, list):
            return any(isinstance(r,dict) and re.search(r'材质|材料|material', str(r.get('name') or r.get('id') or ''),re.I) and r.get('value') for r in attrs)
        return False
    return explicit(context.get('supplier_attributes'))


def build_responses_request(draft: dict[str, Any], model: str) -> dict[str, Any]:
    context = compact_copy_context(sanitized_product_context(draft))
    content: list[dict[str, Any]] = [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}]
    for url in _input_images(draft):
        content.append({"type": "input_image", "image_url": url, "detail": "high"})
    restrictions = []
    if not material_evidence_present(context): restrictions.append('Material has no textual evidence: do not claim plastic, metal, steel, aluminium, or any other material, including in key_facts. Ask about material in unresolved_questions.')
    if not context.get('compatibility_evidence'): restrictions.append('No verified compatibility evidence: omit engine displacement and model compatibility from ALL customer fields, including titles; the supplier title is only a lead.')
    content.append({'type':'input_text','text':
        'Final writing checklist (application instruction, not supplier data): family_name, title, description and key_facts must be English. Only site_titles for MLM are Spanish. '
        'Start description with the literal heading Confirmed product details:, then an English introduction and factual bullet points. '
        'Keep missing facts solely in unresolved_questions. Do not put evidence commentary or missing-information disclaimers in the description. '
        + ' '.join(restrictions)})
    return {
        "model": model,
        "store": False,
        "instructions": LISTING_INSTRUCTIONS + (
            "\nOutput contract checked by the application: description must be nonempty, at most 2000 characters "
            "and contain the exact heading 'Confirmed product details:'. "
            "Start description with the exact heading Confirmed product details:, then write a useful English introduction and evidence-supported details. "
            "Use only verified facts for the explicitly selected SKU; do not invent or repeat claims "
            "to reach a length target. There is no minimum character count. Missing facts belong in unresolved_questions. "
            "Return localized titles for target_sites only, and include methodology."
        ),
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "mercadolibre_listing_copy",
                "strict": True,
                "schema": COPY_SCHEMA,
            }
        },
    }


FORBIDDEN_CUSTOMER_PHRASES = (
    "the source says", "the source mentions", "source information", "supplier information",
    "la información de origen", "el título de origen", "informação de origem", "não foi confirmado",
    "no se confirmó", "not confirmed", "source data", "available evidence",
    "not provided", "not specified", "not available",
    "unavailable", "supplier packaging", "no material specifications",
)


def normalize_copy_sections(proposal):
    """Canonicalize presentation only; never create, repeat or infer product facts."""
    import copy
    result=copy.deepcopy(proposal)
    description=str(result.get('description') or '')
    if len(description.strip())<250:return result
    if 'confirmed product details:' in description.lower():return result
    heading=r'(?im)^\s*(?:\#{1,3}\s*)?(?:\*\*)?(?:confirmed\s+)?(?:product\s+(?:details|specifications)|key\s+(?:facts|details|features)|technical\s+details|specifications)\s*:?\s*(?:\*\*)?\s*$'
    if re.search(heading,description):
        result['description']=re.sub(heading,'Confirmed product details:',description,count=1)
    else:
        bullets=list(re.finditer(r'(?m)^\s*(?:[-*•]\s+|\d+[.)]\s+)\S',description))
        if len(bullets)>=2:
            at=bullets[0].start();result['description']=description[:at].rstrip()+'\n\nConfirmed product details:\n'+description[at:].lstrip()
    return result


def proposal_quality_issues(proposal: dict[str, Any], draft: dict[str, Any]) -> list[str]:
    """Reject shallow or internal-facing copy before it can overwrite a draft."""
    issues: list[str] = []
    title = str(proposal.get("title") or "").strip()
    description = str(proposal.get("description") or "").strip()
    combined = f"{title}\n{description}".lower()
    if not title or len(title) > 60:
        issues.append("英文主标题为空或超过 60 字符")
    if re.search(r"[\u4e00-\u9fff]", title):
        issues.append("英文主标题仍包含中文")
    if not description.replace('Confirmed product details:','').strip():
        issues.append("商品描述缺少可核对的商品事实；不能用空描述或营销套话代替")
    if re.search(r"[\u3400-\u9fff]", description):
        issues.append("英文商品描述仍包含中文，请翻译为英文，不能截断遗漏事实")
    if "confirmed product details:" not in description.lower():
        issues.append("商品描述缺少已确认商品明细")
    if any(phrase in combined for phrase in FORBIDDEN_CUSTOMER_PHRASES):
        issues.append("顾客文案包含内部证据或未确认话术")
    context = sanitized_product_context(draft)
    customer = '\n'.join([title, str(proposal.get('family_name') or ''), description] + [str(x) for x in proposal.get('key_facts') or []] + [str(x.get('title') or '') for x in proposal.get('site_titles') or [] if isinstance(x,dict)])
    from .supplier_facts_v3 import wrong_type
    if wrong_type(context.get('supplier_title'), customer):
        issues.append('节日门帘被误写成门铰链，商品类型错误')
    if re.search(r'\b(?:and|with|for|of|de|con|para)\s*$',title,re.I):issues.append('标题以连接词结尾，必须写成完整的商品名称')
    source_title=str(context.get('supplier_title') or '')
    variant=str(context.get('selected_variant') or '')
    if re.search(r'油泡|油管',source_title) and re.search(r'件套|pc',variant,re.I) and re.search(r'float bowl|(?:fuel|oil) cup|float components|complete assembly',customer,re.I):
        issues.append('选中规格是园林机械维修配件套装；没有浮子杯总成依据，请使用中性的Replacement Parts Kit，不推断套内具体构成')
    if '手拉器' in source_title and re.search(r'ignition process',customer,re.I):issues.append('手拉启动器是机械起动机构，不要误写为点火过程或电气点火部件')
    if re.search(r'grommets?|ribbon loops?',customer,re.I) and not re.search(r'鸡眼|气眼|金属扣|环扣|grommet|ribbon loop',source_title+' '+str(context.get('supplier_attributes') or ''),re.I):issues.append('打孔或丝带不能推断为金属鸡眼或丝带环，删除未提供的结构细节')
    if '导板头' in variant and re.search(r'\b(?:bushing|buffer spring)\b',customer,re.I):issues.append('所选规格明确为导板头，请使用Chainsaw Guide Bar Tip；没有衬套或缓冲弹簧总成的SKU依据')
    if re.search(r'\d+(?:/\d+)?-\d+-\d+-\d+',variant):
        prose=re.sub(r'(?im)^.*packed (?:weight|dimensions).*$','',customer)
        if re.search(r'\b(?:pitch|gauge|length|height|width|diameter)\b',prose,re.I):issues.append('连字符规格串是不可拆解的SKU标识，不证明节距、槽宽或商品长宽高；删除这些解释，只保留中性商品类型及包装明细')
    if re.search(r'according to (?:the )?supplier|supplier (?:listing|attributes|evidence)|confirmed by the user|based on supplier',customer,re.I):issues.append('顾客文案包含供应商证据或人工确认过程，改成直接的商品事实，内部依据留在审计记录')
    if re.search(r'\b(?:attached|fixed|sewn) ribbons?\b',customer,re.I) and not re.search(r'固定丝带|缝制丝带|已安装丝带|attached ribbon',str(context.get('supplier_attributes') or ''),re.I):issues.append('丝带存在不能证明已固定在商品上，删除attached/fixed/sewn结构推断')
    if re.search(r'\boutdoor\b',customer,re.I) and not re.search(r'户外|室外|outdoor',source_title+' '+str(context.get('supplier_attributes') or ''),re.I):issues.append('来源未声明户外适用，删除户外使用断言')
    if '链条' in source_title and re.search(r'(?:bar and chain|chain)\s+(?:measure|measures|is)\s+\d+[- ]?inch',customer,re.I):issues.append('油锯6寸等规格是适配导板的标称尺寸，不能推断链条展开实测长度；保留规格标签和链节数，不写测量结论')
    if '链条' in source_title and re.search(r'bar with \d+ (?:drive )?links',customer,re.I):issues.append('链节数属于链条，不属于导板；写成bar and a chain with the stated link count')
    if '链条' in source_title and re.search(r'(?:guide )?bar (?:link count|has \d+ (?:drive )?links)',customer,re.I):issues.append('导板没有链节数；删除guide bar link count句子，链节数只能描述chain')
    if re.search(r'对联|门帘|横幅',source_title) and re.search(r'\bcouple\b|\bpair of (?:decorative |hanging )?banners',customer,re.I) and not re.search(r'一对|两张|2张|两条|2条|对装',variant+' '+str(context.get('supplier_attributes') or '')):issues.append('对联是商品类型词，不能据此确定此SKU包装内恰有两张；删除pair/couple数量推断，仅描述decorative hanging banners')
    if not material_evidence_present(context) and re.search(r'\b(?:plastic|steel|aluminum|aluminium|polymer|metal|nylon|silicone|rubber|stainless|plástico|acero|aluminio)\b|塑料|钢|铝|尼龙|橡胶', customer, re.I):
        issues.append('顾客文案断言了没有文字依据的材质')
    strokes = set(re.findall(r'(\d+)\s*冲程',str(context.get('supplier_title') or '')))
    stated_strokes = set(re.findall(r'(\d+)\s*[- ]?\s*(?:stroke|tiempos|[Tt]\b)',customer,re.I))
    if strokes and stated_strokes - strokes: issues.append('文案发动机冲程数字与供应商文字不一致')
    if re.search(r'\b(?:chars? max|characters? max|json only|60 characters|site_titles)\b',customer,re.I): issues.append('顾客文案泄漏了输出指令')
    if '手拉器' in str(context.get('supplier_title') or '') and not context.get('compatibility_evidence') and re.search(r'\b(?:scooters?|motorcycles?|ATVs?|off-road vehicles?|motocicletas?)\b',customer,re.I):
        issues.append('启动器车辆适配范围缺少核验依据')
    if '手拉器' in str(context.get('supplier_title') or '') and re.search(r'ignition switch|ignition component|ignition part|starter (?:pull )?cord', customer,re.I):
        issues.append('手拉启动器总成被误写成点火开关或独立拉绳')
    if '手拉器' in str(context.get('supplier_title') or '') and re.search(r'full assembly required for installation|all (?:necessary|installation) parts|ready to install', customer,re.I):
        issues.append('文案断言包含全部安装部件但未提供依据')
    if re.search(r'\b(?:reliable starting|reliable performance|starting power|ease of use|durable|robust|improved performance|easy to use)\b',customer,re.I):
        issues.append('顾客文案包含未提供验证依据的性能或便利性宣传')
    if not context.get('compatibility_evidence') and re.search(r'\b\d+\s*(?:cc|cm3|cm³)\b', customer,re.I):
        issues.append('顾客文案使用了未核验的排量兼容性')
    if re.search(r'\b(?:arrancador|llave|para|tiempos|repuesto|herramienta)\b',title,re.I):
        issues.append('英文主标题包含西班牙语商品词')
    if len(re.findall(r'\b(?:para|este|está|producto|con|del|los|las|por|una|su|como|incluye|se|el|de)\b', title+' '+description,re.I)) >= 8:
        issues.append('英文主标题或描述明显使用了西班牙语')
    if re.search(r'no se han|no se considera|no se proporciona|not independently verified|not verified|based on (?:the )?(?:listing|supplier) title|not explicitly', description,re.I):
        issues.append('顾客描述包含内部证据或缺失信息话术')
    expected_sites = set(_site_ids(draft.get("payload") or {}))
    returned: dict[str, dict[str, Any]] = {}
    for row in proposal.get("site_titles") or []:
        if isinstance(row, dict) and row.get("site_id"):
            returned[str(row["site_id"]).upper()] = row
    missing = expected_sites - set(returned)
    if missing:
        issues.append("缺少站点标题覆盖：" + "、".join(sorted(missing)))
    for site, row in returned.items():
        local_title = str(row.get("title") or "").strip()
        expected_language = "pt-BR" if site == "MLB" else "es"
        if len(local_title) > 60 or not local_title:
            issues.append(f"{site} 站点标题为空或超过 60 字符")
        if re.search(r"[一-鿿]", local_title):
            issues.append(f"{site} 站点标题仍包含中文")
        if local_title.casefold() == title.casefold() and re.search(r"\b(?:wrench|adjustable|blue|carburetor|replacement|inch|inches|kit)\b", title, re.I):
            issues.append(f"{site} 站点标题照搬英文，需实际翻译标题")
        source_context = sanitized_product_context(draft)
        identity = str(source_context.get("supplier_title") or source_context.get("current_title") or "")
        if site == "MLB" and "活动扳手" in identity and "fenda" in local_title.casefold():
            issues.append("MLB 将活动扳手误译成螺丝刀；请用 chave inglesa 或 chave ajustável")
        if row.get("language") != expected_language:
            issues.append(f"{site} 站点标题语言不正确")
    methodology = proposal.get("methodology")
    if not isinstance(methodology, dict):
        issues.append("缺少标题与描述方法记录")
    return issues


def build_repair_request(draft: dict[str, Any], model: str, proposal: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    request = build_responses_request(draft, model)
    content = request["input"][0]["content"]
    content.append({
        "type": "input_text",
        "text": json.dumps({"rejected_proposal": proposal, "quality_issues": issues}, ensure_ascii=False),
    })
    request["instructions"] += "\nThe previous proposal failed the listed quality checks. Rewrite it completely and fix every issue."
    return request


def build_review(draft: dict[str, Any]) -> dict[str, Any]:
    errors, warnings = validate(draft)
    return {
        "status": "BLOCKED" if errors else "READY_FOR_AI_PROPOSAL",
        "publish_allowed": False,
        "human_approved": False,
        "errors": errors,
        "warnings": warnings,
        "image_count": len(((draft.get("evidence") or {}).get("image_review") or {}).get("gallery") or []),
    }


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AIListingError(
            f"找不到 {path.name}。请复制 model_gateway.openai.example.json 后填写模型名。"
        )
    config = read_json(path)
    for key in ("base_url", "api_key_env", "model"):
        if not config.get(key):
            raise AIListingError(f"AI 配置缺少 {key}。")
    return config


def _output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                chunks.append(str(content.get("text") or ""))
    return "".join(chunks)


DEFAULT_RESPONSE_TIMEOUT_SECONDS = 300


def _response_timeout_seconds(config: dict[str, Any]) -> int:
    """Allow slower listing models without leaving a worker stuck indefinitely."""
    try:
        requested = int(config.get("request_timeout_seconds") or DEFAULT_RESPONSE_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        requested = DEFAULT_RESPONSE_TIMEOUT_SECONDS
    return min(900, max(30, requested))


def call_responses_api(config: dict[str, Any], request_body: dict[str, Any]) -> dict[str, Any]:
    # All non-image model calls use the shared local runtime, including old CLI configs.
    try:
        from .local_model_runtime import responses, LocalModelError
    except ImportError:
        from local_model_runtime import responses, LocalModelError
    try:
        return responses(ROOT, request_body, _response_timeout_seconds(config))
    except LocalModelError as exc:
        raise AIListingError(str(exc)) from exc


def generate_quality_checked_proposal(
    config: dict[str, Any], draft: dict[str, Any], model: str
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Generate once, repair once if needed, and never return copy that failed the gate."""
    from .local_model_runtime import profile
    if profile(ROOT).get('copy_mode') == 'scoped_english':
        from .scoped_listing_copy import generate
        return generate(config,draft,model,call_responses_api,proposal_quality_issues,sanitized_product_context,_site_ids)
    response = call_responses_api(config, build_responses_request(draft, model))
    try:
        proposal = normalize_copy_sections(json.loads(_output_text(response)))
    except json.JSONDecodeError as exc:
        raise AIListingError("模型没有返回可解析的结构化 JSON。") from exc
    issues = proposal_quality_issues(proposal, draft)
    retries = 0
    if issues:
        retries = 1
        response = call_responses_api(config, build_repair_request(draft, model, proposal, issues))
        try:
            proposal = normalize_copy_sections(json.loads(_output_text(response)))
        except json.JSONDecodeError as exc:
            raise AIListingError("模型修订后仍未返回可解析的结构化 JSON。") from exc
        issues = proposal_quality_issues(proposal, draft)
    if issues:
        import hashlib
        from datetime import datetime,timezone
        ident=hashlib.sha256(json.dumps(sanitized_product_context(draft),ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()[:16]
        write_json(ROOT/'ai_outputs'/'rejected-copy'/(ident+'.json'),{'created_at':datetime.now(timezone.utc).isoformat(),'quality_issues':issues,'proposal':proposal,'model':response.get('model'),'paid_api_used':response.get('paid_api_used')})
        raise AIListingError("AI 文案质量检查未通过：" + "；".join(issues))
    return proposal, response, retries


def prepare(draft_path: Path, model: str) -> tuple[Path, Path]:
    draft = read_json(draft_path)
    request_path = ROOT / "jobs" / f"{draft_path.stem}-ai-request.json"
    review_path = ROOT / "reviews" / f"{draft_path.stem}-review.json"
    write_json(request_path, build_responses_request(draft, model))
    write_json(review_path, build_review(draft))
    return request_path, review_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="美客多 AI 文案提案流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare", help="只生成请求预览和审核结果，不调用模型")
    prepare_parser.add_argument("draft", type=Path)
    prepare_parser.add_argument("--model", default="gpt-5.6-luna")

    run_parser = sub.add_parser("run", help="调用配置的 Responses API，产生本地提案")
    run_parser.add_argument("draft", type=Path)
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    args = parser.parse_args(argv)
    if args.command == "prepare":
        request_path, review_path = prepare(args.draft, args.model)
        print(json.dumps({"ok": True, "request": str(request_path), "review": str(review_path)}, ensure_ascii=False))
        return 0

    config = load_config(args.config)
    draft = read_json(args.draft)
    proposal, response, quality_retries = generate_quality_checked_proposal(config, draft, str(config["model"]))
    destination = ROOT / "ai_outputs" / f"{args.draft.stem}-proposal.json"
    write_json(
        destination,
        {
            "status": "AI_PROPOSAL_REQUIRES_HUMAN_REVIEW",
            "source_draft": str(args.draft),
            "model": response.get("model", config["model"]),
            "provider": response.get("provider"),
            "paid_api_used": response.get("paid_api_used"),
            "proposal": proposal,
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "quality_retries": quality_retries,
            "human_approved": False,
        },
    )
    print(json.dumps({"ok": True, "saved": str(destination)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (AIListingError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
