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


def metadata_receipt_fixture(asset, source_sha):
    return {"id": "publication:fixture", "accession": "P4-U000001", "kind": "publication", "title": "Synthetic metadata receipt fixture", "asset_id": asset["id"], "file_id": asset["file_id"], "channel_id": "UCp_LyzepcrZX6Cgzv2Qit4w", "youtube_video_id": "Fixture_001", "url": "https://www.youtube.com/watch?v=Fixture_001", "visibility": "public", "record_basis": "youtube_studio_metadata_readback", "platform_readback_verification": "verified", "verification_scope": "metadata_only", "source_sha256": source_sha, "description_source_sha256": source_sha, "source_mapping_status": "exact_description_source_sha256_match", "uploaded_stream_hash_verified": False, "publication_origin": "pre_existing", "new_upload_this_batch": False, "new_upload_completed": False, "playback_status": "not_checked", "caption_publication_status": "not_checked", "subtitle_ids": [], "recorded_at": "2026-10-26T06:00:00-07:00", "platform_observed_at": None, "observation_timestamp_status": "not_supplied_by_source_receipt", "observation_receipt": {"sha256": hashlib.sha256(b"synthetic-receipt").hexdigest(), "interface": "YouTube Studio videos and shorts table DOM"}, "in_current_inventory": True}


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
            if (ROOT / "captions").exists():
                shutil.copytree(ROOT / "captions", public / "captions")
            assets = module.read_json(public / "catalog/assets.json")["items"]
            files = {x["id"]: x for x in module.read_json(public / "catalog/files.json")["items"]}
            existing_publications = module.read_json(public / "catalog/publications.json")["items"]
            registry = module.Registry(work / "private")
            registry.data["counters"]["publication"] = max((int(x["accession"].removeprefix("P4-U")) for x in existing_publications), default=0)
            registry.save()
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
            pub = next(x for x in module.read_json(public / "catalog/publications.json")["items"] if x["youtube_video_id"] == "Fixture_001")
            self.assertEqual(pub["record_basis"], "operator_reported")
            self.assertEqual(pub["platform_readback_verification"], "not_recorded")
            other = next(x for x in assets if x["id"] != asset["id"] and files[x["file_id"]]["bytes"] > 0)
            with self.assertRaises(ValueError):
                module.record_publication(SimpleNamespace(**{**options, "asset": other["id"]}))
            empty = next(x for x in assets if files[x["file_id"]]["bytes"] == 0)
            with self.assertRaises(ValueError):
                module.record_publication(SimpleNamespace(**{**options, "asset": empty["id"], "video": "Fixture_002"}))
            if existing_publications:
                verified = next(x for x in existing_publications if x.get("platform_readback_verification") == "verified")
                with self.assertRaisesRegex(ValueError, "cannot overwrite"):
                    module.record_publication(SimpleNamespace(**{**options, "asset": verified["asset_id"], "video": verified["youtube_video_id"]}))
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
                record.pop("equivalent_public_export_subtitle_id", None)
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

    def test_incremental_import_preserves_caption_release_and_reviewed_project_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            public, private = work / "public", work / "private"
            roots = [work / "source-a", work / "source-b"]
            caption_body = b"1\n00:00:01,000 --> 00:00:02,000\nSynthetic caption\n"
            audit = {"generated_at": "2026-01-01T00:00:00Z", "projects": []}
            cache = {"schema_version": 1, "files": {}}
            for folder, label in zip(roots, ("3P小组 2020", "动物农场")):
                folder.mkdir()
                video = folder / "IMG_0001.MOV"
                video.write_bytes(b"identical video fixture bytes")
                stat = video.stat()
                cache["files"][module.pathkey(video)] = {"path": str(video), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "verified_at": "2026-01-01T00:00:00Z"}
                audit["projects"].append({"directory_label": label, "project_root": str(folder), "videos": [{"path": str(video), "relative_path": video.name, "bytes": stat.st_size, "extension": ".MOV", "derived_candidate": False, "project_media_copy_candidate": False}]})
            (roots[0] / "IMG_0001.srt").write_bytes(caption_body)
            module.write_json(private / "hash_results.json", cache)
            module.write_json(work / "audit.json", audit)
            args = SimpleNamespace(output=str(public), local_state=str(private), audit=str(work / "audit.json"), quality=None, crosswalk=None)
            with contextlib.redirect_stdout(io.StringIO()):
                module.import_catalog(args)
            assets = module.read_json(public / "catalog/assets.json")
            primary, copy = assets["items"]
            reviewed_project_id = copy["project_ids"][0]
            primary["project_ids"] = [reviewed_project_id]
            primary["content_scope_review"] = {"status": "approved_for_public_archive", "source_file_id": primary["file_id"]}
            primary["project_assignment_review"] = {"status": "evidence_supported_project_correction"}
            primary["byte_copy_asset_ids"] = [copy["id"]]
            copy["canonical_byte_source_asset_id"] = primary["id"]
            copy["canonical_byte_source_file_id"] = primary["file_id"]
            copy["byte_identity_relation"] = {"status": "full_sha256_equal"}
            module.write_json(public / "catalog/assets.json", assets)
            captions = module.read_json(public / "catalog/subtitles.json")
            sub = captions["items"][0]
            relative = "captions/" + sub["accession"] + "/" + sub["version_sha256"] + "/original.srt"
            target = public / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(caption_body)
            sub.update(public_export={"path": relative, "sha256": sub["version_sha256"], "scope_review_status": "approved_for_public_archive", "quality_label": "machine_draft"}, public_text_status="public_machine_draft", source_file_id=primary["file_id"], source_asset_id=primary["id"], pairing_review={"method": "reviewed_source_stem_association"})
            old_body = b"1\n00:00:00,000 --> 00:00:01,000\nOld platform cue\n\n2\n00:00:01,000 --> 00:00:02,000\nSeparate old cue\n"
            old_sha = hashlib.sha256(old_body).hexdigest()
            legacy = {**sub, "id": "subtitle:legacy-fixture", "accession": "P4-S000002", "role": "platform_original_language_legacy", "version_sha256": old_sha, "bytes": len(old_body), "cue_count": 2, "quality_label": "platform_review_level_unknown", "human_listening_review": "not_recorded", "public_text_status": "public_platform_version_review_unknown", "existing_platform_label_is_not_review_evidence": True, "platform_track_observation": {"video_id": "Fixture_001", "actual_name": "Original language - machine reviewed", "download_sha256": old_sha, "unchanged_online": True}}
            old_path = "captions/P4-S000002/" + old_sha + "/legacy.srt"
            legacy["public_export"] = {"path": old_path, "sha256": old_sha, "scope_review_status": "approved_same_official_source_platform_version_preservation", "quality_label": "platform_review_level_unknown"}
            (public / old_path).parent.mkdir(parents=True)
            (public / old_path).write_bytes(old_body)
            captions["items"].append(legacy)
            primary["subtitle_ids"].append(legacy["id"])
            primary["platform_subtitle_ids"] = [legacy["id"]]
            module.write_json(public / "catalog/subtitles.json", captions)
            events = module.read_json(public / "catalog/events.json")
            event_ids = [x["id"] for x in events["items"]]
            events["items"][0].update(project_ids=[reviewed_project_id], content_scope_review={"status": "reviewed_mixed_source_group"})
            module.write_json(public / "catalog/events.json", events)
            project_table = module.read_json(public / "catalog/projects.json")
            project_table["items"][0]["aliases"].append("legacy-fixture-code")
            module.write_json(public / "catalog/projects.json", project_table)
            fixture_pub = metadata_receipt_fixture(primary, cache["files"][module.pathkey(roots[0] / "IMG_0001.MOV")]["sha256"])
            module.write_json(public / "catalog/publications.json", {"schema_version": 1, "items": [fixture_pub]})
            primary["youtube_publication_ids"].append(fixture_pub["id"])
            primary["youtube_metadata_association_status"] = "pre_existing_public_video_source_description_sha256_matched"
            module.write_json(public / "catalog/assets.json", assets)
            with contextlib.redirect_stdout(io.StringIO()):
                module.import_catalog(args)
            updated_assets = module.read_json(public / "catalog/assets.json")["items"]
            updated_captions = module.read_json(public / "catalog/subtitles.json")["items"]
            updated_events = module.read_json(public / "catalog/events.json")["items"]
            self.assertEqual(updated_assets[0]["project_ids"], [reviewed_project_id])
            self.assertEqual(updated_assets[1]["canonical_byte_source_asset_id"], primary["id"])
            self.assertEqual(updated_captions[0]["public_export"], sub["public_export"])
            self.assertEqual(updated_captions[0]["pairing_review"], sub["pairing_review"])
            self.assertEqual([x["id"] for x in updated_events], event_ids)
            self.assertEqual(updated_events[0]["project_ids"], [reviewed_project_id])
            self.assertEqual(module.read_json(public / "catalog/summary.json")["public_caption_export_count"], 2)
            preserved_old = next(x for x in updated_captions if x["id"] == legacy["id"])
            self.assertEqual(preserved_old["cue_count"], 2)
            self.assertEqual(preserved_old["quality_label"], "platform_review_level_unknown")
            self.assertEqual(updated_captions[0]["cue_count"], 1)
            self.assertIn(legacy["id"], updated_assets[0]["subtitle_ids"])
            self.assertIn("legacy-fixture-code", module.read_json(public / "catalog/projects.json")["items"][0]["aliases"])
            self.assertEqual(module.read_json(public / "catalog/publications.json")["items"], [fixture_pub])
            self.assertIn(fixture_pub["id"], updated_assets[0]["youtube_publication_ids"])
            summary = module.read_json(public / "catalog/summary.json")
            self.assertEqual(summary["pre_existing_video_associations_verified"], 1)
            self.assertEqual(summary["new_uploads_completed_this_batch"], 0)
            self.assertEqual(summary["playback_verifications_recorded"], 0)
            self.assertTrue(module.validate(public)["valid"], module.validate(public)["errors"])

    def test_official_registry_duplicates_references_and_path_leaks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            public = Path(tmp) / "public"
            for folder in ("catalog", "captions", "registry"):
                if (ROOT / folder).exists():
                    shutil.copytree(ROOT / folder, public / folder)
            registry_path = public / "registry/official-projects.json"
            self.assertTrue(module.validate(public)["valid"])
            baseline = module.read_json(registry_path)
            broken = json.loads(json.dumps(baseline))
            broken["items"].append(broken["items"][0])
            module.write_json(registry_path, broken)
            self.assertTrue(any("Duplicate or invalid official project code" in x for x in module.validate(public)["errors"]))
            broken = json.loads(json.dumps(baseline))
            linked = next(x for x in broken["items"] if x["current_video_catalogue_matches"])
            linked["current_video_catalogue_matches"][0]["id"] = "project:missing"
            module.write_json(registry_path, broken)
            self.assertTrue(any("Broken or overstated official registry project reference" in x for x in module.validate(public)["errors"]))
            broken = json.loads(json.dumps(baseline))
            candidate = next(x for x in broken["items"] if x["candidate_current_video_catalogue_matches"])
            candidate["candidate_current_video_catalogue_matches"][0]["same_as_established"] = True
            module.write_json(registry_path, broken)
            self.assertTrue(any("title candidate cannot assert identity" in x for x in module.validate(public)["errors"]))
            broken = json.loads(json.dumps(baseline))
            broken["items"][0]["private_path"] = chr(67) + ":" + chr(92) + "private" + chr(92) + "fixture"
            module.write_json(registry_path, broken)
            self.assertTrue(any("Absolute local path" in x for x in module.validate(public)["errors"]))

    def test_metadata_receipt_cannot_overstate_playback_new_upload_or_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            public = Path(tmp) / "public"
            for folder in ("catalog", "captions", "registry"):
                if (ROOT / folder).exists():
                    shutil.copytree(ROOT / folder, public / folder)
            path = public / "catalog/publications.json"
            baseline = module.read_json(path)
            metadata = next(x for x in baseline["items"] if x.get("record_basis") == "youtube_studio_metadata_readback")
            self.assertTrue(module.validate(public)["valid"])
            cases = [({"playback_status": "verified"}, "cannot assert playback"), ({"new_upload_this_batch": True, "new_upload_completed": True}, "cannot count as a new upload"), ({"description_source_sha256": "0" * 64}, "does not match catalogued source"), ({"recorded_at": "yesterday"}, "Invalid Studio observation"), ({"record_basis": "operator_reported"}, "cannot assert platform verification")]
            for changes, message in cases:
                broken = json.loads(json.dumps(baseline))
                target = next(x for x in broken["items"] if x["id"] == metadata["id"])
                target.update(changes)
                module.write_json(path, broken)
                self.assertTrue(any(message in error for error in module.validate(public)["errors"]), changes)

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
