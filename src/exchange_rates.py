"""Read-only daily exchange rates used by local pricing calculations."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ExchangeRateError(RuntimeError):
    pass


def fetch_exchange_rate(source: str, target: str = "USD") -> dict[str, Any]:
    source = str(source or "").upper()
    target = str(target or "USD").upper()
    if source == target:
        return {"source": source, "target": target, "rate": 1.0, "date": None, "provider": "identity"}
    query = urllib.parse.urlencode({"from": source, "to": target})
    request = urllib.request.Request(
        f"https://api.frankfurter.app/latest?{query}",
        headers={"Accept": "application/json", "User-Agent": "Youyou-MercadoLibre-Local/0.2"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise ExchangeRateError("汇率服务暂时不可用") from exc
    try:
        rate = float((payload.get("rates") or {})[target])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExchangeRateError(f"汇率服务没有返回 {source}/{target}") from exc
    if rate <= 0:
        raise ExchangeRateError("汇率服务返回了无效汇率")
    return {"source": source, "target": target, "rate": rate, "date": payload.get("date"), "provider": "Frankfurter (ECB reference rates)"}
