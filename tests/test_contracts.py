"""Synthetic catalogue contracts and batch failure boundaries; no real media."""
import contextlib
import copy
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import archive_catalog as catalog
import catalog_transaction as transaction
import merge_official_registry as official
import merge_caption_release as captions
import project_public_scope as projection
import merge_youtube_receipts as studio
from project_public_scope import project
import test_public_scope as scope_tests


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and p.name != "library_verification.lock"}


def import_fixture(work):
    public, state = work / "public", work / "private"
    sources = [work / "source-a", work / "source-b"]
    for folder in sources:
        folder.mkdir()
        (folder / "IMG_0001.MOV").write_bytes(b"synthetic byte-identical fixture")
    def refresh():
        audit = {"generated_at": "2026-01-01T00:00:00Z", "projects": []}
        cache = {"schema_version": 1, "files": {}}
        for folder, label in zip(sources, ("3P小组 2020", "动物农场")):
            video = folder / "IMG_0001.MOV"
            stat = video.stat()
            audit["projects"].append({"directory_label": label, "project_root": str(folder), "videos": [{"path": str(video), "relative_path": video.name, "bytes": stat.st_size, "extension": video.suffix, "derived_candidate": False, "project_media_copy_candidate": False}]})
            cache["files"][catalog.pathkey(video)] = {"path": str(video), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "verified_at": "2026-01-01T00:00:00Z"}
        catalog.write_json(work / "audit.json", audit)
        catalog.write_json(state / "hash_results.json", cache)
    args = SimpleNamespace(output=str(public), local_state=str(state), audit=str(work / "audit.json"), quality=None, crosswalk=None)
    def run():
        with contextlib.redirect_stdout(io.StringIO()):
            catalog.import_catalog(args)
    refresh()
    run()
    return public, state, sources, refresh, run


class ContractTests(unittest.TestCase):
    def test_wrong_types_and_cross_asset_current_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, state, sources, refresh, run = import_fixture(Path(tmp))
            cases = (
                ("files", lambda t: t.update(schema_version=999)),
                ("files", lambda t: t.update(schema_version=True)),
                ("files", lambda t: t["items"][0].update(bytes=-1)),
                ("files", lambda t: t["items"][0].update(bytes=True)),
                ("projects", lambda t: t["items"][0].update(title="")),
                ("assets", lambda t: t["items"][0].update(project_ids="project:string-is-not-an-array")),
                ("assets", lambda t: t["items"][0].update(file_id=t["items"][1]["file_id"])),
                ("checksums", lambda t: t["items"][0].update(value="0" * 64)),
            )
            for name, mutate in cases:
                with self.subTest(table=name, mutate=mutate):
                    path = public / "catalog" / (name + ".json")
                    old = catalog.read_json(path)
                    changed = copy.deepcopy(old)
                    mutate(changed)
                    catalog.write_json(path, changed)
                    result = catalog.validate(public)
                    self.assertFalse(result["valid"], result)
                    catalog.write_json(path, old)

    def test_changed_copy_invalidates_relation_but_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, state, sources, refresh, run = import_fixture(Path(tmp))
            path = public / "catalog/assets.json"
            table = catalog.read_json(path)
            primary, copied = table["items"]
            old_file = copied["file_id"]
            primary["byte_copy_asset_ids"] = [copied["id"]]
            copied.update(human_review="passed", canonical_byte_source_asset_id=primary["id"], canonical_byte_source_file_id=primary["file_id"], byte_identity_relation={"status": "full_sha256_equal"}, content_scope_review={"status": "approved_for_public_archive", "source_file_id": old_file}, project_assignment_review={"status": "source_context_supported"})
            catalog.write_json(path, table)
            self.assertTrue(catalog.validate(public)["valid"])
            (sources[1] / "IMG_0001.MOV").write_bytes(b"new copy version with different byte content")
            refresh()
            run()
            updated = catalog.read_json(path)["items"]
            copy_now = next(x for x in updated if x["id"] == copied["id"])
            canonical_now = next(x for x in updated if x["id"] == primary["id"])
            self.assertNotEqual(copy_now["file_id"], old_file)
            self.assertNotIn("canonical_byte_source_asset_id", copy_now)
            self.assertNotIn("byte_copy_asset_ids", canonical_now)
            self.assertEqual(copy_now["version_history"][0]["file_id"], old_file)
            self.assertEqual(copy_now["prior_version_review"]["human_review"], "passed")
            self.assertEqual(copy_now["project_assignment_review"]["status"], "needs_review_source_version_changed")
            self.assertEqual(copy_now["relation_history"][0]["canonical_byte_source_asset_id"], primary["id"])
            self.assertTrue(catalog.validate(public)["valid"])
            history = copy.deepcopy(copy_now["relation_history"])
            run()
            again = next(x for x in catalog.read_json(path)["items"] if x["id"] == copied["id"])
            self.assertEqual(again["relation_history"], history)
            self.assertEqual(len(again["version_history"]), 1)

    def test_invalid_import_candidate_keeps_output_and_identity_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, state, sources, refresh, run = import_fixture(Path(tmp))
            (sources[0] / "IMG_0002.MOV").write_bytes(b"new allocation must roll back")
            refresh()
            old_public, old_state = snapshot(public), snapshot(state)
            with mock.patch.object(catalog, "validate", return_value={"valid": False, "errors": ["synthetic rejection"]}):
                with self.assertRaisesRegex(ValueError, "Candidate catalogue"):
                    run()
            self.assertEqual(snapshot(public), old_public)
            self.assertEqual(snapshot(state), old_state)

    def test_official_merge_validation_failure_keeps_formal_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, state, sources, refresh, run = import_fixture(Path(tmp))
            prepared = {"prepared_at": "2026-01-01T00:00:00Z", "scope": "synthetic", "sources": [], "summary": {"official_public_index_items": 0}, "preservation_rules": [], "items": []}
            staging = Path(tmp) / "official.json"
            catalog.write_json(staging, prepared)
            before = snapshot(public)
            with mock.patch.object(official, "validate", return_value={"valid": False, "errors": ["synthetic rejection"]}):
                with self.assertRaisesRegex(ValueError, "Official registry failed"):
                    official.merge_registry(public, staging)
            self.assertEqual(snapshot(public), before)

    def test_caption_merge_validation_failure_keeps_formal_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            public, state, sources, refresh, run = import_fixture(work)
            options, queue = work / "options.json", work / "queue.json"
            catalog.write_json(options, {"generated_at": "2026-01-01T00:00:00Z", "approved_original_exports": [], "scope_approved_original_caption_files": 0, "all_canonical_caption_options": []})
            catalog.write_json(queue, {"items": []})
            before_public, before_state = snapshot(public), snapshot(state)
            with mock.patch.object(captions, "validate", return_value={"valid": False, "errors": ["synthetic rejection"]}):
                with self.assertRaisesRegex(ValueError, "Merged release did not validate"):
                    captions.merge_release(public, state, options, queue)
            self.assertEqual(snapshot(public), before_public)
            self.assertEqual(snapshot(state), before_state)

    def test_rejected_scope_projection_does_not_remove_current_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            master, public, state, tables, policy, proof = scope_tests.PublicScopeTests().fixture(Path(tmp))
            before_public, before_state = snapshot(public), snapshot(state)
            with mock.patch.object(projection, "validate", return_value={"valid": False, "errors": ["synthetic rejection"]}):
                with self.assertRaisesRegex(ValueError, "Scope projection is invalid"):
                    projection.project(public, master, state, state / "deny.json", [state / "proof.json"], [s["id"] for s in tables["subtitles"][:3]])
            self.assertEqual(snapshot(public), before_public)
            self.assertEqual(snapshot(state), before_state)

    def test_studio_merge_validation_failure_keeps_output_and_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            public, state, sources, refresh, run = import_fixture(work)
            asset = catalog.read_json(public / "catalog/assets.json")["items"][0]
            source = next(f for f in catalog.read_json(public / "catalog/files.json")["items"] if f["id"] == asset["file_id"])
            location = catalog.read_json(state / "mapping.json")["files"][source["id"]]
            manifest, dedup = state / "manifest.json", state / "dedup.json"
            catalog.write_json(manifest, {"synthetic": True})
            catalog.write_json(dedup, {"synthetic": True})
            receipt = {"schema_version": "p4-existing-youtube-mapping-receipts-v1", "recorded_at": "2026-01-01T00:00:00Z", "observation_provenance": {"target_channel_id": "UCp_LyzepcrZX6Cgzv2Qit4w", "interface": "YouTube Studio videos and shorts table DOM", "platform_observed_at": None, "recorded_at_is_not_substituted_for_observed_at": True, "raw_studio_rows_persisted_with_this_receipt": False}, "trusted_local_hash_provenance": {"manifest_integrity_verified": True, "manifest_path": str(manifest), "manifest_sha256": catalog.digest(manifest), "deduplication_path": str(dedup), "deduplication_sha256": catalog.digest(dedup)}, "summary": {"exact_existing_source_mappings": 1}, "exact_existing_mappings": [{"asset_id": asset["id"], "asset_accession": asset["accession"], "file_id": source["id"], "file_accession": source["accession"], "source_sha256": source["sha256"], "source_bytes": location["bytes"], "source_mtime_ns": location["mtime_ns"], "local_source_verification": {"checks": {"synthetic_fingerprint_matched": True}}, "youtube": {"video_id": "Fixture_001", "channel_id": "UCp_LyzepcrZX6Cgzv2Qit4w", "url": "https://www.youtube.com/watch?v=Fixture_001", "visibility": "public", "description_source_sha256": source["sha256"], "source_mapping_status": "exact_description_source_sha256_match", "existing_video_not_new_upload": True, "uploaded_stream_hash_verified": False, "playback_status": "not_checked", "caption_status": "not_checked"}}]}
            receipt_path = state / "receipt.json"
            catalog.write_json(receipt_path, receipt)
            before_public, before_state = snapshot(public), snapshot(state)
            with mock.patch.object(studio, "validate", return_value={"valid": False, "errors": ["synthetic rejection"]}):
                with self.assertRaisesRegex(ValueError, "Platform metadata release failed"):
                    studio.merge_receipts(public, state, receipt_path)
            self.assertEqual(snapshot(public), before_public)
            self.assertEqual(snapshot(state), before_state)
            self.assertEqual(catalog.read_json(public / "catalog/publications.json")["items"], [])

    def test_promotion_io_failure_rolls_back_public_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            public, state = work / "public", work / "private"
            public.mkdir(); state.mkdir()
            (public / "index.md").write_bytes(b"before")
            catalog.write_json(state / "registry.json", {"before": 1})
            catalog.write_json(state / "mapping.json", {"before": 2})
            before_public, before_state = snapshot(public), snapshot(state)
            def change(candidate, private):
                (candidate / "index.md").write_bytes(b"after")
                (candidate / "new.md").write_bytes(b"new")
                catalog.write_json(private / "registry.json", {"after": 1})
                catalog.write_json(private / "mapping.json", {"after": 2})
            original = transaction._atomic_copy
            failed = []
            def fail_once(source, target, private=False):
                if Path(target).resolve() == (state / "registry.json").resolve() and not failed:
                    failed.append(True)
                    raise OSError("synthetic disk write failure")
                return original(source, target, private)
            with mock.patch.object(transaction, "_atomic_copy", fail_once):
                with self.assertRaisesRegex(OSError, "synthetic disk"):
                    transaction.staged_update(public, change, lambda root: {"valid": True}, local_state=state)
            self.assertEqual(snapshot(public), before_public)
            self.assertEqual(snapshot(state), before_state)

    def test_abrupt_exit_recovers_before_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            public, state = work / "public", work / "private"
            public.mkdir(); state.mkdir()
            (public / "index.md").write_bytes(b"before")
            catalog.write_json(state / "registry.json", {"before": 1})
            code = '''import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import catalog_transaction as t
public,state=Path(sys.argv[2]),Path(sys.argv[3])
def change(candidate,private):
    (candidate/'index.md').write_bytes(b'after')
    (private/'registry.json').write_bytes(b'{"after":1}')
original=t._atomic_copy
def interrupted(source,target,private=False):
    original(source,target,private)
    if Path(target).resolve()==(public/'index.md').resolve(): os._exit(23)
t._atomic_copy=interrupted
t.staged_update(public,change,lambda root:{'valid':True},local_state=state)
'''
            child = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(ROOT / "scripts"), str(public), str(state)], capture_output=True)
            self.assertEqual(child.returncode, 23, child.stderr)
            self.assertTrue((state / transaction.PENDING).exists())
            transaction.staged_update(public, lambda root, private: None, lambda root: {"valid": True}, local_state=state)
            self.assertEqual((public / "index.md").read_bytes(), b"before")
            self.assertEqual(catalog.read_json(state / "registry.json"), {"before": 1})
            self.assertFalse((state / transaction.PENDING).exists())

    def test_projection_approved_id_cannot_accept_a_new_caption_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            master, public, state, tables, policy, proof = scope_tests.PublicScopeTests().fixture(Path(tmp))
            for event in tables["events"]:
                event["event_type"] = "rehearsal"
            catalog.write_json(master / "catalog/events.json", {"schema_version": 1, "items": tables["events"]})
            project(public, master, state, state / "deny.json", [state / "proof.json"], [s["id"] for s in tables["subtitles"][:3]])
            path = public / "catalog/subtitles.json"
            subtitles = catalog.read_json(path)
            sub = subtitles["items"][0]
            old = public / sub["public_export"]["path"]
            content = b"1\n00:00:01,000 --> 00:00:02,000\nUnapproved changed version\n"
            sha = hashlib.sha256(content).hexdigest()
            relative = "captions/" + sub["accession"] + "/" + sha + "/original.srt"
            target = public / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(content)
            old.unlink()
            sub.update(version_sha256=sha, bytes=len(content))
            sub["public_export"].update(path=relative, sha256=sha)
            catalog.write_json(path, subtitles)
            result = catalog.validate(public)
            self.assertFalse(result["valid"])
            self.assertTrue(any("explicit approval" in e for e in result["errors"]), result)


if __name__ == "__main__":
    unittest.main()
