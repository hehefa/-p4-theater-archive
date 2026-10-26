import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("identity_export", Path(__file__).resolve().parents[1] / "scripts/export_identity_crosswalk.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IdentityExportTests(unittest.TestCase):
    def write_projects(self, root, items):
        (root / "catalog").mkdir()
        (root / "catalog/projects.json").write_text(json.dumps({"schema_version": 1, "items": items}), encoding="utf-8")

    def test_keeps_existing_identity_basis_without_publishing_private_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_projects(root, [{
                "id": "project:existing", "accession": "P4-P000001", "title": "示例项目",
                "legacy_reconciliation": "pending", "aliases": ["legacy-code"],
                "existing_registry_identifiers": [{"namespace": "research:slug", "value": "example", "mapping_basis": "directory_title_match_only", "asset_membership_verified": False}],
                "private_source_path": "/private/fixture/source", "internal_note": "do not export",
            }])
            before = (root / "catalog/projects.json").read_bytes()
            module.build(root)
            data = json.loads((root / module.TARGET).read_bytes())
            item = data["items"][0]
            self.assertEqual(item["catalogue_id"], "project:existing")
            self.assertEqual(item["reconciliation_status"], "pending")
            self.assertFalse(item["external_identifiers"][0]["asset_membership_verified"])
            self.assertNotIn("private", (root / module.TARGET).read_text(encoding="utf-8"))
            self.assertEqual(before, (root / "catalog/projects.json").read_bytes())
            self.assertTrue(module.check(root))
            (root / module.TARGET).write_bytes(b"{}")
            self.assertFalse(module.check(root))

    def test_rejects_external_identifier_claimed_by_two_catalogue_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            items = [{"id": "project:" + str(n), "accession": f"P4-P{n:06d}", "title": str(n), "existing_registry_identifiers": [{"namespace": "existing:slug", "value": "same"}]} for n in (1, 2)]
            self.write_projects(root, items)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                module.build(root)
            self.assertFalse((root / module.TARGET).exists())

    def test_nested_private_values_and_string_boolean_never_enter_projection(self):
        cases = [
            {"related_public_project_page": {"private_path": "/private/fixture", "token": "fixture"}},
            {"existing_registry_identifiers": [{"namespace": "research:slug", "value": "example", "mapping_basis": {"private_path": "/private/fixture"}}]},
            {"existing_registry_identifiers": [{"namespace": "research:slug", "value": "example", "asset_membership_verified": "false"}]},
            {"related_public_project_page": "https://b.p4theater.cn/projects/example?token=fixture"},
        ]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.write_projects(root, [{"id": "project:existing", "accession": "P4-P000001", "title": "示例", **change}])
                with self.assertRaises((ValueError, TypeError)):
                    module.build(root)
                self.assertFalse((root / module.TARGET).exists())


if __name__ == "__main__":
    unittest.main()
