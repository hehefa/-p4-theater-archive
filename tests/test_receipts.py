import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_receipts", ROOT / "scripts/publication_receipts.py")
receipts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipts)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.catalog_dir = self.work / "catalog"
        self.catalog_dir.mkdir()
        self.sha = "a" * 64
        self.cat = {
            "projects": {"project:one": {"id": "project:one"}},
            "assets": {"asset:one": {"id": "asset:one", "file_id": "file:one", "project_ids": ["project:one"]}},
            "files": {"file:one": {"id": "file:one", "asset_id": "asset:one", "sha256": self.sha, "bytes": 123}},
            "checksums": {"checksum:one": {"id": "checksum:one", "file_id": "file:one", "algorithm": "sha256", "value": self.sha}},
        }
        for name, rows in self.cat.items():
            receipts.write(self.catalog_dir / (name + ".json"), {"schema_version": 1, "items": list(rows.values())})
        self.document = {
            "channelId": "UCp_LyzepcrZX6Cgzv2Qit4w", "updatedAt": "2026-10-22T13:24:22+00:00",
            "sourceRoot": "/Users/private-person/credentials", "session": "PRIVATE_SESSION_VALUE",
            "assets": [{"state": "public", "videoId": "yc3J_OmAZj8", "sha256": self.sha, "bytes": 123,
                        "sourceFile": "/Volumes/PRIVATE/camera.mov", "rawReply": "PRIVATE_RESPONSE",
                        "operator_reported": "verified", "uploadedAt": "2026-10-22T12:00:00Z"}],
        }
        self.path = self.work / "receipt.json"

    def preview(self, adapter="vortex", project_ids=("project:one",)):
        receipts.write(self.path, self.document)
        return receipts.preview(self.path, adapter, "fixture-source", self.cat, project_ids)

    def mappings(self, candidates):
        return {"schema_version": 1, "kind": "publication_mappings", "items": [{
            "candidate_id": candidates["items"][0]["id"], "asset_id": "asset:one", "file_id": "file:one",
            "sha256": self.sha, "bytes": 123, "identity_confirmed": True,
        }]}

    def test_same_version_is_still_only_a_candidate_and_old_public_is_not_current_verification(self):
        candidates, sources = self.preview()
        row = candidates["items"][0]
        self.assertEqual(row["catalogue_match"]["same_version_file_candidates"], ["file:one"])
        self.assertIsNone(row["catalogue_match"]["asset_id"])
        self.assertEqual(row["historical_claim"]["state"], "public")
        self.assertEqual(row["historical_platform_observation"]["status"], "not_recorded")
        self.assertEqual(row["current_platform_observation"], {"status": "not_performed", "observed_at": None})
        self.assertEqual(receipts.check(candidates, sources, self.cat)["candidate_count"], 1)

    def test_unmatched_receipt_stays_project_related_candidate(self):
        self.document["assets"][0]["sha256"] = "b" * 64
        candidates, sources = self.preview()
        row = candidates["items"][0]
        self.assertEqual(row["project_ids"], ["project:one"])
        self.assertEqual(row["catalogue_match"]["same_version_file_candidates"], [])
        self.assertIn("no_catalogue_file_with_same_sha256_and_bytes", row["catalogue_match"]["reason_codes"])
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_bindings(candidates, sources, self.mappings(candidates), self.cat)

    def test_confirmed_identical_record_exports_without_mutating_catalog_or_candidate(self):
        candidates, sources = self.preview()
        before = copy.deepcopy((self.cat, candidates))
        result = receipts.export_bindings(candidates, sources, self.mappings(candidates), self.cat)
        self.assertEqual(result["items"][0]["file_id"], "file:one")
        self.assertEqual(result["items"][0]["relationship"], "same_file_version")
        self.assertEqual(result["items"][0]["current_platform_observation"]["status"], "not_performed")
        self.assertEqual((self.cat, candidates), before)

    def test_wrong_sha_size_id_and_asset_relationship_are_rejected(self):
        candidates, sources = self.preview()
        for key, value in [("sha256", "b" * 64), ("bytes", 124), ("file_id", "file:wrong"),
                           ("asset_id", "asset:wrong"), ("candidate_id", "publication-candidate:wrong"),
                           ("identity_confirmed", False)]:
            with self.subTest(key=key):
                mapping = self.mappings(candidates)
                mapping["items"][0][key] = value
                with self.assertRaises(receipts.ReceiptError):
                    receipts.export_bindings(candidates, sources, mapping, self.cat)
        bad_catalog = copy.deepcopy(self.cat)
        bad_catalog["files"]["file:one"]["asset_id"] = "asset:another"
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_bindings(candidates, sources, self.mappings(candidates), bad_catalog)

    def test_missing_recorded_checksum_cannot_establish_binding(self):
        candidates, sources = self.preview()
        self.cat["checksums"] = {}
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_bindings(candidates, sources, self.mappings(candidates), self.cat)

    def test_private_fields_and_operator_verified_do_not_leak(self):
        candidates, sources = self.preview()
        public = receipts.encoded({"candidates": candidates, "sources": sources}).decode()
        for private in ["/Volumes/", "/Users/", "PRIVATE", "session", "rawReply", "sourceFile", "operator_reported", '"verified"']:
            self.assertNotIn(private, public)
        for target in [candidates["items"][0], candidates["items"][0]["historical_claim"], sources["items"][0]]:
            target["private_path"] = "/Users/private"
            with self.assertRaises(receipts.ReceiptError):
                receipts.check(candidates, sources, self.cat)
            del target["private_path"]

    def test_offline_table_cannot_be_edited_to_claim_current_readback(self):
        candidates, sources = self.preview()
        candidates["items"][0]["current_platform_observation"] = {"status": "verified", "observed_at": "2026-10-26T00:00:00Z"}
        with self.assertRaises(receipts.ReceiptError):
            receipts.check(candidates, sources, self.cat)

    def test_human_surrender_missing_sha_and_queued_are_not_bindable(self):
        self.document = {"channel_id": "UCp_LyzepcrZX6Cgzv2Qit4w", "updated_at": 1791000125,
                         "status": "blocked", "assets": [{"status": "public_processed", "video_id": "7afucA3KuNo", "bytes": 123,
                         "remote_observed_at": 1791070505, "privacy_status": "public", "metadata_verified": True},
                         {"status": "browser_queued", "bytes": 456, "source": "/PRIVATE/DJI.MP4"}]}
        candidates, sources = self.preview("human-surrender", project_ids=())
        self.assertEqual(sources["items"][0]["batch_state"], "blocked")
        self.assertEqual(len(candidates["items"]), 2)
        uploaded = next(r for r in candidates["items"] if r["video_id"])
        self.assertEqual(uploaded["historical_platform_observation"]["status"], "receipt_reports_platform_observation")
        self.assertIsNone(uploaded["receipt_file_version"]["sha256"])
        self.assertEqual(uploaded["project_ids"], [])
        mapping = self.mappings(candidates)
        mapping["items"][0]["candidate_id"] = uploaded["id"]
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_bindings(candidates, sources, mapping, self.cat)

    def test_fa_keeps_related_artist_work_unassigned_and_readback_historical(self):
        row = self.document["assets"][0]
        row.update(state="public_processed_verified", metadataReadback={"raw": "PRIVATE"}, actualPrivacyStatus="public",
                   publicWatchPlaybackVerified={"raw": "PRIVATE"}, updatedAt="2026-10-24T17:07:38Z")
        candidates, unused = self.preview("fa", project_ids=())
        row = candidates["items"][0]
        self.assertEqual(row["project_ids"], [])
        self.assertEqual(row["historical_platform_observation"]["observed_at"], "2026-10-24T17:07:38Z")
        self.assertEqual(row["current_platform_observation"]["status"], "not_performed")

    def test_nonpublic_records_omitted_and_invalid_public_video_rejected(self):
        self.document["assets"][0].update(state="private")
        candidates, sources = self.preview()
        self.assertEqual(candidates["items"], [])
        self.assertEqual(sources["items"][0]["nonpublic_records_omitted"], 1)
        self.document["assets"][0].update(state="public", videoId="invalid-ID")
        with self.assertRaises(receipts.ReceiptError):
            self.preview()

    def test_timezone_missing_upload_claim_is_preserved_without_guessing_utc(self):
        self.document["assets"][0]["uploadedAt"] = "2026-10-22T19:04:10"
        candidates, unused = self.preview()
        claim = candidates["items"][0]["historical_claim"]
        self.assertEqual(claim["reported_upload_time"], "2026-10-22T19:04:10")
        self.assertEqual(claim["recorded_at"], "2026-10-22T13:24:22Z")

    def test_queued_video_id_does_not_become_a_public_url(self):
        self.document["assets"][0]["state"] = "browser_queued"
        candidates, unused = self.preview()
        self.assertIsNone(candidates["items"][0]["video_id"])
        self.assertIsNone(candidates["items"][0]["public_url"])

    def test_source_media_are_never_opened(self):
        self.document["assets"][0]["sourceFile"] = str(self.work / "missing-original.mov")
        candidates, unused = self.preview()
        self.assertEqual(candidates["items"][0]["receipt_file_version"]["sha256"], self.sha)

    def test_source_cannot_allocate_missing_project_or_leak_ref_path(self):
        with self.assertRaises(receipts.ReceiptError):
            self.preview(project_ids=("project:not-in-catalog",))
        receipts.write(self.path, self.document)
        with self.assertRaises(receipts.ReceiptError):
            receipts.preview(self.path, "fa", "fixture-source", self.cat, (), [{"namespace": "local", "id": "/Users/private"}])

    def test_cli_rerun_and_append_are_byte_identical_utf8_lf(self):
        receipts.write(self.path, self.document)
        output, sources = self.work / "candidates.json", self.work / "sources.json"
        command = [sys.executable, str(ROOT / "scripts/publication_receipts.py"), "preview", "--receipt", str(self.path),
                   "--adapter", "vortex", "--source-id", "fixture-source", "--catalog-dir", str(self.catalog_dir),
                   "--output", str(output), "--sources-output", str(sources), "--project-id", "project:one"]
        first = subprocess.run(command, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = output.read_bytes(), sources.read_bytes()
        for extra in [[], ["--append"]]:
            result = subprocess.run(command + extra, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output.read_bytes(), sources.read_bytes()), before)
        self.assertNotIn(b"\r", before[0] + before[1])
        self.assertTrue(before[0].endswith(b"\n"))

    def test_outputs_cannot_overwrite_inputs_or_entity_catalogs(self):
        for output in [self.catalog_dir / "files.json", self.path, self.catalog_dir.parent / ".p4-public-scope.json"]:
            with self.subTest(output=output.name), self.assertRaises(receipts.ReceiptError):
                receipts.guard_outputs([output], [self.path], self.catalog_dir)
        with self.assertRaises(receipts.ReceiptError):
            receipts.guard_outputs([self.work / "out.json", self.work / "out.json"], [], self.catalog_dir)

    def test_failed_cli_export_keeps_previous_output_intact(self):
        candidates, sources = self.preview()
        c, s, mapping, out = [self.work / name for name in ["candidates.json", "sources.json", "mapping.json", "bindings.json"]]
        receipts.write(c, candidates)
        receipts.write(s, sources)
        wrong = self.mappings(candidates)
        wrong["items"][0]["sha256"] = "b" * 64
        receipts.write(mapping, wrong)
        out.write_bytes(b"existing output\n")
        command = [sys.executable, str(ROOT / "scripts/publication_receipts.py"), "export", "--candidates", str(c),
                   "--sources", str(s), "--mappings", str(mapping), "--output", str(out), "--catalog-dir", str(self.catalog_dir)]
        result = subprocess.run(command, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(out.read_bytes(), b"existing output\n")
        self.assertNotIn(str(self.work).encode(), result.stderr)

    def enable_scope(self):
        self.cat["scope_policy"] = {"projection_required": True, "excluded_identity_sha256s": [], "decision_source": "fixture_official_source_and_no_family"}
        self.cat["publications"] = {}

    def official_source(self):
        self.cat["assets"]["asset:one"].update(
            official_media_affiliation_status="confirmed_official_source_and_project_context",
            official_media_evidence={"decision": "confirmed_project_media", "scope": "project_media_provenance_only",
                                     "asset_id": "asset:one", "file_id": "file:one", "source_sha256": self.sha, "source_bytes": 123,
                                     "evidence": [{"url": "https://a.p4theater.cn/videos/fixture", "http_status": 200},
                                                  {"url": "https://b.p4theater.cn/questions/fixture", "http_status": 200}]})

    def existing_publication(self):
        self.cat["publications"] = {"publication:existing": {"id": "publication:existing", "asset_id": "asset:one",
            "file_id": "file:one", "youtube_video_id": "yc3J_OmAZj8", "visibility": "public", "source_sha256": self.sha,
            "platform_readback_verification": "verified", "verification_scope": "metadata_only"}}

    def test_scope_unproven_history_is_source_aggregate_only(self):
        self.enable_scope()
        candidates, sources = self.preview()
        self.assertEqual(candidates["items"], [])
        self.assertEqual(sources["items"][0]["public_projection_count"], 0)
        self.assertEqual(sources["items"][0]["public_scope"]["withheld_record_count"], 1)
        text = receipts.encoded({"candidates": candidates, "sources": sources}).decode()
        self.assertNotIn("yc3J_OmAZj8", text)
        self.assertNotIn(self.sha, text)

    def test_scope_known_video_is_source_related_and_cannot_reintroduce_unknown_sha(self):
        self.enable_scope()
        self.existing_publication()
        self.document["assets"][0]["sha256"] = "b" * 64
        candidates, sources = self.preview()
        row = candidates["items"][0]
        self.assertEqual(row["public_scope"]["related_publication_ids"], ["publication:existing"])
        self.assertEqual(row["public_scope"]["basis"], "existing_publication_metadata_only")
        self.assertIsNone(row["receipt_file_version"]["sha256"])
        self.assertIsNone(row["receipt_file_version"]["bytes"])
        self.assertEqual(row["historical_platform_observation"]["status"], "not_recorded")
        self.assertEqual(row["current_platform_observation"]["status"], "not_performed")
        with self.assertRaises(receipts.ReceiptError):
            receipts.export_bindings(candidates, sources, self.mappings(candidates), self.cat)

    def test_scope_official_exact_source_allows_candidate_but_does_not_claim_playback(self):
        self.enable_scope()
        self.official_source()
        candidates, sources = self.preview()
        row = candidates["items"][0]
        self.assertEqual(row["public_scope"]["basis"], "official_source_version_evidence")
        self.assertIsNone(row["catalogue_match"]["file_id"])
        result = receipts.export_bindings(candidates, sources, self.mappings(candidates), self.cat)
        self.assertEqual(result["items"][0]["current_platform_observation"]["status"], "not_performed")

    def test_scope_excluded_identity_wins_over_official_or_existing_public_metadata(self):
        self.enable_scope()
        self.official_source()
        self.existing_publication()
        for identity in ["file:one", "asset:one"]:
            with self.subTest(identity=identity):
                self.cat["scope_policy"]["excluded_identity_sha256s"] = [hashlib.sha256(identity.encode()).hexdigest()]
                candidates, sources = self.preview()
                self.assertEqual(candidates["items"], [])
                self.assertEqual(sources["items"][0]["public_scope"]["withheld_record_count"], 1)

    def test_scope_policy_change_invalidates_prior_public_projection(self):
        candidates, sources = self.preview()
        self.enable_scope()
        with self.assertRaises(receipts.ReceiptError):
            receipts.check(candidates, sources, self.cat)

    def test_scope_missing_policy_file_is_rejected_for_a_projected_catalog(self):
        assets = {"items": [{**self.cat["assets"]["asset:one"], "official_media_affiliation_status": "official_media_affiliation_pending"}]}
        receipts.write(self.catalog_dir / "assets.json", assets)
        with self.assertRaises(receipts.ReceiptError):
            receipts.catalogue(self.catalog_dir)

    def test_scope_policy_digest_is_identical_for_lf_and_crlf_json(self):
        self.enable_scope()
        receipts.write(self.catalog_dir / "publications.json", {"items": []})
        p = self.catalog_dir.parent / ".p4-public-scope.json"
        raw = receipts.encoded(self.cat["scope_policy"])
        p.write_bytes(raw)
        first = receipts.scope_header(receipts.catalogue(self.catalog_dir), 1)
        p.write_bytes(raw.replace(b"\n", b"\r\n"))
        second = receipts.scope_header(receipts.catalogue(self.catalog_dir), 1)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
