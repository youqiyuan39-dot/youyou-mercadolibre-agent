"""Build a local-only AI request for packaging evidence extraction.

Preparing a request is free and does not call any model. The request uses
structured output and requires null values when evidence is not visible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

PACKAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "weight_g": {"type": ["number", "null"]},
        "length_cm": {"type": ["number", "null"]},
        "width_cm": {"type": ["number", "null"]},
        "height_cm": {"type": ["number", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["weight_g", "length_cm", "width_cm", "height_cm", "confidence", "evidence", "warnings"],
}

INSTRUCTIONS = """Extract shipping package weight and length, width, and height from the supplied
1688 evidence. Use grams and centimeters. Do not treat bare-product weight or dimensions as packed
shipping data unless the evidence explicitly says they are package data. Return null for any value
that is not visible. Never estimate or invent values. This output requires human review."""


def build_package_request(draft: dict[str, Any], model: str = "gpt-5.6-luna") -> dict[str, Any]:
    evidence = draft.get("evidence") or {}
    review = evidence.get("image_review") or {}
    images: list[str] = []
    for item in (review.get("detail_gallery") or []) + (review.get("source_gallery") or []):
        if not isinstance(item, dict):
            continue
        url = item.get("remote_url")
        if isinstance(url, str) and url.startswith("https://") and url not in images:
            images.append(url)
        if len(images) >= 6:
            break
    supplier = evidence.get("supplier_page") or {}
    visible_facts = {
        "product_weight_g": supplier.get("product_weight_g"),
        "product_length_cm": supplier.get("product_length_cm"),
        "product_width_cm": supplier.get("product_width_cm"),
        "product_height_cm": supplier.get("product_height_cm"),
        "table_evidence": supplier.get("package_table_evidence"),
        "attributes": supplier.get("attributes") or {},
    }
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": json.dumps(visible_facts, ensure_ascii=False)}
    ]
    content.extend({"type": "input_image", "image_url": url, "detail": "low"} for url in images)
    return {
        "model": model,
        "store": False,
        "instructions": INSTRUCTIONS,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "package_evidence",
                "strict": True,
                "schema": PACKAGE_SCHEMA,
            }
        },
    }


def prepare_package_request(draft_path: Path, root: Path = ROOT, model: str = "gpt-5.6-luna") -> Path:
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    destination = root / "jobs" / f"{draft_path.stem}-package-ai-request.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(build_package_request(draft, model), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination
