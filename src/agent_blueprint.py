"""Validate and print the local Mercado Libre agent module blueprint.

The blueprint is deliberately safe by default: marketplace write modules must
stay disabled until a later command receives explicit user authorization.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BLUEPRINT = ROOT / "config" / "agent_modules.json"


class BlueprintError(ValueError):
    pass


def load_blueprint(path: Path = DEFAULT_BLUEPRINT) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    modules = value.get("modules")
    if not isinstance(modules, list) or not modules:
        raise BlueprintError("agent_modules.json 缺少 modules。")

    seen: set[str] = set()
    for module in modules:
        if not isinstance(module, dict) or not module.get("id"):
            raise BlueprintError("每个模块都必须有 id。")
        module_id = str(module["id"])
        if module_id in seen:
            raise BlueprintError(f"模块 id 重复：{module_id}")
        seen.add(module_id)

        if module.get("writes_marketplace"):
            if module.get("enabled", True):
                raise BlueprintError(f"外部写入模块必须默认关闭：{module_id}")
            if module.get("requires_explicit_user_authorization") is not True:
                raise BlueprintError(f"外部写入模块必须要求用户明确授权：{module_id}")
    return value


def summarize(value: dict[str, Any]) -> dict[str, Any]:
    modules = value["modules"]
    return {
        "ok": True,
        "mode": value.get("default_mode"),
        "module_count": len(modules),
        "read_or_local_modules": [
            module["id"] for module in modules if not module.get("writes_marketplace")
        ],
        "disabled_write_modules": [
            module["id"]
            for module in modules
            if module.get("writes_marketplace") and module.get("enabled") is False
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查美客多 Agent 模块蓝图")
    parser.add_argument("--config", type=Path, default=DEFAULT_BLUEPRINT)
    args = parser.parse_args(argv)
    print(json.dumps(summarize(load_blueprint(args.config)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (BlueprintError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
