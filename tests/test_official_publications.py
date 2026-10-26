"""Separate official editions preserve raw scope and cannot revive withheld text."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from archive_catalog import write_json
import official_publications as published


BODY = b"1\n00:00:01,000 --> 00:00:02,000\nApproved edition text\n"
WITHHELD_BODY = b"1\n00:00:01,000 --> 00:00:02,000\nWithheld synthetic text\n"
MEDIA_SHA = hashlib.sha256(b"withheld synthetic source media").hexdigest()
EXCLUDED = "asset:synthetic-hard-excluded"


class OfficialPublicationTests(unittest.TestCase):
    def fixture(self, work):
        # Copy only the already public three SRTs, never source media or private state.
        policy = json.loads((ROOT / ".p4-public-scope.json").read_text())
        policy["excluded_identity_sha256s"].append(hashlib.sha256(EXCLUDED.encode()).hexdigest())
        write_json(work / ".p4-public-scope.json", policy)
        raw = json.loads((ROOT / "catalog/subtitles.json").read_text())["items"]
        keep = [s for s in raw if s["id"] in published.RAW_APPROVED_VERSIONS]
        for sub in keep:
            target = work / sub["public_export"]["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / sub["public_export"]["path"], target)
        keep.append({"id": "subtitle:synthetic-withheld", "asset_ids": ["asset:synthetic-withheld"], "source_file_id": "file:synthetic-withheld", "version_sha256": hashlib.sha256(WITHHELD_BODY).hexdigest()})
        write_json(work / "catalog/subtitles.json", {"schema_version": 1, "items": keep})
        write_json(work / "catalog/assets.json", {"schema_version": 1, "items": [{"id": "asset:synthetic-withheld", "file_id": "file:synthetic-withheld"}]})
        write_json(work / "catalog/files.json", {"schema_version": 1, "items": [{"id": "file:synthetic-withheld", "asset_id": "asset:synthetic-withheld", "sha256": MEDIA_SHA}]})
        record = self.file(work, "published/captions/official-fixture/en.srt", BODY)
        item = {"id": "official-caption:fixture", "kind": "caption", "title": "Official edition fixture", "source_url": "https://a.p4theater.cn/subtitles/fixture", "official_context_url": "https://hefa.p4theater.cn/works/fixture", "quality_label": "machine_draft", "files": [record]}
        approval = {key: copy.deepcopy(item[key]) for key in ("id", "kind", "source_url", "official_context_url", "files")}
        approval.update(decision="mirror_verified_official_public_edition", observed_at="2026-10-26T17:00:00Z", evidence=[{"url": item["official_context_url"], "http_status": 200, "sha256": hashlib.sha256(b"synthetic official context response").hexdigest(), "observed_at": "2026-10-26T17:00:00Z"}])
        self.save(work, [item], [approval])
        return item, approval

    def file(self, root, path, body):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

    def save(self, root, items, approvals):
        write_json(root / "published/manifest.json", {"schema_version": 1, "items": items})
        write_json(root / "published/approvals.json", {"schema_version": 1, "items": approvals})

    def test_valid_check_search_and_cli_keep_edition_clock_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            result = published.check(work)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["preserved_raw_caption_count"], 3)
            match = published.search(work, "approved edition")["matches"][0]
            self.assertEqual((match["cue"], match["clock"], match["start_ms"]), (1, "edition_zero", 1000))
            self.assertEqual(match["source_url"], item["source_url"])
            self.assertEqual(match["sha256"], item["files"][0]["sha256"])
            cli = subprocess.run([sys.executable, str(ROOT / "scripts/official_publications.py"), "check", "--root", str(work)], capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertTrue(json.loads(cli.stdout)["valid"])

    def test_approval_identity_version_and_hash_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            for mutate in (lambda x: x.update(id="other"), lambda x: x.update(source_url="https://b.p4theater.cn/other"), lambda x: x["files"][0].update(sha256="a" * 64), lambda x: x["files"][0].update(bytes=True)):
                changed = copy.deepcopy(approval)
                mutate(changed)
                self.save(work, [item], [changed])
                self.assertFalse(published.check(work)["valid"])
            self.save(work, [item, item], [approval])
            self.assertFalse(published.check(work)["valid"])

    def test_nested_operational_metadata_is_never_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            for extra in ({"private_path": "not-public"},
                          {"internal_evidence": {"upload_session": "not-public"}},
                          {"source_info": {"location": "/Volumes/private/source.mov"}}):
                changed = copy.deepcopy(item)
                changed.update(extra)
                self.save(work, [changed], [approval])
                self.assertFalse(published.check(work)["valid"])

    def test_reading_indexes_are_generated_from_verified_editions(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            self.assertFalse(published.build_indexes(work, check_only=True)["valid"])
            self.assertTrue(published.build_indexes(work)["valid"])
            self.assertTrue(published.build_indexes(work, check_only=True)["valid"])
            text = (work / "published/captions.md").read_text(encoding="utf-8")
            self.assertIn(item["official_context_url"], text)
            self.assertIn("1份SRT", text)
            (work / "published/captions.md").write_text("stale", encoding="utf-8")
            self.assertFalse(published.build_indexes(work, check_only=True)["valid"])
    def test_exact_official_hosts_and_timezone_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            for url in ("https://a.p4theater.cn.evil.example/", "http://a.p4theater.cn/", "https://a.p4theater.cn:443/", "https://user@a.p4theater.cn/", "https://youtube.com/watch?v=Fixture_001", "https://hefa.art/"):
                one, approved = copy.deepcopy(item), copy.deepcopy(approval)
                one["source_url"] = approved["source_url"] = url
                self.save(work, [one], [approved])
                self.assertFalse(published.check(work)["valid"], url)
            approval["observed_at"] = "2026-10-26T17:00:00"
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])

    def test_tampering_unregistered_text_traversal_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            target = work / item["files"][0]["path"]
            target.write_bytes(BODY + b"tampered")
            self.assertFalse(published.check(work)["valid"])
            target.write_bytes(BODY)
            extra = work / "published/materials/unregistered.TXT"
            extra.parent.mkdir(parents=True)
            extra.write_text("unregistered")
            self.assertFalse(published.check(work)["valid"])
            extra.unlink()
            for path in ("published/../escape.srt", "/published/captions/escape.srt", "published\\captions\\escape.srt", "published/captions/C:/escape.srt"):
                one, approved = copy.deepcopy(item), copy.deepcopy(approval)
                one["files"][0]["path"] = approved["files"][0]["path"] = path
                self.save(work, [one], [approved])
                self.assertFalse(published.check(work)["valid"])
            self.save(work, [item], [approval])
            link = work / "published/linked"
            try:
                link.symlink_to(target)
            except OSError:
                return  # Windows without symlink privilege still tests all path guards.
            self.assertFalse(published.check(work)["valid"])

    def test_withheld_caption_hash_and_excluded_identity_cannot_be_revived(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            file = self.file(work, item["files"][0]["path"], WITHHELD_BODY)
            item["files"] = approval["files"] = [file]
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            file = self.file(work, file["path"], BODY)
            item["files"] = approval["files"] = [file]
            item["source_asset_ids"] = approval["source_asset_ids"] = [EXCLUDED]
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])

    def test_withheld_source_needs_explicit_review_but_hard_exclusion_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            item["source_media_sha256"] = approval["source_media_sha256"] = MEDIA_SHA
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            approval["source_media_review"] = {"decision": "approved_official_public_edition_after_scope_review", "source_media_sha256": MEDIA_SHA, "official_context_url": item["official_context_url"], "reviewed_at": "2026-10-26T17:00:00Z"}
            self.save(work, [item], [approval])
            self.assertTrue(published.check(work)["valid"])
            files = json.loads((work / "catalog/files.json").read_text())
            files["items"].append({"id": "file:hard-excluded", "asset_id": EXCLUDED, "sha256": MEDIA_SHA})
            write_json(work / "catalog/files.json", files)
            self.assertFalse(published.check(work)["valid"])

    def test_successful_exact_official_http_evidence_is_mandatory(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            for mutate in (lambda a: a.update(evidence=[]), lambda a: a["evidence"][0].update(http_status=404), lambda a: a["evidence"][0].pop("sha256"), lambda a: a["evidence"][0].update(url="https://a.p4theater.cn/unrelated")):
                changed = copy.deepcopy(approval)
                mutate(changed)
                self.save(work, [item], [changed])
                self.assertFalse(published.check(work)["valid"])

    def test_paths_are_globally_unique_and_caption_clock_is_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            second, approved = copy.deepcopy(item), copy.deepcopy(approval)
            second["id"] = approved["id"] = "official-caption:second"
            self.save(work, [item, second], [approval, approved])
            self.assertFalse(published.check(work)["valid"])
            second["files"] = [self.file(work, "published/captions/official-fixture/EN.srt", BODY)]
            approved["files"] = copy.deepcopy(second["files"])
            self.save(work, [item, second], [approval, approved])
            self.assertFalse(published.check(work)["valid"])
            duplicate_path = work / second["files"][0]["path"]
            if not duplicate_path.samefile(work / item["files"][0]["path"]):
                duplicate_path.unlink()
            item["clock"] = "source_zero"
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            approval["clock"] = "source_zero"
            self.save(work, [item], [approval])
            self.assertTrue(published.check(work)["valid"])
            self.assertEqual(published.search(work, "edition")["matches"][0]["clock"], "source_zero")

    def test_unknown_source_ids_and_owner_or_media_hash_substitution_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            item["source_asset_ids"] = approval["source_asset_ids"] = ["asset:unknown"]
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            item["source_asset_ids"] = approval["source_asset_ids"] = ["asset:synthetic-withheld"]
            item["source_file_ids"] = approval["source_file_ids"] = ["file:synthetic-withheld"]
            item["source_media_sha256"] = approval["source_media_sha256"] = "0" * 64
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            assets = json.loads((work / "catalog/assets.json").read_text())
            assets["items"].append({"id": "asset:other", "file_id": "file:other"})
            files = json.loads((work / "catalog/files.json").read_text())
            files["items"].append({"id": "file:other", "asset_id": "asset:other", "sha256": "0" * 64})
            write_json(work / "catalog/assets.json", assets)
            write_json(work / "catalog/files.json", files)
            item["source_file_ids"] = approval["source_file_ids"] = ["file:other"]
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])

    def test_raw_scope_marker_and_three_original_versions_must_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self.fixture(work)
            policy = work / ".p4-public-scope.json"
            old = policy.read_bytes()
            policy.unlink()
            self.assertFalse(published.check(work)["valid"])
            policy.write_bytes(old)
            one = next(iter(published.RAW_APPROVED_VERSIONS))
            scope = json.loads(old)
            scope["allowed_caption_versions"][one] = "0" * 64
            write_json(policy, scope)
            self.assertFalse(published.check(work)["valid"])
            policy.write_bytes(old)
            raw = json.loads((work / "catalog/subtitles.json").read_text())["items"][0]
            (work / raw["public_export"]["path"]).write_bytes(b"changed raw caption")
            self.assertFalse(published.check(work)["valid"])

    def test_prepared_caption_retains_machine_draft_and_documents_are_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            approval["decision"] = "publish_prepared_caption_for_verified_public_work"
            self.save(work, [item], [approval])
            self.assertFalse(published.check(work)["valid"])
            item["quality_label"] = "local_machine_draft_unreviewed"
            self.save(work, [item], [approval])
            self.assertTrue(published.check(work)["valid"])
            document = {"id": "official-document:fixture", "kind": "research", "title": "Official research", "source_url": "https://b.p4theater.cn/article/fixture", "official_context_url": "https://b.p4theater.cn/projects/fixture", "files": [self.file(work, "published/materials/fixture.md", b"# Research\n\nEvidence paragraph.\n")]}
            approved = {key: copy.deepcopy(document[key]) for key in ("id", "kind", "source_url", "official_context_url", "files")}
            approved.update(decision="mirror_verified_official_public_edition", observed_at="2026-10-26T17:00:00Z", evidence=[{"url": document["source_url"], "http_status": 200, "sha256": hashlib.sha256(b"synthetic document response").hexdigest(), "observed_at": "2026-10-26T17:00:00Z"}])
            self.save(work, [item, document], [approval, approved])
            self.assertTrue(published.check(work)["valid"])
            match = published.search(work, "evidence")["matches"][0]
            self.assertEqual(match["id"], document["id"])
            self.assertNotIn("clock", match)
            wrong = copy.deepcopy(document)
            wrong["files"][0]["bytes"] += 1
            wrong_approval = copy.deepcopy(approved)
            wrong_approval["files"] = copy.deepcopy(wrong["files"])
            self.save(work, [item, wrong], [approval, wrong_approval])
            self.assertFalse(published.check(work)["valid"])
            approved["decision"] = "publish_prepared_caption_for_verified_public_work"
            self.save(work, [item, document], [approval, approved])
            self.assertFalse(published.check(work)["valid"])


if __name__ == "__main__":
    unittest.main()
