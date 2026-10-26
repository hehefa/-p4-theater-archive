import importlib.util
import json
import tempfile
import unittest
import contextlib
import hashlib
import io
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_catalog", ROOT / "scripts" / "archive_catalog.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CatalogueTests(unittest.TestCase):
    def test_registry_append_does_not_renumber_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = module.Registry(tmp)
            first = registry.get("asset", "first")
            second = registry.get("asset", "second")
            registry.save()
            loaded = module.Registry(tmp)
            third = loaded.get("asset", "alphabetically_before_first")
            self.assertEqual(first, loaded.get("asset", "first"))
            self.assertEqual(second, loaded.get("asset", "second"))
            self.assertEqual(third["accession"], "P4-V000003")
            self.assertNotEqual(first["id"], third["id"])

    def test_dates_do_not_invent_year_or_accept_invalid_dates(self):
        self.assertIsNone(module.label_date("4.19")['value'])
        self.assertIsNone(module.label_date("20220231")['value'])
        self.assertEqual(module.label_date("20220818排练")['value'], "2022-08-18")

    def test_subtitle_review_detects_overlap_and_empty_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            caption = Path(tmp) / "sample.srt"
            caption.write_text("1\n00:00:01,000 --> 00:00:03,000\n甲\n\n2\n00:00:02,000 --> 00:00:04,000\n乙\n", encoding="utf-8")
            self.assertEqual(module.srt_metadata(caption)["issue_counts"], {"overlap": 1})
            caption.write_text("", encoding="utf-8")
            self.assertEqual(module.srt_metadata(caption)["format_status"], "empty_no_recognized_text")

    def test_incremental_import_retains_asset_id_history_and_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "masters"
            source.mkdir()
            video = source / "IMG_0002.MOV"
            video.write_bytes(b"first version")
            audit = {"generated_at": "2026-01-01T00:00:00Z", "projects": [{"directory_label": "3P小组 2020", "project_root": str(source), "videos": []}]}
            def refresh():
                audit["projects"][0]["videos"] = [{"path": str(p), "relative_path": p.name, "bytes": p.stat().st_size, "extension": p.suffix, "derived_candidate": False, "project_media_copy_candidate": False} for p in sorted(source.glob("*.MOV"))]
                module.write_json(work / "audit.json", audit)
                args = SimpleNamespace(output=str(work / "public"), local_state=str(work / "private"), audit=str(work / "audit.json"), quality=None, crosswalk=None)
                with contextlib.redirect_stdout(io.StringIO()):
                    module.import_catalog(args)
            refresh()
            assets_path = work / "public/catalog/assets.json"
            original = module.read_json(assets_path)
            first_id = original["items"][0]["id"]
            first_file_id = original["items"][0]["file_id"]
            original["items"][0]["human_review"] = "passed"
            module.write_json(assets_path, original)
            (source / "IMG_0001.MOV").write_bytes(b"new earlier sorted file")
            video.write_bytes(b"second version changed bytes")
            refresh()
            updated = module.read_json(assets_path)["items"]
            first = next(x for x in updated if x["id"] == first_id)
            self.assertEqual(first["human_review"], "needs_review_source_version_changed")
            self.assertEqual(first["prior_version_review"]["human_review"], "passed")
            self.assertEqual(first["accession"], "P4-V000001")
            self.assertNotEqual(first["file_id"], first_file_id)
            files = module.read_json(work / "public/catalog/files.json")["items"]
            historical = next(x for x in files if x["id"] == first_file_id)
            self.assertFalse(historical["in_current_inventory"])
            self.assertTrue(module.validate(work / "public")["valid"])

    def test_checksum_queue_resumes_without_rehashing_finished_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            source = state / "fixture.bin"
            source.write_bytes(b"checksum fixture")
            stat = source.stat()
            module.write_json(state / "hash_plan.json", {"files": [{"file_id": "fixture", "accession": "fixture", "path": str(source), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}]})
            command = [sys.executable, str(ROOT / "scripts/checksums.py"), "--local-state", str(state), "--limit", "1"]
            first = subprocess.run(command, capture_output=True, text=True, check=True)
            second = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(first.stdout.splitlines()[-1])["hashed_this_run"], 1)
            self.assertEqual(json.loads(second.stdout.splitlines()[-1])["hashed_this_run"], 0)

    def test_publication_is_only_an_operator_report_and_preserves_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            work = Path(tmp)
            public = work / "public"
            shutil.copytree(ROOT / "catalog", public / "catalog")
            assets = module.read_json(public / "catalog/assets.json")["items"]
            files = {x["id"]: x for x in module.read_json(public / "catalog/files.json")["items"]}
            asset = next(x for x in assets if x["publication_status"].startswith("hold_") and files[x["file_id"]]["bytes"] > 0 and x["subtitle_ids"])
            original_hold = asset["publication_status"]
            options = dict(root=str(public), local_state=str(work / "private"), asset=asset["id"], channel="UCp_LyzepcrZX6Cgzv2Qit4w", video="Fixture_001", visibility="public", observed_at="2026-10-26T06:00:00-07:00", playback_status="operator_reported_playable", caption_status="not_recorded", subtitle=[])
            for changes in ({"channel": "wrong"}, {"observed_at": "yesterday"}, {"observed_at": "2026-10-26T06:00:00"}, {"playback_status": "verified"}, {"caption_status": "operator_reported_visible"}):
                with self.assertRaises(ValueError):
                    module.record_publication(SimpleNamespace(**{**options, **changes}))
            with contextlib.redirect_stdout(io.StringIO()):
                module.record_publication(SimpleNamespace(**options))
            updated = next(x for x in module.read_json(public / "catalog/assets.json")["items"] if x["id"] == asset["id"])
            self.assertEqual(updated["publication_status"], original_hold)
            summary = module.read_json(public / "catalog/summary.json")
            self.assertEqual(summary["publication_reports_recorded"], 1)
            self.assertEqual(summary["youtube_uploads_recorded"], 0)
            pub = module.read_json(public / "catalog/publications.json")["items"][0]
            self.assertEqual(pub["record_basis"], "operator_reported")
            self.assertEqual(pub["platform_readback_verification"], "not_recorded")
            other = next(x for x in assets if x["id"] != asset["id"] and files[x["file_id"]]["bytes"] > 0)
            with self.assertRaises(ValueError):
                module.record_publication(SimpleNamespace(**{**options, "asset": other["id"]}))
            empty = next(x for x in assets if files[x["file_id"]]["bytes"] == 0)
            with self.assertRaises(ValueError):
                module.record_publication(SimpleNamespace(**{**options, "asset": empty["id"], "video": "Fixture_002"}))
            self.assertTrue(module.validate(public)["valid"])

    def test_only_scope_reviewed_hash_bound_caption_export_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            public = Path(tmp) / "public"
            shutil.copytree(ROOT / "catalog", public / "catalog")
            # Actual repository exports, if present, are irrelevant to this
            # synthetic export fixture; clear their declarations in this copy.
            table = module.read_json(public / "catalog/subtitles.json")
            for record in table["items"]:
                record.pop("public_export", None)
            sub = table["items"][0]
            content = b"1\n00:00:01,000 --> 00:00:02,000\nSynthetic fixture\n"
            sha = hashlib.sha256(content).hexdigest()
            relative = "captions/" + sub["accession"] + "/" + sha + "/original.srt"
            sub["version_sha256"] = sha
            sub["public_text_status"] = "public_machine_draft"
            sub["public_export"] = {"path": relative, "sha256": sha, "scope_review_status": "approved_for_public_archive", "quality_label": "machine_draft"}
            module.write_json(public / "catalog/subtitles.json", table)
            caption = public / relative
            caption.parent.mkdir(parents=True)
            caption.write_bytes(content)
            self.assertTrue(module.validate(public)["valid"])
            caption.write_bytes(content + b"tampered")
            self.assertTrue(any("hash mismatch" in x for x in module.validate(public)["errors"]))
            caption.write_bytes(content)
            (public / "captions/rogue.srt").write_bytes(content)
            self.assertTrue(any("Prohibited media" in x for x in module.validate(public)["errors"]))

    def test_public_catalogue_references_and_privacy_guard(self):
        self.assertTrue(module.validate(ROOT)["valid"], module.validate(ROOT)["errors"])
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            work = Path(tmp) / "public"
            shutil.copytree(ROOT / "catalog", work / "catalog")
            files = module.read_json(work / "catalog" / "files.json")
            files["items"][0]["asset_id"] = "asset:missing"
            module.write_json(work / "catalog" / "files.json", files)
            (work / "leak.md").write_text(chr(67) + ":" + chr(92) + "private" + chr(92) + "source.mp4", encoding="utf-8")
            (work / "unexpected.mp4").write_bytes(b"x")
            errors = module.validate(work)["errors"]
            self.assertTrue(any("Broken reference" in e for e in errors))
            self.assertTrue(any("Absolute local path" in e for e in errors))
            self.assertTrue(any("Prohibited media" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
