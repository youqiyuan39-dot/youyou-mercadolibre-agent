import json
import tempfile
import unittest
from pathlib import Path

from src.agent_blueprint import BlueprintError, load_blueprint, summarize


class AgentBlueprintTests(unittest.TestCase):
    def test_project_blueprint_keeps_marketplace_writes_disabled(self):
        summary = summarize(load_blueprint())
        self.assertIn("publish_executor", summary["disabled_write_modules"])
        self.assertGreaterEqual(summary["module_count"], 10)

    def test_enabled_write_module_is_rejected(self):
        value = {
            "modules": [
                {
                    "id": "unsafe_publish",
                    "writes_marketplace": True,
                    "enabled": True,
                    "requires_explicit_user_authorization": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modules.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(BlueprintError):
                load_blueprint(path)


if __name__ == "__main__":
    unittest.main()
