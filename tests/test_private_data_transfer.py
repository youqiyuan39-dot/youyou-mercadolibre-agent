import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.portable_data import export_data, import_data


class PrivateDataTransferTests(unittest.TestCase):
    def test_export_import_preserves_business_data_without_secrets_or_static_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old, new = base / "old", base / "new"
            old.mkdir()
            new.mkdir()
            samples = {
                "assets/products/image.png": b"image",
                "drafts/item-draft.json": b"{}",
                "data/publish_records/item.json": b"{}",
                "reviews/item-approval.json": b"{}",
                "config/model_settings.json": b"{}",
                ".secrets/mercadolibre_tokens.dat": b"private",
                "data/workflow_board.json": b"stale",
                "assets/brand/youyou-logo.png": b"bundled",
            }
            for name, body in samples.items():
                path = old / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            archive = base / "private.zip"
            self.assertEqual(export_data(archive, old), 5)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(
                    sorted(bundle.namelist()),
                    sorted(name for name in samples if name not in {
                        ".secrets/mercadolibre_tokens.dat",
                        "data/workflow_board.json",
                        "assets/brand/youyou-logo.png",
                    }),
                )
            self.assertEqual(import_data(archive, new), 5)
            self.assertEqual((new / "assets/products/image.png").read_bytes(), b"image")
            self.assertFalse((new / ".secrets").exists())
            with self.assertRaises(FileExistsError):
                import_data(archive, new)

    def test_import_rejects_archive_path_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "project"
            root.mkdir()
            archive = base / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("drafts/../../escape.json", "{}")
            with self.assertRaises(ValueError):
                import_data(archive, root)
            self.assertFalse((base / "escape.json").exists())


if __name__ == "__main__":
    unittest.main()