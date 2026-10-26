import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_reading", ROOT / "scripts/public_reading.py")
reading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reading)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


class ReadingTests(unittest.TestCase):
    def test_individual_practice_evidence_from_one_official_site_keeps_scope_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, _ = self.fixture(root)
            asset = tables["assets"][0]
            proof = asset["official_media_evidence"]
            proof.update(decision="confirmed_p4_practice_media", scope="item_level_project_or_practice_provenance_only", formal_project_claim=False)
            proof["evidence"] = proof["evidence"][:1]
            write_json(root / "catalog/assets.json", {"items": tables["assets"]})
            write_json(root / "evidence" / (asset["accession"] + "-official-media.json"), proof)
            self.assertTrue(reading.build(root)["ok"])
            self.assertTrue(reading.check(root)["ok"])
            proof["source_sha256"] = "b" * 64
            write_json(root / "catalog/assets.json", {"items": tables["assets"]})
            with self.assertRaisesRegex(ValueError, "exact official source"):
                reading.build(root)

    def test_legacy_platform_metadata_is_not_public_search_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, _ = self.fixture(root)
            tables["subtitles"].append({"id": "subtitle:legacy", "accession": "P4-S000005", "asset_ids": [tables["assets"][0]["id"]], "source_asset_id": tables["assets"][0]["id"], "source_file_id": "file:one", "language": "zh", "role": "platform_original_language_legacy", "quality_label": "platform_review_level_unknown", "public_text_status": "withheld_platform_legacy_text_context_review_pending", "platform_track_actual_name": "Original language - machine reviewed"})
            write_json(root / "catalog/subtitles.json", {"items": tables["subtitles"]})
            self.assertEqual(reading.build(root)["public_captions"], 2)
            self.assertEqual(reading.search(root, "machine reviewed"), [])
            self.assertTrue(reading.check(root)["ok"])

    def approve_exports(self, root, tables):
        """Synthetic approvals contain no real private material or provenance."""
        exports = [s for s in tables["subtitles"] if s.get("public_export")]
        for asset in tables["assets"]:
            subs = [s for s in exports if s.get("source_asset_id") == asset["id"]]
            if not subs:
                continue
            source = next(f for f in tables["files"] if f["id"] == asset["file_id"])
            proof = {"decision": "confirmed_project_media", "scope": "project_media_provenance_only",
                     "asset_id": asset["id"], "asset_accession": asset["accession"],
                     "file_id": source["id"], "file_accession": source["accession"],
                     "source_sha256": source["sha256"], "evidence": [
                         {"url": "https://a.p4theater.cn/videos/fixture", "http_status": 200, "html_utf8_sha256": "c" * 64},
                         {"url": "https://b.p4theater.cn/questions/fixture", "http_status": 200, "html_utf8_sha256": "d" * 64}]}
            asset.update(official_media_evidence=proof, public_caption_export_allowed=True,
                         official_media_affiliation_status="confirmed_official_source_and_project_context")
            write_json(root / "evidence" / (asset["accession"] + "-official-media.json"), proof)
            for sub in subs:
                sub["official_media_affiliation_status"] = "confirmed_official_source_and_project_context"
                sub["public_export"].update(scope_review_status="approved_official_source_and_text_scope",
                                             official_evidence_source_sha256=source["sha256"])
        write_json(root / ".p4-public-scope.json", {"projection_required": True,
                   "allowed_caption_subtitle_ids": sorted(s["id"] for s in exports),
                   "allowed_caption_versions": {s["id"]: s["version_sha256"] for s in exports},
                   "excluded_identity_sha256s": [], "public_selected_video_files": len(tables["assets"])})

    def fixture(self, root):
        p = {"id": "project:one", "accession": "P4-P000001", "title": "资料名甲", "aliases": [],
             "legacy_reconciliation": "pending", "related_public_project_page": "https://b.p4theater.cn/projects/fixture"}
        e = {"id": "event:one", "accession": "P4-E000001", "title": "原有资料组", "project_ids": [p["id"]],
             "event_type": "rehearsal", "date": {"value": "2022-05-15", "basis": "source_folder_label", "verification": "unverified"}}
        a = {"id": "asset:one", "accession": "P4-V000001", "title": "视频记录", "project_ids": [p["id"]],
             "event_ids": [e["id"]], "file_id": "file:one", "subtitle_ids": [], "publication_status": "not_recorded"}
        f = {"id": "file:one", "accession": "P4-F000001", "asset_id": a["id"], "extension": ".mov",
             "bytes": 1234, "sha256": "a" * 64, "camera_filename": "SCENE_001.MOV", "full_decode_status": "not_recorded"}
        body = "\ufeff1\r\n00:00:01,125 --> 00:00:02,500\r\n合成 苹果 讨论\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,250\r\n下一段话\r\n".encode()
        english = b"1\n00:00:01,125 --> 00:00:02,500\nSynthetic APPLE discussion\n"
        subs = []
        for number, raw, language, role in [(1, body, "und", "original_asr"), (2, english, "en", "machine_translation")]:
            accession = f"P4-S{number:06d}"
            digest = hashlib.sha256(raw).hexdigest()
            relative = f"captions/{accession}/{digest}/{accession}.srt"
            path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            sub = {"id": f"subtitle:{number}", "accession": accession, "asset_ids": [a["id"]],
                   "source_asset_id": a["id"], "source_file_id": f["id"], "language": language, "role": role,
                   "clock": "source_zero", "version_sha256": digest, "public_text_status": "public_machine_draft",
                   "human_listening_review": "not_recorded",
                   "public_export": {"path": relative, "sha256": digest,
                                     "scope_review_status": "approved_for_public_archive", "quality_label": "machine_draft"}}
            subs.append(sub); a["subtitle_ids"].append(sub["id"])
        subs += [{"id": "subtitle:private", "accession": "P4-S000003", "asset_ids": [a["id"]],
                  "language": "und", "role": "original_asr", "clock": "source_zero",
                  "public_text_status": "withheld_pending_review", "private_source": "unreviewed/private.srt"},
                 {"id": "subtitle:equivalent", "accession": "P4-S000004", "asset_ids": [a["id"]],
                  "language": "und", "role": "original_asr", "clock": "source_zero",
                  "equivalent_public_export_subtitle_id": "subtitle:1"}]
        private = root / "unreviewed/private.srt"; private.parent.mkdir(); private.write_bytes(b"never parse this private Apple text")
        tables = {"projects": [p], "collections": [{"id": "collection:one", "accession": "P4-C000001"}],
                  "events": [e], "assets": [a], "files": [f], "subtitles": subs, "publications": []}
        self.approve_exports(root, tables)
        for name, items in tables.items():write_json(root / "catalog" / (name + ".json"), {"items": items})
        official = {"snapshot_at": "2026-10-26T00:00:00Z", "items": [{"official_project_code": "P4C-001",
                    "title_zh": "官方作品甲", "project_url_zh": "https://a.p4theater.cn/wechat/projects/fixture",
                    "project_url_en": "https://a.p4theater.cn/en/projects/fixture", "published_temporal_coverage": "2022",
                    "current_video_catalogue_matches": [{"id": p["id"]}], "candidate_current_video_catalogue_matches": []}]}
        write_json(root / "registry/official-projects.json", official)
        return tables, body

    def table(self, root, name):
        return json.loads((root / "catalog" / (name + ".json")).read_bytes())

    def test_build_is_deterministic_lf_and_preserves_srt_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, original = self.fixture(root)
            first = reading.build(root)
            self.assertEqual((first["public_captions"], first["source_files"]), (2, 1))
            before = {p: (root / p).read_bytes() for p in first["generated_files"]}
            self.assertEqual(reading.build(root)["changed_files"], [])
            self.assertTrue(reading.check(root)["ok"])
            for body in before.values():
                self.assertNotIn(b"\r", body)
                self.assertFalse(any(line.endswith((b" ", b"\t")) for line in body.splitlines()))
            sub = tables["subtitles"][0]
            self.assertEqual((root / sub["public_export"]["path"]).read_bytes(), original)
            page = (root / "projects/P4-P000001/index.md").read_text()
            self.assertIn("# 官方作品甲", page)
            self.assertIn("资料名甲", page)
            self.assertIn("https://b.p4theater.cn/projects/fixture", page)
            self.assertIn("2022-05-15〔目录日期线索〕", page)
            self.assertIn("<details>", page)

    def test_search_returns_bound_cue_and_excludes_unexported_equivalents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, _ = self.fixture(root); reading.build(root)
            result = reading.search(root, "苹果", language="original")
            self.assertEqual(len(result), 1)
            row = result[0]
            self.assertEqual((row["cue"], row["start_ms"], row["end_ms"]), (1, 1125, 2500))
            self.assertEqual((row["clock"], row["quality_label"]), ("source_zero", "machine_draft"))
            self.assertEqual(row["subtitle_id"], "subtitle:1")
            self.assertEqual(row["source_file_id"], "file:one")
            self.assertEqual(row["source_asset_id"], "asset:one")
            self.assertEqual(row["original_source_sha256"], "a" * 64)
            self.assertEqual(len(row["official_source_urls"]), 2)
            self.assertEqual(row["subtitle_sha256"], tables["subtitles"][0]["version_sha256"])
            self.assertTrue(row["source_url"].endswith("P4-S000001.srt#L1"))
            self.assertEqual([x["subtitle_accession"] for x in reading.search(root, "apple", language="en")], ["P4-S000002"])
            self.assertEqual(reading.search(root, "private"), [])
            self.assertNotIn("never parse", (root / "reading/manifest.json").read_text())
            manifest = json.loads((root / "reading/manifest.json").read_bytes())
            self.assertFalse(any("text" in x for x in manifest["captions"]))

    def test_tampered_export_fails_before_output_replacement_or_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, _ = self.fixture(root); reading.build(root)
            index = (root / "INDEX.md").read_bytes()
            (root / tables["subtitles"][0]["public_export"]["path"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):reading.build(root)
            self.assertEqual((root / "INDEX.md").read_bytes(), index)
            self.assertFalse(reading.check(root)["ok"])
            with self.assertRaisesRegex(ValueError, "hash mismatch"):reading.search(root, "Apple")

    def test_manifest_and_generated_drift_are_detected_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); reading.build(root)
            (root / "README.md").write_text("another worker owns this")
            self.assertTrue(reading.check(root)["ok"])
            index = root / "INDEX.md"; index.write_bytes(index.read_bytes() + b"drift\n")
            self.assertFalse(reading.check(root)["ok"])
            self.assertTrue(index.read_bytes().endswith(b"drift\n"))
            manifest = root / "reading/manifest.json"; data = json.loads(manifest.read_bytes())
            data["captions"][0]["path"] = "unreviewed/private.srt"; write_json(manifest, data)
            with self.assertRaisesRegex(ValueError, "manifest.*stale"):reading.search(root, "Apple")

    def test_public_path_and_source_binding_cannot_be_substituted(self):
        for mutation in ("path", "source", "scope", "clock"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); table = self.table(root, "subtitles"); sub = table["items"][0]
                if mutation == "path":sub["public_export"]["path"] = "captions/../../unreviewed/private.srt"
                elif mutation == "source":sub["source_asset_id"] = "asset:missing"
                elif mutation == "scope":sub["public_export"]["scope_review_status"] = "pending"
                else:sub["clock"] = "edited_timeline"
                write_json(root / "catalog/subtitles.json", table)
                with self.assertRaises(ValueError):reading.build(root)
                self.assertFalse((root / "INDEX.md").exists())

    def test_same_caption_bytes_keep_distinct_approved_file_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tables, body = self.fixture(root)
            assets = self.table(root, "assets"); files = self.table(root, "files"); subs = self.table(root, "subtitles")
            second = dict(assets["items"][0], id="asset:two", accession="P4-V000002", file_id="file:two", subtitle_ids=["subtitle:5"])
            second["alternate_representation_candidate_of_asset_id"] = "asset:one"
            assets["items"].append(second)
            files["items"].append(dict(files["items"][0], id="file:two", accession="P4-F000002", asset_id="asset:two", sha256="b" * 64))
            sub = dict(subs["items"][0], id="subtitle:5", accession="P4-S000005", asset_ids=["asset:two"], source_asset_id="asset:two", source_file_id="file:two")
            sub["public_export"] = dict(sub["public_export"], path=f"captions/P4-S000005/{sub['version_sha256']}/P4-S000005.srt")
            file = root / sub["public_export"]["path"]; file.parent.mkdir(parents=True); file.write_bytes(body); subs["items"].append(sub)
            self.approve_exports(root, {"assets": assets["items"], "files": files["items"], "subtitles": subs["items"]})
            for name, data in [("assets", assets), ("files", files), ("subtitles", subs)]:write_json(root / "catalog" / (name + ".json"), data)
            reading.build(root)
            results = reading.search(root, "苹果", language="original")
            self.assertEqual({x["source_file_id"] for x in results}, {"file:one", "file:two"})
            self.assertEqual(len(results), 2)
            self.assertIn("另一文件候选版本", (root / "projects/P4-P000001/index.md").read_text())

    def test_metadata_only_copy_needs_matching_file_hash_and_does_not_duplicate_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            assets = self.table(root, "assets"); files = self.table(root, "files"); events = self.table(root, "events")
            copy = dict(assets["items"][0], id="asset:copy", accession="P4-V000002", file_id="file:copy", subtitle_ids=[],
                        canonical_byte_source_asset_id="asset:one", canonical_byte_source_file_id="file:one",
                        byte_identity_relation={"status": "full_sha256_equal"})
            assets["items"].append(copy)
            files["items"].append(dict(files["items"][0], id="file:copy", accession="P4-F000002", asset_id="asset:copy", sha256="b" * 64))
            events["items"].append(dict(events["items"][0], id="event:two", accession="P4-E000002"))
            assets["items"][0]["event_ids"].append("event:two")
            for name, data in [("assets", assets), ("files", files), ("events", events)]:write_json(root / "catalog" / (name + ".json"), data)
            reading.build(root); page = (root / "projects/P4-P000001/index.md").read_text()
            self.assertNotIn("已登记字节副本", page)
            self.assertEqual(page.count('id="p4-v000001"'), 1)
            files["items"][1]["sha256"] = "a" * 64; write_json(root / "catalog/files.json", files)
            reading.build(root); page = (root / "projects/P4-P000001/index.md").read_text()
            self.assertIn("已登记字节副本 → P4-V000001 / P4-F000001；字幕仅在获批来源记录提供", page)
            copy_row = next(line for line in page.splitlines() if "P4-V000002 / P4-F000002" in line)
            self.assertIn("暂无公开文本", copy_row)
            self.assertNotIn("](../../captions/", copy_row)
            self.assertEqual(len(reading.search(root, "苹果")), 1)

    def test_scope_rejects_unapproved_export_and_metadata_shadow(self):
        for identity in ("subtitle:private", "subtitle:equivalent"):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); reading.build(root)
                subs = self.table(root, "subtitles")
                old = (root / "INDEX.md").read_bytes()
                sub = next(s for s in subs["items"] if s["id"] == identity)
                original = subs["items"][0]
                sub.update(public_export=original["public_export"], version_sha256=original["version_sha256"],
                           public_text_status="public_machine_draft", source_asset_id="asset:one", source_file_id="file:one")
                write_json(root / "catalog/subtitles.json", subs)
                with self.assertRaisesRegex(ValueError, "scope allow list"):reading.build(root)
                with self.assertRaisesRegex(ValueError, "scope allow list"):reading.search(root, "苹果")
                self.assertEqual((root / "INDEX.md").read_bytes(), old)

    def test_scope_rejects_restored_text_outside_approved_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); reading.build(root)
            restored = root / "captions/P4-S999999/old-version/P4-S999999.srt"
            restored.parent.mkdir(parents=True); restored.write_bytes(b"withheld text")
            with self.assertRaisesRegex(ValueError, "outside the explicit public scope"):reading.build(root)
            with self.assertRaisesRegex(ValueError, "outside the explicit public scope"):reading.search(root, "withheld")

    def test_scope_rejects_same_id_new_caption_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); reading.build(root)
            subs = self.table(root, "subtitles"); sub = subs["items"][0]
            (root / sub["public_export"]["path"]).unlink()
            body = b"1\n00:00:00,000 --> 00:00:01,000\nA replacement version\n"
            digest = hashlib.sha256(body).hexdigest()
            relative = f"captions/{sub['accession']}/{digest}/{sub['accession']}.srt"
            path = root / relative; path.parent.mkdir(parents=True); path.write_bytes(body)
            sub["version_sha256"] = digest; sub["public_export"].update(path=relative, sha256=digest)
            write_json(root / "catalog/subtitles.json", subs)
            with self.assertRaisesRegex(ValueError, "version differs"):reading.build(root)
            with self.assertRaisesRegex(ValueError, "version differs"):reading.search(root, "replacement")

    def test_scope_and_exact_official_source_evidence_cannot_be_removed(self):
        for mutation in ("missing_policy", "disabled", "excluded_identity", "source_hash", "evidence_file"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root)
                policy_path = root / ".p4-public-scope.json"; policy = json.loads(policy_path.read_bytes())
                if mutation == "missing_policy":policy_path.unlink()
                elif mutation in {"disabled", "excluded_identity"}:
                    if mutation == "disabled":policy["projection_required"] = False
                    else:policy["excluded_identity_sha256s"] = [hashlib.sha256(b"asset:one").hexdigest()]
                    write_json(policy_path, policy)
                elif mutation == "source_hash":
                    files = self.table(root, "files"); files["items"][0]["sha256"] = "b" * 64
                    write_json(root / "catalog/files.json", files)
                else:(root / "evidence/P4-V000001-official-media.json").unlink()
                with self.assertRaises(ValueError):reading.build(root)
                self.assertFalse((root / "INDEX.md").exists())

    def test_existing_video_metadata_links_keep_verification_scopes_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            write_json(root / "catalog/publications.json", {"items": [{"id": "publication:one", "accession": "P4-U000001",
                       "asset_id": "asset:one", "file_id": "file:one", "verification_scope": "metadata_only",
                       "platform_readback_verification": "verified", "playback_status": "not_checked", "caption_publication_status": "not_checked"}]})
            reading.build(root)
            for relative in ["INDEX.md", "reading/index.md", "projects/P4-P000001/index.md"]:
                page = (root / relative).read_text()
                self.assertIn("publications/README.md", page)
                self.assertIn("元数据", page)
                self.assertIn("播放", page)
                self.assertIn("在线字幕", page)

    def test_feature_cannot_publish_private_caption_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            write_json(root / "content/reading-notes.json", {"projects": {"P4-P000001": {"featured": [{"subtitle_accession": "P4-S000003", "title": "private", "summary": "private", "cue_start": 1, "cue_end": 2}]}}})
            with self.assertRaisesRegex(ValueError, "public caption"):reading.build(root)
            self.assertFalse((root / "INDEX.md").exists())

    def test_cli_accepts_flag_or_positional_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); reading.build(root)
            base = [sys.executable, "-B", str(ROOT / "scripts/public_reading.py"), "search", "--root", str(root), "--limit", "1"]
            a = subprocess.run(base + ["苹果"], capture_output=True, check=True)
            b = subprocess.run(base + ["--query", "苹果"], capture_output=True, check=True)
            self.assertEqual(json.loads(a.stdout), json.loads(b.stdout))

    def test_cli_outputs_utf8_json_when_environment_forces_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); reading.build(root)
            env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/public_reading.py"),
                                     "search", "--root", str(root), "--query", "苹果", "--limit", "1"],
                                    env=env, capture_output=True, check=True)
            output = result.stdout.decode("utf-8")
            self.assertIn("苹果", output)
            self.assertEqual(json.loads(output)[0]["text"], "合成 苹果 讨论")

    def test_real_directory_generation_is_current_and_all_public_srt_are_searchable(self):
        # Read-only acceptance over the real generated files and approved exports.
        self.assertTrue(reading.check(ROOT)["ok"], reading.check(ROOT).get("errors"))
        data = reading._data(ROOT)
        allowed = data["policy"]["allowed_caption_versions"]
        self.assertEqual(data["manifest"]["public_caption_count"], len(allowed))
        self.assertEqual({c["id"]: c["sha256"] for c in data["manifest"]["captions"]}, allowed)
        self.assertEqual(data["manifest"]["source_file_count"], len({s["source_file_id"] for s in data["tables"]["subtitles"] if s["id"] in allowed}))
        self.assertEqual(len(data["tables"]["files"]), data["policy"]["public_selected_video_files"])
        for c in data["manifest"]["captions"]:
            self.assertTrue(reading._parse_srt(reading._caption_bytes(ROOT, c), c["accession"]))
        for p in data["tables"]["projects"]:
            if p.get("related_public_project_page"):
                self.assertIn(p["related_public_project_page"], (ROOT / "projects" / p["accession"] / "index.md").read_text())
        for relative in reading.check(ROOT)["checked_files"]:
            body = (ROOT / relative).read_bytes()
            self.assertNotIn(b"\r", body)
        page = (ROOT / "projects/P4-P000013/index.md").read_text()
        self.assertIn("读本之后的排演设想", page)
        self.assertIn("机器字幕依据", page)
        self.assertIn("2022-05-15〔目录日期线索〕", page)
        self.assertEqual(len(re.findall(r'<a id="p4-v\d+"', page)), len(set(re.findall(r'<a id="p4-v\d+"', page))))


if __name__ == "__main__":
    unittest.main()
