import json
import tempfile
import unittest
from pathlib import Path

from src.workflow_orchestrator import build_board, workflow_item


def sample_draft() -> dict:
    return {
        "payload": {"title": "Test product", "category_id": "CBT1", "attributes": []},
        "evidence": {
            "supplier_url": "https://example.invalid/item",
            "purchase_cost": {"amount": 1},
            "packed_weight": {"value": 100},
            "packed_dimensions": {"length": 1, "width": 1, "height": 1},
            "gtin_or_exemption": "official exemption confirmed",
            "compatibility_evidence": "supplier model table",
            "images_human_reviewed": True,
            "image_review": {"gallery": [{"role": "main"}]},
        },
        "human_approved": False,
    }


class WorkflowOrchestratorTests(unittest.TestCase):
    def test_clean_draft_waits_for_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft_path.write_text(json.dumps(sample_draft()), encoding="utf-8")
            item = workflow_item(draft_path, root)
        self.assertEqual(item["stage"], "READY_FOR_AI")
        self.assertFalse(item["publish_allowed"])

    def test_proposal_waits_for_human_then_allows_publish_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft = sample_draft()
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            proposal = root / "ai_outputs" / "test-draft-proposal.json"
            proposal.parent.mkdir()
            proposal.write_text("{}", encoding="utf-8")
            job = root / "jobs" / "test-draft-full-ai-job.json"
            job.parent.mkdir()
            job.write_text(json.dumps({"status": "done", "progress": 100}), encoding="utf-8")
            self.assertEqual(workflow_item(draft_path, root)["stage"], "READY_FOR_HUMAN_REVIEW")
            draft["human_approved"] = True
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            item = workflow_item(draft_path, root)
        self.assertEqual(item["stage"], "READY_TO_PUBLISH")
        self.assertTrue(item["publish_allowed"])

    def test_partial_proposal_does_not_enter_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft_path.write_text(json.dumps(sample_draft()), encoding="utf-8")
            proposal = root / "ai_outputs" / "test-draft-proposal.json"
            proposal.parent.mkdir()
            proposal.write_text("{}", encoding="utf-8")
            item = workflow_item(draft_path, root)
        self.assertEqual(item["stage"], "READY_FOR_AI")

    def test_manual_review_request_allows_human_review_after_ai_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft = sample_draft()
            draft["edit_metadata"] = {"manual_review_requested_at": "2026-09-17T00:00:00+00:00"}
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            job = root / "jobs" / "test-draft-full-ai-job.json"
            job.parent.mkdir()
            job.write_text(json.dumps({"status": "failed", "error": "timeout"}), encoding="utf-8")
            self.assertEqual(workflow_item(draft_path, root)["stage"], "READY_FOR_HUMAN_REVIEW")

    def test_running_full_flow_reports_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft_path.write_text(json.dumps(sample_draft()), encoding="utf-8")
            job = root / "jobs" / "test-draft-full-ai-job.json"
            job.parent.mkdir()
            job.write_text(json.dumps({"status": "running", "stage": "生成公共商品图", "progress": 58, "message": "已完成 1/3"}), encoding="utf-8")
            item = workflow_item(draft_path, root)
        self.assertEqual(item["stage"], "AI_PROCESSING")
        self.assertEqual(item["full_ai_progress"], 58)

    def test_board_counts_blocked_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drafts = root / "drafts"
            drafts.mkdir()
            draft = sample_draft()
            draft["evidence"]["gtin_or_exemption"] = ""
            (drafts / "blocked-draft.json").write_text(json.dumps(draft), encoding="utf-8")
            board = build_board(drafts, root)
        self.assertEqual(board["counts"]["BLOCKED_EVIDENCE"], 1)
        self.assertFalse(board["publishing_performed"])

    def test_site_failure_after_publish_is_partial_not_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_path = root / "drafts" / "test-draft.json"
            draft_path.parent.mkdir()
            draft = sample_draft(); draft["human_approved"] = True
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            proposal = root / "ai_outputs" / "test-draft-proposal.json"; proposal.parent.mkdir(); proposal.write_text("{}", encoding="utf-8")
            job = root / "jobs" / "test-draft-full-ai-job.json"; job.parent.mkdir(); job.write_text(json.dumps({"status": "done"}), encoding="utf-8")
            record = root / "data" / "publish_records" / "test-draft.json"; record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"status": "success", "response": [{"site_items": [{"site_id": "MLB", "item_id": "MLB1"}, {"site_id": "MLM", "error": {"message": "shipping unsupported"}}]}]}), encoding="utf-8")
            item = workflow_item(draft_path, root)
        self.assertEqual(item["stage"], "PUBLISH_PARTIAL")
        self.assertEqual(item["publish_status"], "partial")


if __name__ == "__main__":
    unittest.main()
