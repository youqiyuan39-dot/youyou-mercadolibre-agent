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
        "description": {"type": "string", "maxLength": 2000},
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


def build_responses_request(draft: dict[str, Any], model: str) -> dict[str, Any]:
    context = sanitized_product_context(draft)
    content: list[dict[str, Any]] = [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}]
    for url in _input_images(draft):
        content.append({"type": "input_image", "image_url": url, "detail": "high"})
    return {
        "model": model,
        "store": False,
        "instructions": LISTING_INSTRUCTIONS,
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
    "no se confirmó", "not confirmed",
)


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
    if len(description) < 250:
        issues.append("商品描述少于 250 字符，信息过于单薄")
    if "confirmed product details:" not in description.lower():
        issues.append("商品描述缺少已确认商品明细")
    if any(phrase in combined for phrase in FORBIDDEN_CUSTOMER_PHRASES):
        issues.append("顾客文案包含内部证据或未确认话术")
    expected_sites = set(_site_ids(draft))
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
    env_name = str(config["api_key_env"])
    api_key = str(config.get("_api_key") or os.environ.get(env_name) or "").strip()
    if not api_key:
        raise AIListingError(f"环境变量 {env_name} 未设置，本次没有调用模型。")
    url = str(config["base_url"]).rstrip("/") + "/responses"
    request = urllib.request.Request(
        url,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_response_timeout_seconds(config)) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AIListingError(f"AI 接口返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise AIListingError(f"AI 接口读取超时（已等待 {_response_timeout_seconds(config)} 秒）。") from exc
        raise AIListingError("无法连接 AI 接口。") from exc


def generate_quality_checked_proposal(
    config: dict[str, Any], draft: dict[str, Any], model: str
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Generate once, repair once if needed, and never return copy that failed the gate."""
    response = call_responses_api(config, build_responses_request(draft, model))
    try:
        proposal = json.loads(_output_text(response))
    except json.JSONDecodeError as exc:
        raise AIListingError("模型没有返回可解析的结构化 JSON。") from exc
    issues = proposal_quality_issues(proposal, draft)
    retries = 0
    if issues:
        retries = 1
        response = call_responses_api(config, build_repair_request(draft, model, proposal, issues))
        try:
            proposal = json.loads(_output_text(response))
        except json.JSONDecodeError as exc:
            raise AIListingError("模型修订后仍未返回可解析的结构化 JSON。") from exc
        issues = proposal_quality_issues(proposal, draft)
    if issues:
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
            "model": config["model"],
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
