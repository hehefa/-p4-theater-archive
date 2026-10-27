"""Separate official editions preserve raw scope and cannot revive withheld text."""
import copy
import base64
import hashlib
import json
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

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
        policy = json.loads((ROOT / ".p4-public-scope.json").read_text(encoding="utf-8"))
        policy["excluded_identity_sha256s"].append(hashlib.sha256(EXCLUDED.encode()).hexdigest())
        write_json(work / ".p4-public-scope.json", policy)
        raw = json.loads((ROOT / "catalog/subtitles.json").read_text(encoding="utf-8"))["items"]
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
            cli = subprocess.run([sys.executable, str(ROOT / "scripts/official_publications.py"), "check", "--root", str(work)], capture_output=True, text=True, encoding="utf-8")
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
            self.assertIn("1条字幕记录", text)
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

    def test_only_evidenced_p4_youtube_caption_identity_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            item, approval = self.fixture(work)
            video = "URZf2R5N_jw"
            watch = "https://www.youtube.com/watch?v=" + video
            response = {"title": "Synthetic public test", "type": "video", "provider_name": "YouTube", "author_name": "P4 Theater | He Fa", "author_url": "https://www.youtube.com/@p4theater510", "html": '<iframe src="https://www.youtube.com/embed/' + video + '?feature=oembed"></iframe>'}
            raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
            proof = {"video_id": video, "channel_id": "UCbZU8SmY-hU_BhTlQHwctkg", "author_url": "https://www.youtube.com/@p4theater510", "oembed_url": "https://www.youtube.com/oembed?url=" + watch + "&format=json", "oembed_http_status": 200, "oembed_observed_at": "2026-10-27T02:14:45Z", "oembed_sha256": hashlib.sha256(raw).hexdigest(), "oembed_response_body_base64": base64.b64encode(raw).decode("ascii"), "watch_response_sha256": hashlib.sha256(b"synthetic watch response").hexdigest(), "observed_at": "2026-10-27T02:14:44Z"}
            item.update(youtube_id=video, source_url=watch, official_context_url=watch, youtube_source_identity=proof)
            approval.update(source_url=watch, official_context_url=watch, youtube_source_identity=copy.deepcopy(proof), evidence=[{"url": watch, "http_status": 200, "sha256": proof["watch_response_sha256"], "observed_at": proof["observed_at"]}])
            self.save(work, [item], [approval])
            result = published.check(work)
            self.assertTrue(result["valid"], result)
            self.assertTrue(published.build_indexes(work)["valid"])
            self.assertIn(watch, (work / "published/captions.md").read_text(encoding="utf-8"))

            def changed_identity(key, value):
                i, a = copy.deepcopy(item), copy.deepcopy(approval)
                i["youtube_source_identity"][key] = value
                a["youtube_source_identity"][key] = value
                return i, a

            mutations = [
                lambda: changed_identity("video_id", "different11"),
                lambda: changed_identity("channel_id", "UCdifferent"),
                lambda: changed_identity("author_url", "https://www.youtube.com/@other"),
                lambda: changed_identity("oembed_url", "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=different11&format=json"),
                lambda: changed_identity("oembed_http_status", 404),
                lambda: changed_identity("oembed_observed_at", "2026-10-27T02:14:45"),
                lambda: changed_identity("oembed_sha256", "0" * 64),
                lambda: changed_identity("oembed_response_body_base64", "!not-base64!"),
                lambda: changed_identity("watch_response_sha256", "0" * 64),
                lambda: changed_identity("observed_at", "2026-10-27T03:14:44Z"),
            ]
            for mutate in mutations:
                i, a = mutate()
                self.save(work, [i], [a])
                self.assertFalse(published.check(work)["valid"], (i, a))

            for mutate_response in (
                lambda r: r.update(author_url="https://www.youtube.com/@other"),
                lambda r: r.update(author_name="Another channel"),
                lambda r: r.update(html='<iframe src="https://www.youtube.com/embed/different11?feature=oembed"></iframe>'),
            ):
                changed = copy.deepcopy(response); mutate_response(changed)
                body = json.dumps(changed, ensure_ascii=False).encode("utf-8")
                i, a = changed_identity("oembed_response_body_base64", base64.b64encode(body).decode("ascii"))
                i["youtube_source_identity"]["oembed_sha256"] = a["youtube_source_identity"]["oembed_sha256"] = hashlib.sha256(body).hexdigest()
                self.save(work, [i], [a])
                self.assertFalse(published.check(work)["valid"])
            i, a = copy.deepcopy(item), copy.deepcopy(approval)
            a["youtube_source_identity"]["channel_id"] = "UCdifferent"
            self.save(work, [i], [a]); self.assertFalse(published.check(work)["valid"])
            i, a = copy.deepcopy(item), copy.deepcopy(approval)
            i["kind"] = a["kind"] = "document"
            self.save(work, [i], [a]); self.assertFalse(published.check(work)["valid"])
            i, a = copy.deepcopy(item), copy.deepcopy(approval)
            i["source_url"] = a["source_url"] = watch + "&extra=1"
            self.save(work, [i], [a]); self.assertFalse(published.check(work)["valid"])

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
            files = json.loads((work / "catalog/files.json").read_text(encoding="utf-8"))
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
            assets = json.loads((work / "catalog/assets.json").read_text(encoding="utf-8"))
            assets["items"].append({"id": "asset:other", "file_id": "file:other"})
            files = json.loads((work / "catalog/files.json").read_text(encoding="utf-8"))
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
            raw = json.loads((work / "catalog/subtitles.json").read_text(encoding="utf-8"))["items"][0]
            (work / raw["public_export"]["path"]).write_bytes(b"changed raw caption")
            self.assertFalse(published.check(work)["valid"])

    def image_fixture(self, work):
        caption, old_approval = self.fixture(work)
        body = b"\x89PNG\r\n\x1a\nsynthetic image bytes"
        preview = self.file(work, "published/images/fixture/preview.png", body)
        preview.update(storage="git", role="repo_preview", mime="image/png", width=1, height=1)
        members = [{"path": "published/packaged-images/fixture-bundle/tiles/" + str(i) + ".jpg", "storage": "release", "bundle_id": "fixture-bundle", "archive_path": "tiles/" + str(i) + ".jpg", "download_url": "https://github.com/example/archive/releases/download/v1/fixture.zip", "asset_id": 123, "sha256": hashlib.sha256(("tile " + str(i)).encode()).hexdigest(), "bytes": 6, "mime": "image/jpeg", "width": 1, "height": 1, "role": "tile"} for i in (1, 2)]
        image = {"id": "official-image:fixture", "original_id": "hf-img-fixture", "kind": "image", "title": "Reference portrait fixture", "source_url": "https://hefa.p4theater.cn/images/fixture", "official_context_url": caption["official_context_url"], "image_classification": "reference", "keywords": ["portrait"], "previewURL": preview["path"], "files": [preview, *members]}
        approval = {key: copy.deepcopy(image[key]) for key in ("id", "kind", "source_url", "official_context_url", "files")}
        approval.update(decision="mirror_verified_official_public_edition", observed_at=old_approval["observed_at"], evidence=copy.deepcopy(old_approval["evidence"]))
        self.save(work, [caption, image], [old_approval, approval])
        return caption, old_approval, image, approval

    def fake_image_store(self, work):
        store = mock.Mock()
        def identity(item):
            if item.get("original_id") == "hf-img-held":
                raise ValueError("Excluded image identity")
        def file(record):
            if record.get("storage") == "release":
                if record.get("bundle_id") != "fixture-bundle" or record.get("asset_id") != 123:
                    raise ValueError("Unverified bundle/member receipt")
                return None
            return (work / record["path"]).read_bytes()
        store.validate_identity.side_effect = identity
        store.validate_file.side_effect = file
        return store

    def test_images_use_verified_storage_search_once_and_index_real_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            caption, old_approval, image, approval = self.image_fixture(work)
            tile_download = "https://github.com/example/archive/releases/download/v1/separate-tiles.zip"
            image["files"][2]["download_url"] = tile_download
            approval["files"] = copy.deepcopy(image["files"])
            self.save(work, [caption, image], [old_approval, approval])
            store = self.fake_image_store(work)
            with mock.patch.object(published, "_image_store", return_value=store):
                result = published.check(work)
                self.assertTrue(result["valid"], result)
                self.assertEqual((result["image_item_count"], result["image_access_file_count"], result["image_tile_file_count"]), (1, 1, 2))
                self.assertEqual(store.validate_identity.call_count, 1)
                self.assertEqual(store.validate_file.call_count, 3)
                matches = published.search(work, "portrait")["matches"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["tile_count"], 2)
                self.assertEqual(matches[0]["id"], image["id"])
                self.assertTrue(published.build_indexes(work)["valid"])
                self.assertTrue(published.build_indexes(work, check_only=True)["valid"])
                images = (work / "published/images.md").read_text(encoding="utf-8")
                self.assertIn("hf-img-fixture", images)
                self.assertIn("reference", images)
                self.assertIn(tile_download, images)
                self.assertNotIn("![]", images)
                self.assertNotIn("](packaged-images/", images)
                self.assertNotIn(image["title"], (work / "published/materials.md").read_text(encoding="utf-8"))
                # A Release-only image has an actual package URL, never a fake repo member link.
                image["files"] = image["files"][1:]
                image.pop("previewURL")
                approval["files"] = copy.deepcopy(image["files"])
                (work / "published/images/fixture/preview.png").unlink()
                self.save(work, [caption, image], [old_approval, approval])
                published.build_indexes(work)
                self.assertIn(image["files"][0]["download_url"], (work / "published/images.md").read_text(encoding="utf-8"))

    def test_image_fake_bundle_hold_identity_and_unregistered_preview_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            caption, old_approval, image, approval = self.image_fixture(work)
            store = self.fake_image_store(work)
            with mock.patch.object(published, "_image_store", return_value=store):
                image["files"][1]["bundle_id"] = "fabricated-bundle"
                approval["files"] = copy.deepcopy(image["files"])
                self.save(work, [caption, image], [old_approval, approval])
                self.assertFalse(published.check(work)["valid"])
                image["files"][1]["bundle_id"] = "fixture-bundle"
                approval["files"] = copy.deepcopy(image["files"])
                image["original_id"] = "hf-img-held"
                self.save(work, [caption, image], [old_approval, approval])
                self.assertFalse(published.check(work)["valid"])
                image["original_id"] = "hf-img-fixture"
                image["previewURL"] = "https://hefa.p4theater.cn/unregistered-image.png"
                self.save(work, [caption, image], [old_approval, approval])
                self.assertFalse(published.check(work)["valid"])

    def test_binary_documents_and_structured_exports_delegate_signatures_and_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            caption, old_approval = self.fixture(work)
            docx = io.BytesIO()
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
            files = [self.file(work, "published/materials/doc/file.pdf", b"%PDF-1.4\nsynthetic document\n%%EOF"), self.file(work, "published/materials/doc/file.docx", docx.getvalue()), self.file(work, "published/materials/doc/data.json", b'{"public_record":"fixture"}'), self.file(work, "published/materials/doc/data.csv", b"id,title\n1,public\n")]
            document = {"id": "official-document:binary", "kind": "document", "title": "Public document downloads", "source_url": caption["source_url"], "official_context_url": caption["official_context_url"], "files": files}
            approved = {key: copy.deepcopy(document[key]) for key in ("id", "kind", "source_url", "official_context_url", "files")}
            approved.update(decision="mirror_verified_official_public_edition", observed_at=old_approval["observed_at"], evidence=copy.deepcopy(old_approval["evidence"]))
            self.save(work, [caption, document], [old_approval, approved])
            def signature(record, body):
                if record["path"].endswith(".pdf") and not body.startswith(b"%PDF-"):
                    raise ValueError("Invalid public PDF signature")
                if record["path"].endswith(".docx") and not zipfile.is_zipfile(io.BytesIO(body)):
                    raise ValueError("Invalid public DOCX signature")
            with mock.patch.object(published, "_document_bytes", side_effect=signature) as helper:
                result = published.check(work)
                self.assertTrue(result["valid"], result)
                self.assertEqual(helper.call_count, 2)
                for index, body in ((0, b"<!doctype html>fake PDF"), (2, b"not valid JSON"), (3, b'id,title\n1,"unterminated')):
                    old = copy.deepcopy(files[index])
                    files[index] = self.file(work, old["path"], body)
                    approved["files"] = copy.deepcopy(files)
                    self.save(work, [caption, document], [old_approval, approved])
                    self.assertFalse(published.check(work)["valid"])
                    files[index] = old
                    # Rewrite original bytes so each next subcase starts from valid sources.
                    originals = [b"%PDF-1.4\nsynthetic document\n%%EOF", docx.getvalue(), b'{"public_record":"fixture"}', b"id,title\n1,public\n"]
                    (work / old["path"]).write_bytes(originals[index])
                    approved["files"] = copy.deepcopy(files)

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


class PublicMetadataPathTests(unittest.TestCase):
    def test_repost_requires_same_wechat_id_and_existing_original_text(self):
        base = "https://mp.weixin.qq.com/s?__biz=abc&mid=12&idx=1&sn=1234"
        original_url = "https://mp.weixin.qq.com/s?__biz=xyz&mid=34&idx=1&sn=5678"
        source_id = {"__biz": "abc", "mid": "12", "idx": "1", "sn": "1234"}
        target_id = {"__biz": "xyz", "mid": "34", "idx": "1", "sn": "5678"}
        path = "published/materials/original.md"
        sha = hashlib.sha256(b"saved original").hexdigest()
        proof = {"kind": "forward_card", "http_status": 200, "response_sha256": "a" * 64,
                 "observed_at": "2026-10-26T17:00:00Z", "short_url": "https://mp.weixin.qq.com/s/short-proof",
                 "wrapper_identity": source_id, "original_identity": target_id,
                 "original_item_id": "article:original", "original_file_path": path, "original_file_sha256": sha}
        item = {"subtype": "wechat_article", "content_status": "verified_forwarded_original", "source_original_url": base, "wechat_source_resolution": proof}
        target = {"subtype": "wechat_article", "content_status": "captured_text", "source_original_url": original_url, "files": [{"path": path, "sha256": sha}]}
        approval = {"wechat_source_resolution": copy.deepcopy(proof)}
        published._wechat_source_resolution(item, approval, {"article:original": target})
        for change in (lambda p: p["wrapper_identity"].update(mid="99"), lambda p: p["original_identity"].update(sn="wrong"), lambda p: p.update(original_file_sha256="b" * 64), lambda p: p.update(original_item_id="article:missing")):
            altered = copy.deepcopy(proof); change(altered)
            with self.assertRaises(ValueError):
                published._wechat_source_resolution({**item, "wechat_source_resolution": altered}, {"wechat_source_resolution": altered}, {"article:original": target})

    def test_public_nested_home_folder_is_not_a_local_home_path(self):
        published._public_metadata({"path": "published/packaged-images/bundle/media/home/hero.webp"})
        published._public_metadata({"archive_path": "media/home/hero.webp"})
        published._public_metadata({"source_url": "https://hefa.p4theater.cn/media/home/hero.webp"})
        for value in ("/home/user/private.txt", "source: /home/user/private.txt", "/Users/test/x", "/Volumes/archive/x"):
            with self.assertRaises(ValueError):
                published._public_metadata({"source": value})

    def test_offline_wechat_snapshot_does_not_claim_http_readback(self):
        original = "https://mp.weixin.qq.com/s?__biz=abc&mid=12&idx=1&sn=1234"
        proof = {"kind": "video_page", "evidence_kind": "local_archive_snapshot", "observed_at": "2026-10-26T17:00:00Z", "response_sha256": "a" * 64, "observed_url": original, "wrapper_identity": {"__biz": "abc", "mid": "12", "idx": "1", "sn": "1234"}, "video_id": "export/" + "a" * 32}
        item = {"subtype": "wechat_article", "content_status": "verified_video_post", "source_original_url": original, "wechat_source_resolution": proof}
        published._wechat_source_resolution(item, {"wechat_source_resolution": proof}, {})
        invalid = {**proof, "http_status": 200}
        with self.assertRaises(ValueError):
            published._wechat_source_resolution({**item, "wechat_source_resolution": invalid}, {"wechat_source_resolution": invalid}, {})


if __name__ == "__main__":
    unittest.main()
