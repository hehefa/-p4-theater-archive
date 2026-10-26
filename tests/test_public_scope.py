import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import archive_catalog as catalog
from merge_caption_release import merge_release
from project_public_scope import project, under_prefix
from projection_safe_queue import apply_guard


def entity(kind, number):
    return {"id": kind + ":fixture-" + str(number), "accession": "P4-" + catalog.PREFIX[kind] + f"{number:06d}", "kind": kind, "title": "Synthetic fixture", "in_current_inventory": True}


class PublicScopeTests(unittest.TestCase):
    def fixture(self, work):
        master, public, state = work / "master", work / "public", work / "private"
        tables = {name: [] for name in catalog.TABLES}
        p = entity("project", 1)
        p.update(aliases=[], legacy_reconciliation="pending")
        c = entity("collection", 1)
        c.update(project_id=p["id"], inventory_file_count=3)
        tables["projects"], tables["collections"] = [p], [c]
        mapping = {"files": {}}
        for number in (1, 2, 3):
            a, f, e = entity("asset", number), entity("file", number), entity("event", number)
            sha = hashlib.sha256(("video fixture " + str(number)).encode()).hexdigest()
            a.update(project_ids=[p["id"]], collection_id=c["id"], file_id=f["id"], event_ids=[e["id"]], subtitle_ids=[], youtube_publication_ids=[], publication_status="pending_review", human_review="not_recorded", rights_status="not_established")
            f.update(asset_id=a["id"], extension=".mov", bytes=100, sha256=sha, full_decode_status="not_recorded")
            e.update(project_ids=[p["id"]], collection_id=c["id"], date={"value": None})
            tables["assets"].append(a)
            tables["files"].append(f)
            tables["events"].append(e)
            path = "K:/fixture/allowed.mov" if number == 1 else ("K:/fixture/private.mov" if number == 2 else "k:/FIXTURE/camera2/new-not-in-id-deny.mov")
            mapping["files"][f["id"]] = {"path": path, "bytes": 100, "mtime_ns": 1}
        for number in (1, 2, 3, 4):
            owner = tables["assets"][0 if number <= 3 else 1]
            sub = entity("subtitle", number)
            body = ("1\n00:00:01,000 --> 00:00:02,000\nSynthetic variant " + str(number) + "\n").encode()
            sha = hashlib.sha256(body).hexdigest()
            relative = "captions/" + sub["accession"] + "/" + sha + "/original.srt"
            sub.update(asset_ids=[owner["id"]], source_asset_id=owner["id"], source_file_id=owner["file_id"], language="und", role="original_asr", clock="source_zero", version_sha256=sha, bytes=len(body), cue_count=1, format_status="structure_passed", human_listening_review="not_recorded", public_text_status="public_machine_draft", public_export={"path": relative, "sha256": sha, "scope_review_status": "approved_for_public_archive", "quality_label": "machine_draft"})
            owner["subtitle_ids"].append(sub["id"])
            owner.setdefault("public_caption_subtitle_ids", []).append(sub["id"])
            tables["subtitles"].append(sub)
            target = master / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        for name, items in tables.items():
            catalog.write_json(master / "catalog" / (name + ".json"), {"schema_version": 1, "items": items})
        catalog.write_json(master / "catalog/byte-equality-groups.json", {"schema_version": 1, "items": []})
        catalog.write_json(master / "catalog/summary.json", {"schema_version": 1, "counts": {name: len(items) for name, items in tables.items()}})
        catalog.write_json(state / "mapping.json", mapping)
        catalog.write_json(state / "registry.json", {"permanent_fixture_ids": [a["id"] for a in tables["assets"]]})
        policy = {"active": True, "fail_closed": True, "decision_source": "synthetic_scope_fixture", "excluded_asset_ids": [tables["assets"][1]["id"]], "excluded_file_ids": [tables["files"][1]["id"]], "excluded_subtitle_ids": [tables["subtitles"][3]["id"]], "deny_path_prefixes": ["K:\\fixture\\Camera2"]}
        proof = {"schema_version": "fixture", "decision": "confirmed_project_media", "scope": "project_media_provenance_only", "asset_id": tables["assets"][0]["id"], "asset_accession": tables["assets"][0]["accession"], "file_id": tables["files"][0]["id"], "file_accession": tables["files"][0]["accession"], "source_sha256": tables["files"][0]["sha256"], "youtube_video_id": "Fixture_001", "evidence": [{"url": "https://a.p4theater.cn/videos/fixture", "http_status": 200, "html_utf8_sha256": "a" * 64}, {"url": "https://b.p4theater.cn/questions/fixture", "http_status": 200, "html_utf8_sha256": "b" * 64}], "unsupported_claims": ["Synthetic test data only"]}
        catalog.write_json(state / "deny.json", policy)
        catalog.write_json(state / "proof.json", proof)
        shutil.copytree(master, public)
        return master, public, state, tables, policy, proof

    def test_projection_closure_ids_and_import_cannot_revive_excluded_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            master, public, state, tables, policy, proof = self.fixture(Path(tmp))
            registry_before = (state / "registry.json").read_bytes()
            allow = [x["id"] for x in tables["subtitles"][:3]]
            first = project(public, master, state, state / "deny.json", [state / "proof.json"], allow)
            self.assertEqual(first["public_selected_video_files"], 1)
            self.assertEqual(first["excluded_video_files"], 2)  # ID deny plus future prefix
            self.assertEqual(len(list((public / "captions").rglob("*.srt"))), 3)
            self.assertEqual(len(list((master / "captions").rglob("*.srt"))), 4)
            text = "\n".join(p.read_text(encoding="utf-8") for p in (public / "catalog").glob("*.json"))
            self.assertNotIn(tables["assets"][1]["id"], text)
            self.assertNotIn(tables["assets"][2]["id"], text)
            second = project(public, master, state, state / "deny.json", [state / "proof.json"], allow)
            self.assertEqual(second["public_caption_files"], 3)
            self.assertEqual((state / "registry.json").read_bytes(), registry_before)
            with self.assertRaisesRegex(ValueError, "Direct import"):
                catalog.import_catalog(SimpleNamespace(output=str(public), local_state=str(state), audit="does-not-exist"))
            with self.assertRaisesRegex(ValueError, "Direct caption merge"):
                merge_release(public, state, "does-not-exist", "does-not-exist")
            self.assertTrue(catalog.validate(public)["valid"])

    def test_pending_new_metadata_cannot_revive_prepared_caption_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            master, public, state, tables, policy, proof = self.fixture(Path(tmp))
            # A later private import adds an unconfirmed source and an old
            # internal-context caption approval. Projection must withhold it.
            asset = dict(tables["assets"][0])
            source = dict(tables["files"][0])
            asset.update(entity("asset", 4), file_id="file:fixture-4", subtitle_ids=["subtitle:fixture-5"], public_caption_subtitle_ids=["subtitle:fixture-5"])
            source.update(entity("file", 4), asset_id=asset["id"], sha256="c" * 64)
            sub = dict(tables["subtitles"][3])
            sub.update(entity("subtitle", 5), asset_ids=[asset["id"]], source_asset_id=asset["id"], source_file_id=source["id"])
            tables["assets"].append(asset)
            tables["files"].append(source)
            tables["subtitles"].append(sub)
            for name in ("assets", "files", "subtitles"):
                catalog.write_json(master / "catalog" / (name + ".json"), {"schema_version": 1, "items": tables[name]})
            mapping = catalog.read_json(state / "mapping.json")
            mapping["files"][source["id"]] = {"path": "K:/fixture/unconfirmed/new.mov", "bytes": 100, "mtime_ns": 1}
            catalog.write_json(state / "mapping.json", mapping)
            project(public, master, state, state / "deny.json", [state / "proof.json"], [x["id"] for x in tables["subtitles"][:3]])
            pending = next(x for x in catalog.read_json(public / "catalog/assets.json")["items"] if x["id"] == asset["id"])
            self.assertFalse(pending["new_public_upload_allowed"])
            self.assertEqual(pending["official_media_affiliation_status"], "official_media_affiliation_pending")
            pending_sub = next(x for x in catalog.read_json(public / "catalog/subtitles.json")["items"] if x["id"] == sub["id"])
            self.assertNotIn("public_export", pending_sub)
            self.assertEqual(len(list((public / "captions").rglob("*.srt"))), 3)

    def test_queue_missing_policy_evidence_fingerprint_and_future_prefix_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            master, public, state, tables, policy, proof = self.fixture(Path(tmp))
            mapping = catalog.read_json(state / "mapping.json")["files"]
            queue = {"items": [{"asset_id": a["id"], "file_id": a["file_id"], "source_path": mapping[a["file_id"]]["path"], "preflight": {"sha256": tables["files"][i]["sha256"]}, "proposed_upload": {"enabled": True}, "upload_allowed": True} for i, a in enumerate(tables["assets"])]}
            guarded = apply_guard(queue, policy, [proof], lambda item: True)
            self.assertTrue(guarded["items"][0]["existing_video_caption_update_allowed"])
            self.assertTrue(all(x["new_public_upload_allowed"] is False and x["upload_allowed"] is False and x["proposed_upload"]["enabled"] is False for x in guarded["items"]))
            self.assertTrue(guarded["items"][2]["scope_guard"]["mandatory_exclusion"])
            for absent_policy, absent_proof, fingerprint in ((None, [proof], True), (policy, [], True), (policy, [proof], False)):
                closed = apply_guard(queue, absent_policy, absent_proof, lambda item: fingerprint)
                self.assertTrue(all(x["existing_video_caption_update_allowed"] is False for x in closed["items"]))
            self.assertTrue(under_prefix("k:/FIXTURE/camera2/NEW.MOV", "K:\\fixture\\Camera2"))
            self.assertFalse(under_prefix("K:/fixture/Camera20/a.mov", "K:\\fixture\\Camera2"))
            # A future, previously unpublished source can be authorized after
            # source-bound official-context + content/family + duplicate review;
            # it need not already have a public video or hash on the website.
            future = dict(proof)
            future.pop("youtube_video_id")
            future["scope"] = "official_context_plus_actual_media_cross_review"
            future["evidence"] = [proof["evidence"][1]]  # Main archive alone is sufficient.
            future["actual_content_review"] = {"completed": True, "source_sha256": proof["source_sha256"], "reviewed_at": "2026-10-26T00:00:00Z", "coverage": "synthetic full fixture content review"}
            authorized_policy = dict(policy)
            authorized_policy["new_upload_authorizations"] = [{"asset_id": proof["asset_id"], "file_id": proof["file_id"], "source_sha256": proof["source_sha256"], "decision": "approved_for_new_upload", "official_context_verified": True, "actual_content_review_completed": True, "family_privacy_review_passed": True, "duplicate_check_completed": True}]
            future_queue = apply_guard(queue, authorized_policy, [future], lambda item: True)
            self.assertTrue(future_queue["items"][0]["new_public_upload_allowed"])
            self.assertFalse(future_queue["items"][2]["new_public_upload_allowed"])


if __name__ == "__main__":
    unittest.main()
