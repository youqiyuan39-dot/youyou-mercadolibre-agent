import json
import sys
from pathlib import Path

try:
    from .barcode_policy import barcode_issues
except ImportError:
    from barcode_policy import barcode_issues

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

    errors.extend(barcode_issues(product))

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
