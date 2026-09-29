"""Local workflow board for the self-owned Mercado Libre automation.

This module never publishes products and never calls a paid model. It turns
drafts, validation results, AI proposals, and human approval into one clear
local task board.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from .ai_listing_pipeline import prepare
    from .validate_listing import validate
except ImportError:  # Direct script execution.
    from ai_listing_pipeline import prepare
    from validate_listing import validate


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def publish_status_from_record(record: dict[str, Any] | None) -> tuple[str, str | None]:
    """Return the actual marketplace outcome, including partial site failures."""
    if not record:
        return "pending", None
    if record.get("variants"):
        sites = [site for variant in record.get("variants") or [] for site in variant.get("sites") or []]
        failures = [site for site in sites if site.get("status") == "failed"]
        successes = [site for site in sites if site.get("status") == "success"]
        pending = [site for site in sites if site.get("status") == "pending_confirmation"]
        messages = "；".join(f"{site.get('site_id') or '-'}：{site.get('error') or '发布失败'}" for site in failures) or None
        if pending:
            return "pending_confirmation", messages
        if failures:
            return ("partial" if successes else "failed"), messages
        if successes:
            return "success", None
    response = record.get("response")
    results = response if isinstance(response, list) else [response]
    site_rows = [
        site for result in results if isinstance(result, dict)
        for site in (result.get("site_items") or []) if isinstance(site, dict)
    ]
    failures = [site for site in site_rows if site.get("error")]
    successes = [site for site in site_rows if site.get("item_id") and not site.get("error")]
    if failures:
        messages = []
        for site in failures:
            error = site.get("error") or {}
            cause = (error.get("cause") or [{}])[0]
            detail = cause.get("message") if isinstance(cause, dict) else None
            messages.append(f"{site.get('site_id') or '-'}：{detail or error.get('message') or '发布失败'}")
        return ("partial" if successes else "failed"), "；".join(messages)
    return str(record.get("status") or "pending"), record.get("error")


def workflow_item(draft_path: Path, root: Path = ROOT) -> dict[str, Any]:
    draft = read_json(draft_path)
    errors, warnings = validate(draft)
    proposal_path = root / "ai_outputs" / f"{draft_path.stem}-proposal.json"
    request_path = root / "jobs" / f"{draft_path.stem}-ai-request.json"
    review_path = root / "reviews" / f"{draft_path.stem}-review.json"
    full_job_path = root / "jobs" / f"{draft_path.stem}-full-ai-job.json"
    full_job = read_json(full_job_path) if full_job_path.exists() else None
    publish_path = root / "data" / "publish_records" / f"{draft_path.stem}.json"
    publish_record = read_json(publish_path) if publish_path.exists() else None
    publish_status, publish_error = publish_status_from_record(publish_record)
    has_proposal = proposal_path.exists()
    human_approved = draft.get("human_approved") is True
    manual_review_requested = bool(
        ((draft.get("edit_metadata") or {}).get("manual_review_requested_at"))
    )

    full_status = (full_job or {}).get("status")
    if publish_status == "success":
        stage = "PUBLISHED"
        next_action = "已发布到全部目标站点"
    elif publish_status == "pending_confirmation":
        stage = "PUBLISH_PENDING_CONFIRMATION"
        next_action = "发布请求已受理，等待回查站点结果"
    elif publish_status == "partial":
        stage = "PUBLISH_PARTIAL"
        next_action = "部分站点发布成功；" + str(publish_error or "请查看发布回执")
    elif errors:
        stage = "BLOCKED_EVIDENCE"
        next_action = "补齐证据：" + "；".join(errors)
    elif full_status in {"queued", "running"}:
        stage = "AI_PROCESSING"
        next_action = str((full_job or {}).get("message") or "完整 AI 流程处理中")
    elif manual_review_requested and not human_approved:
        stage = "READY_FOR_HUMAN_REVIEW"
        next_action = "已手动进入审核；请核对并补齐发布字段"
    elif full_status == "paused":
        stage = "AI_PAUSED"
        next_action = "完整 AI 流程已暂停，可重新运行"
    elif full_status == "failed":
        stage = "AI_FAILED"
        next_action = str((full_job or {}).get("error") or "完整 AI 流程失败，可查看日志后重试")
    elif not full_job or full_status != "done":
        stage = "READY_FOR_AI"
        next_action = "可以启动完整 AI 流程"
    elif not has_proposal:
        stage = "READY_FOR_AI"
        next_action = "可以启动完整 AI 流程"
    elif not human_approved:
        stage = "READY_FOR_HUMAN_REVIEW"
        next_action = "人工核对文案、图片、类目、属性和价格"
    else:
        stage = "READY_TO_PUBLISH"
        next_action = "等待用户明确授权后才能发布"

    image_count = len(
        (((draft.get("evidence") or {}).get("image_review") or {}).get("gallery") or [])
    )
    return {
        "draft": draft_path.relative_to(root).as_posix() if draft_path.is_relative_to(root) else str(draft_path),
        "product": (draft.get("payload") or {}).get("family_name")
        or (draft.get("payload") or {}).get("title")
        or draft_path.stem,
        "category_id": (draft.get("payload") or {}).get("category_id"),
        "stage": stage,
        "next_action": next_action,
        "errors": errors,
        "warnings": warnings,
        "image_count": image_count,
        "request_prepared": request_path.exists(),
        "review_prepared": review_path.exists(),
        "ai_proposal_ready": has_proposal,
        "full_ai_status": full_status,
        "full_ai_stage": (full_job or {}).get("stage"),
        "full_ai_progress": (full_job or {}).get("progress"),
        "full_ai_message": (full_job or {}).get("message"),
        "full_ai_error": (full_job or {}).get("error"),
        "human_approved": human_approved,
        "publish_allowed": stage == "READY_TO_PUBLISH",
        "publish_status": publish_status,
        "publish_error": publish_error,
    }


def build_board(drafts_dir: Path, root: Path = ROOT) -> dict[str, Any]:
    items = [workflow_item(path, root) for path in sorted(drafts_dir.glob("*-draft.json"))]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["stage"]] = counts.get(item["stage"], 0) + 1
    return {
        "mode": "LOCAL_DRAFT_ONLY",
        "publishing_performed": any(item.get("publish_status") in {"success", "partial"} for item in items),
        "paid_model_called": False,
        "counts": counts,
        "items": items,
    }


def prepare_all(drafts_dir: Path, model: str) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    for draft_path in sorted(drafts_dir.glob("*-draft.json")):
        request_path, review_path = prepare(draft_path, model)
        outputs.append(
            {
                "draft": str(draft_path),
                "request": str(request_path),
                "review": str(review_path),
            }
        )
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="美客多自有自动化任务状态中心")
    parser.add_argument("command", choices=("board", "prepare"))
    parser.add_argument("--drafts", type=Path, default=ROOT / "drafts")
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args(argv)

    prepared: list[dict[str, str]] = []
    if args.command == "prepare":
        prepared = prepare_all(args.drafts, args.model)

    board = build_board(args.drafts)
    destination = ROOT / "data" / "workflow_board.json"
    write_json(destination, board)
    print(
        json.dumps(
            {
                "ok": True,
                "board": str(destination),
                "counts": board["counts"],
                "prepared": prepared,
                "publishing_performed": False,
                "paid_model_called": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
