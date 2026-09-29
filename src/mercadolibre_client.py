"""Mercado Libre Global Selling API client.

This module never prints or stores access/refresh tokens. Write methods are
only called by the separately confirmed publishing service.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import mercadolibre_oauth as oauth
except ImportError:  # Direct script execution.
    import mercadolibre_oauth as oauth


API_BASE = "https://api.mercadolibre.com"
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


class MercadoLibreAPIError(RuntimeError):
    pass


def _active_token_path() -> Path:
    """Resolve the selected local store without exposing any token content."""
    registry = ROOT / "config" / "mercadolibre_stores.json"
    try:
        value = json.loads(registry.read_text(encoding="utf-8"))
        store_id = str(value.get("active_store_id") or "legacy")
        if store_id != "legacy" and store_id.isalnum():
            return ROOT / ".secrets" / f"mercadolibre_tokens_{store_id}.dat"
    except (OSError, ValueError, AttributeError):
        pass
    return oauth.TOKEN_FILE


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class MercadoLibreClient:
    def __init__(self, access_token: str | None = None) -> None:
        if access_token is None:
            access_token = oauth.load_private_json(_active_token_path()).get("access_token")
        if not access_token:
            raise MercadoLibreAPIError("本机没有可用的 Access Token，请重新授权。")
        self._access_token = access_token

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(payload.get("message") or payload.get("error") or "").strip()
            except Exception:
                pass
            if exc.code == 401:
                raise MercadoLibreAPIError("Access Token 已失效，需要刷新或重新授权。") from exc
            suffix = f"：{detail}" if detail else ""
            raise MercadoLibreAPIError(f"美客多 API 返回 HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise MercadoLibreAPIError("无法连接美客多 API。") from exc

    def _write_request(self, path: str, body: bytes, content_type: str, method: str = "POST") -> Any:
        request = urllib.request.Request(
            f"{API_BASE}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": content_type,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(payload.get("message") or payload.get("error") or "").strip()
                causes = payload.get("cause") or []
                if causes and isinstance(causes, list):
                    cause_text = "; ".join(str(row.get("message") or row.get("code") or "") for row in causes[:3] if isinstance(row, dict))
                    detail = "; ".join(value for value in (detail, cause_text) if value)
            except Exception:
                pass
            if exc.code == 401:
                raise MercadoLibreAPIError("Access Token 已失效，需要刷新或重新授权。") from exc
            suffix = f"：{detail}" if detail else ""
            raise MercadoLibreAPIError(f"美客多发布接口返回 HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise MercadoLibreAPIError("无法连接美客多发布接口。") from exc

    def post_json(self, path: str, payload: Any) -> Any:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._write_request(path, body, "application/json; charset=utf-8")

    def put_json(self, path: str, payload: Any) -> Any:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._write_request(path, body, "application/json; charset=utf-8", method="PUT")

    def upload_picture(self, data: bytes, filename: str, content_type: str) -> dict[str, Any]:
        if not data or len(data) > 10_000_000:
            raise MercadoLibreAPIError("图片为空或超过美客多 10 MB 限制。")
        safe_name = Path(filename).name.replace('"', "") or "image.jpg"
        boundary = "----CodexMercadoLibrePictureBoundary"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type or 'image/jpeg'}\r\n\r\n"
        ).encode("utf-8")
        body = prefix + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._write_request("/pictures/items/upload", body, f"multipart/form-data; boundary={boundary}")
        if not isinstance(result, dict):
            raise MercadoLibreAPIError("图片上传响应格式不正确。")
        return result

    def me(self) -> dict[str, Any]:
        return self.get("/users/me")

    def marketplace_mapping(self, merchant_id: int | str) -> dict[str, Any]:
        return self.get(f"/marketplace/users/{merchant_id}")

    def listing_capacity(self, merchant_id: int | str) -> Any:
        return self.get("/marketplace/users/cap", {"user_id": merchant_id})

    def category(self, category_id: str) -> dict[str, Any]:
        return self.get(f"/categories/{category_id}")

    def category_attributes(self, category_id: str) -> list[dict[str, Any]]:
        return self.get(f"/categories/{category_id}/attributes")

    def technical_specs(self, category_id: str) -> dict[str, Any]:
        return self.get(f"/categories/{category_id}/technical_specs/input")

    def items(self, merchant_id: int | str, limit: int = 10) -> dict[str, Any]:
        return self.get(f"/marketplace/users/{merchant_id}/items/search", {"limit": limit})

    def orders(self, limit: int = 10) -> dict[str, Any]:
        return self.get("/marketplace/orders/search", {"limit": limit})

    def search_competitors(self, site_id: str, query: str, limit: int = 20) -> dict[str, Any]:
        site = str(site_id or "").upper()
        if site not in {"MLM", "MLB", "MLC", "MLA", "MCO"}:
            raise MercadoLibreAPIError("竞品查询需要具体目标站点。")
        return self.get(f"/sites/{site}/search", {"q": str(query or "")[:160], "limit": max(1, min(limit, 50))})

    def currency_conversion(self, source: str, target: str = "USD") -> Any:
        return self.get("/currency_conversions/search", {"from": source, "to": target})

    def shipping_options(self, site_id: str, zip_from: str, zip_to: str, dimensions: str) -> dict[str, Any]:
        site = str(site_id or "").upper()
        if site not in {"MLM", "MLB", "MLC", "MLA", "MCO"}:
            raise MercadoLibreAPIError("物流报价需要具体目标站点。")
        return self.get(f"/sites/{site}/shipping_options", {
            "zip_code_from": zip_from, "zip_code_to": zip_to, "dimensions": dimensions,
        })


class MercadoLibreCategoryClient:
    """Public, read-only category endpoints that do not need a seller token."""

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{API_BASE}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(payload.get("message") or payload.get("error") or "").strip()
            except Exception:
                pass
            suffix = f"：{detail}" if detail else ""
            raise MercadoLibreAPIError(f"美客多类目接口返回 HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise MercadoLibreAPIError("无法连接美客多类目接口。") from exc

    def domain_discovery(self, site_id: str, query: str) -> list[dict[str, Any]]:
        site = str(site_id or "CBT").upper()
        if site not in {"CBT", "MLM", "MLB", "MLC", "MLA", "MCO"}:
            site = "CBT"
        encoded = urllib.parse.urlencode({"q": str(query or "")[:300]})
        return self.get(f"/sites/{site}/domain_discovery/search?{encoded}")

    def site_categories(self, site_id: str) -> list[dict[str, Any]]:
        site = str(site_id or "CBT").upper()
        if site not in {"CBT", "MLM", "MLB", "MLC", "MLA", "MCO"}:
            site = "CBT"
        return self.get(f"/sites/{site}/categories")

    def category(self, category_id: str) -> dict[str, Any]:
        return self.get(f"/categories/{category_id}")

    def category_attributes(self, category_id: str) -> list[dict[str, Any]]:
        return self.get(f"/categories/{category_id}/attributes")

    def technical_specs(self, category_id: str) -> dict[str, Any]:
        return self.get(f"/categories/{category_id}/technical_specs/input")


def relevant_tags(tags: list[str] | None) -> list[str]:
    keep = {"user_product_seller", "warehouse_management"}
    return sorted(tag for tag in (tags or []) if tag in keep)


def account_probe(client: MercadoLibreClient) -> dict[str, Any]:
    me = client.me()
    merchant_id = me["id"]
    mapping = client.marketplace_mapping(merchant_id)
    tags = relevant_tags(me.get("tags"))
    capacity: Any
    try:
        capacity = client.listing_capacity(merchant_id)
    except MercadoLibreAPIError as exc:
        capacity = {"unavailable": str(exc)}
    items = client.items(merchant_id, 1)
    orders = client.orders(1)
    marketplaces = [
        {
            "user_id": row.get("user_id"),
            "site_id": row.get("site_id"),
            "logistic_type": row.get("logistic_type"),
        }
        for row in mapping.get("marketplaces", [])
    ]
    return {
        "merchant_id": merchant_id,
        "site_id": me.get("site_id"),
        "sell_allowed": ((me.get("status") or {}).get("sell") or {}).get("allow"),
        "relevant_tags": tags,
        "user_products_model": "user_product_seller" in tags,
        "business_model": mapping.get("business_model"),
        "marketplaces": marketplaces,
        "listing_capacity": capacity,
        "items_total": (items.get("paging") or {}).get("total"),
        "orders_total": (orders.get("paging") or {}).get("total"),
    }


def _technical_attributes(specs: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for group in specs.get("groups", []):
        for component in group.get("components", []):
            found.extend(component.get("attributes", []))
    return found


def _tag_names(attribute: dict[str, Any]) -> set[str]:
    tags = attribute.get("tags") or {}
    if isinstance(tags, list):
        return set(tags)
    return {key for key, value in tags.items() if value}


def _attribute_summary(attribute: dict[str, Any]) -> dict[str, Any]:
    tag_names = sorted(_tag_names(attribute))
    values = attribute.get("values") or []
    return {
        "id": attribute.get("id"),
        "name": attribute.get("name"),
        "value_type": attribute.get("value_type"),
        "value_max_length": attribute.get("value_max_length"),
        "tags": tag_names,
        "allowed_units": [unit.get("id") for unit in attribute.get("allowed_units", [])],
        "values_preview": [
            {"id": value.get("id"), "name": value.get("name")} for value in values[:20]
        ],
    }


def category_preflight(client: MercadoLibreClient, category_id: str) -> dict[str, Any]:
    category = client.category(category_id)
    attributes = client.category_attributes(category_id)
    technical_specs_error = None
    try:
        specs = client.technical_specs(category_id)
    except MercadoLibreAPIError as exc:
        specs = {"groups": []}
        technical_specs_error = str(exc)
    merged: dict[str, dict[str, Any]] = {
        str(row.get("id")): dict(row) for row in attributes if row.get("id")
    }
    for row in _technical_attributes(specs):
        attribute_id = row.get("id")
        if not attribute_id:
            continue
        if attribute_id not in merged:
            merged[attribute_id] = dict(row)
            continue
        current = merged[attribute_id]
        for key, value in row.items():
            if key != "tags" and current.get(key) in (None, "", []):
                current[key] = value
        current["tags"] = sorted(_tag_names(current) | _tag_names(row))
    summaries = [_attribute_summary(row) for row in merged.values()]
    required = [row for row in summaries if "required" in row["tags"]]
    recommended = [
        row for row in summaries if "catalog_required" in row["tags"] and row not in required
    ]
    return {
        "category_id": category_id,
        "name": category.get("name"),
        "path_from_root": category.get("path_from_root"),
        "is_leaf": not bool(category.get("children_categories")),
        "settings": {
            key: (category.get("settings") or {}).get(key)
            for key in ("status", "max_variations_allowed", "minimum_price")
        },
        "required_attributes": required,
        "recommended_attributes": recommended,
        "all_attributes": summaries,
        "technical_specs_status": "UNAVAILABLE" if technical_specs_error else "AVAILABLE",
        "technical_specs_error": technical_specs_error,
    }


def create_local_draft(preflight: dict[str, Any], model: str) -> dict[str, Any]:
    by_id = {row["id"]: row for row in preflight.get("all_attributes", [])}
    selected_ids = [row["id"] for row in preflight["required_attributes"]]
    for useful_id in ("ITEM_CONDITION", "BRAND", "MODEL", "SELLER_SKU", "GTIN"):
        if useful_id in by_id and useful_id not in selected_ids:
            selected_ids.append(useful_id)
    attributes = []
    for attribute_id in selected_ids:
        if attribute_id == "ITEM_CONDITION":
            attributes.append({"id": attribute_id, "value_id": "2230284", "value_name": "New"})
        else:
            attributes.append({"id": attribute_id, "value_id": None, "value_name": ""})
    return {
        "status": "DRAFT_LOCAL_ONLY",
        "publish_endpoint": "/global/user-products" if model == "user_products" else "/global/items",
        "model": model,
        "payload": {
            "title": "",
            "family_name": "",
            "description": "",
            "category_id": preflight["category_id"],
            "currency_id": "USD",
            "available_quantity": None,
            "pictures": [],
            "attributes": attributes,
            "sites_to_sell": [],
        },
        "evidence": {
            "supplier_url": "",
            "purchase_cost": None,
            "packed_weight": None,
            "packed_dimensions": None,
            "gtin_or_exemption": "",
            "compatibility_evidence": "",
            "images_human_reviewed": False,
        },
        "human_approved": False,
    }


def orders_snapshot(client: MercadoLibreClient, limit: int) -> dict[str, Any]:
    response = client.orders(limit)
    rows = []
    for cart in response.get("results", []):
        rows.append(
            {
                "order_ids": [order.get("id") for order in cart.get("orders", []) if order.get("id")],
                "item_ids": [item.get("id") for item in (cart.get("config") or {}).get("items", [])],
                "shipment_id": (cart.get("shipment") or {}).get("id"),
            }
        )
    return {
        "total": (response.get("paging") or {}).get("total"),
        "returned": len(rows),
        "orders": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="美客多官方 API 只读接入工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe")
    orders_parser = sub.add_parser("orders")
    orders_parser.add_argument("--limit", type=int, default=10)
    category_parser = sub.add_parser("category")
    category_parser.add_argument("category_id")
    draft_parser = sub.add_parser("draft")
    draft_parser.add_argument("category_id")
    draft_parser.add_argument("--model", choices=("user_products", "legacy"), required=True)
    args = parser.parse_args(argv)

    client = MercadoLibreClient()
    if args.command == "probe":
        result = account_probe(client)
        destination = DATA_DIR / "connection_probe.json"
    elif args.command == "orders":
        if not 1 <= args.limit <= 50:
            raise ValueError("订单 limit 必须在 1 到 50 之间。")
        result = orders_snapshot(client, args.limit)
        destination = DATA_DIR / "orders_snapshot.json"
    else:
        preflight = category_preflight(client, args.category_id)
        if args.command == "category":
            result = preflight
            destination = DATA_DIR / "categories" / f"{args.category_id}.json"
        else:
            result = create_local_draft(preflight, args.model)
            destination = ROOT / "drafts" / f"{args.category_id}-draft.json"
    write_json(destination, result)
    print(json.dumps({"ok": True, "saved": str(destination)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (MercadoLibreAPIError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
