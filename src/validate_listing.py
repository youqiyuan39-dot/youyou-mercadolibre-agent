import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def present(value):
    return value is not None and value != "" and value != []


def validate(product):
    errors = []
    warnings = []

    # v0.2 drafts keep source evidence separate from the Mercado Libre payload.
    # Continue accepting the old flat example format during migration.
    evidence = product.get("evidence") or product

    for field, label in (
        ("supplier_url", "货源链接"),
        ("purchase_cost", "采购成本"),
        ("packed_weight", "打包重量"),
        ("packed_dimensions", "打包尺寸"),
    ):
        if not present(evidence.get(field)):
            errors.append(f"缺少{label}")

    payload_attributes = {
        item.get("id"): item.get("value_name")
        for item in (product.get("payload", {}).get("attributes") or [])
        if isinstance(item, dict)
    }
    gtin = evidence.get("gtin") or payload_attributes.get("GTIN")
    exemption = evidence.get("gtin_exemption_evidence") or evidence.get("gtin_or_exemption")
    barcode_type = str(product.get("payload", {}).get("barcode_type") or "").upper()
    pending_no_gtin = barcode_type == "NO_GTIN" and exemption == "NO_GTIN_PENDING_CATEGORY_CHECK"
    if not present(gtin) and not present(exemption):
        errors.append("缺少真实条码或平台无条码依据")
    elif pending_no_gtin:
        warnings.append("已按无条码商品处理，正式发布前需通过官方类目校验")
        if product.get("human_approved") is True:
            errors.append("无条码原因尚未通过官方类目校验")

    if not present(evidence.get("compatibility_evidence")):
        warnings.append("缺少型号兼容证据，不能让 AI 自行补写")

    if not evidence.get("images_human_reviewed", False):
        warnings.append("图片尚未人工核对")

    if (evidence.get("packed_weight") or {}).get("status") == "USER_INPUT_UNVERIFIED" or \
            (evidence.get("packed_dimensions") or {}).get("status") == "USER_INPUT_UNVERIFIED":
        warnings.append("浮窗手填包装数据尚未实物确认")

    if product.get("requested_action") == "publish" and not product.get("human_approved", False):
        errors.append("发布前必须人工确认")

    return errors, warnings


def main():
    if len(sys.argv) != 2:
        print("用法: python validate_listing.py 商品草稿.json")
        return 2

    path = Path(sys.argv[1])
    product = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = validate(product)

    result = {
        "ok": not errors,
        "status": "READY_FOR_REVIEW" if not errors else "REAL_PROFIT_UNVERIFIED",
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
