import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from archive_catalog import write_json
import public_accessions as accessions


class PublicAccessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "public"
        self.root.mkdir()
        self.verified = mock.patch.object(accessions, "check_publications", return_value={"valid": True})
        self.verified.start()
        self.addCleanup(self.verified.stop)
        self.addCleanup(self.temp.cleanup)
        self.items = [self.item("document:original", "document", b"article", accession="P4-X-existing"), self.item("caption:original", "caption", b"caption"), self.item("research:original", "research", b"research"), self.item("hf-img0001", "image", b"image", image_id="hf-img0001")]
        self.manifest()

    def item(self, identity, kind, body, **fields):
        slug = hashlib.sha256(identity.encode()).hexdigest()[:12]
        file = {"path": "published/fixture/" + slug + ".bin", "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "storage": "git"}
        target = self.root / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {"id": identity, "kind": kind, "title": "中文公开成品 " + identity, "source_url": "https://a.p4theater.cn/edition/" + slug, "official_context_url": "https://hefa.p4theater.cn/works/fixture", "files": [file], **fields}

    def manifest(self):
        write_json(self.root / "published/manifest.json", {"schema_version": 1, "items": self.items})

    def state(self):
        return json.loads((self.root / accessions.REGISTRY).read_text(encoding="utf-8"))

    def baseline(self):
        path = self.root.parent / "trusted-prior-registry.json"
        shutil.copyfile(self.root / accessions.REGISTRY, path)
        return path

    def save_state(self, value):
        write_json(self.root / accessions.REGISTRY, value)

    def test_first_assignment_explicit_kind_segments_aliases_and_determinism(self):
        with self.assertRaisesRegex(ValueError, "state is missing"):
            accessions.assign(self.root)
        self.assertFalse((self.root / accessions.REGISTRY).exists())
        result = accessions.assign(self.root, initialize=True)
        self.assertTrue(result["valid"])
        state = self.state()
        self.assertEqual([i["accession"] for i in state["items"]], ["AR-T000001", "AR-S000001", "AR-R000001", "AR-I000001"])
        self.assertIn({"namespace": "published:accession", "value": "P4-X-existing"}, state["items"][0]["aliases"])
        self.assertIn({"namespace": "published:image_id", "value": "hf-img0001"}, state["items"][3]["aliases"])
        before = (self.root / accessions.REGISTRY).read_bytes()
        accessions.assign(self.root)
        self.assertEqual(before, (self.root / accessions.REGISTRY).read_bytes())
        self.assertTrue(accessions.check(self.root)["valid"])
        self.assertNotIn(b"\r", (self.root / accessions.INDEX).read_bytes())
        with self.assertRaises(ValueError):
            accessions.assign(self.root, initialize=True)

    def test_reorder_new_item_and_new_file_append_without_renumbering(self):
        accessions.assign(self.root, initialize=True)
        baseline = self.baseline()
        original = self.state()
        image = next(i for i in self.items if i["kind"] == "image")
        added = self.item("file:new-for-image", "image", b"new member")
        image["files"].insert(0, added["files"][0])
        self.items.insert(0, self.item("document:new-earlier", "document", b"new earlier item"))
        self.items.reverse()
        self.manifest()
        accessions.assign(self.root, baseline=baseline)
        state = self.state()
        by_id = {i["source_id"]: i for i in state["items"]}
        for old in original["items"]:
            self.assertEqual(by_id[old["source_id"]]["accession"], old["accession"])
            self.assertEqual(by_id[old["source_id"]]["files"][0]["file_accession"], old["files"][0]["file_accession"])
        self.assertEqual(by_id["document:new-earlier"]["accession"], "AR-T000002")
        image = by_id["hf-img0001"]
        self.assertEqual(image["file_counter"], 2)
        self.assertEqual(image["files"][1]["file_accession"], "AR-I000001-F00002")
        self.assertTrue(accessions.check(self.root, baseline=baseline)["valid"])

    def test_same_bytes_multiple_git_release_locations_reuse_file_number(self):
        accessions.assign(self.root, initialize=True)
        baseline = self.baseline()
        image = self.items[3]
        file = copy.deepcopy(image["files"][0])
        file.update(path="published/images/virtual/original.png", storage="release", download_url="https://github.com/example/archive/releases/download/v1/images.zip", archive_path="images/original.png")
        image["files"].append(file)
        self.manifest()
        accessions.assign(self.root, baseline=baseline)
        row = next(r for r in self.state()["items"] if r["source_id"] == image["id"])
        self.assertEqual(row["file_counter"], 1)
        self.assertEqual(len(row["files"][0]["locations"]), 2)
        self.assertEqual({f["file_accession"] for f in row["versions"][-1]["files"]}, {"AR-I000001-F00001"})
        self.assertFalse((self.root / file["path"]).exists())
        self.assertTrue(accessions.check(self.root, baseline=baseline)["valid"])
        index = (self.root / "numbered/images.md").read_text(encoding="utf-8")
        self.assertIn("](" + file["download_url"] + ")", index)
        self.assertNotIn("](" + file["path"] + ")", index)
        self.assertNotIn("](published/packaged-images/", index)

    def test_release_members_without_actual_download_url_cannot_allocate(self):
        for value in (None, "file:///private/archive.zip", "published/packaged-images/virtual.png"):
            image = self.items[3]
            image["files"][0].update(storage="release", archive_path="images/member.png")
            if value is None:
                image["files"][0].pop("download_url", None)
            else:
                image["files"][0]["download_url"] = value
            self.manifest()
            with self.assertRaises(ValueError):
                accessions.assign(self.root, initialize=True)
            self.assertFalse((self.root / accessions.REGISTRY).exists())

    def test_new_version_retains_accession_old_versions_and_old_file_number(self):
        accessions.assign(self.root, initialize=True)
        baseline = self.baseline()
        old = self.state()["items"][0]
        self.items[0]["files"] = self.item("document:new-bytes", "document", b"new text version")["files"]
        self.items[0]["source_url"] += "-revised"
        self.manifest()
        accessions.assign(self.root, baseline=baseline)
        current = self.state()["items"][0]
        self.assertEqual(current["accession"], old["accession"])
        self.assertEqual(current["versions"][0], old["versions"][0])
        self.assertEqual(len(current["versions"]), 2)
        self.assertEqual(current["file_counter"], 2)
        self.assertTrue(accessions.check(self.root, baseline=baseline)["valid"])

    def test_removed_source_keeps_historical_number_and_is_not_reused(self):
        accessions.assign(self.root, initialize=True)
        baseline = self.baseline()
        self.items = self.items[1:]
        self.items.append(self.item("document:replacement", "document", b"replacement"))
        self.manifest()
        accessions.assign(self.root, baseline=baseline)
        rows = {r["source_id"]: r for r in self.state()["items"]}
        self.assertFalse(rows["document:original"]["in_current_manifest"])
        self.assertEqual(rows["document:replacement"]["accession"], "AR-T000002")
        self.assertTrue(accessions.check(self.root, baseline=baseline)["valid"])

    def test_deleted_mapping_number_swap_file_swap_and_history_erasure_fail(self):
        accessions.assign(self.root, initialize=True)
        baseline = self.baseline()
        state = self.state()
        for mutate in (lambda s: s["items"].pop(), lambda s: s["items"][0].update(accession="AR-S000001"), lambda s: s["items"][0]["files"][0].update(file_accession="AR-T000001-F00002"), lambda s: s["items"][0].update(versions=[]), lambda s: s["counters"].update(document=0), lambda s: s["items"][0]["versions"][0].update(version_sha256="0" * 64), lambda s: s["items"][0]["versions"][0].update(source_url="file:///private/source"), lambda s: s["items"][0]["versions"][0]["files"][0].update(location_sha256="a" * 64)):
            changed = copy.deepcopy(state)
            mutate(changed)
            self.save_state(changed)
            self.assertFalse(accessions.check(self.root, baseline=baseline)["valid"])
        self.save_state(state)
        # Even coherent removal from both source and current state cannot erase a baseline mapping.
        self.items.pop()
        self.manifest()
        changed = copy.deepcopy(state)
        changed["items"].pop(); changed["counters"]["image"] = 0
        changed["source_manifest_sha256"] = hashlib.sha256((self.root / "published/manifest.json").read_bytes()).hexdigest()
        self.save_state(changed)
        self.assertFalse(accessions.check(self.root, baseline=baseline)["valid"])

    def test_missing_registry_never_silently_reinitializes_and_index_check_is_readonly(self):
        accessions.assign(self.root, initialize=True)
        path = self.root / accessions.INDEX
        path.write_text("stale", encoding="utf-8")
        self.assertFalse(accessions.check(self.root)["valid"])
        self.assertEqual(path.read_text(encoding="utf-8"), "stale")
        (self.root / accessions.REGISTRY).unlink()
        with self.assertRaises(ValueError):
            accessions.assign(self.root, initialize=True)

    def test_compact_classification_pages_relative_links_and_stable_range_split(self):
        # A low display threshold exercises stable pagination without large fixtures.
        self.items.extend(self.item("hf-img-extra-" + str(i), "image", ("member " + str(i)).encode()) for i in range(9))
        self.manifest()
        with mock.patch.object(accessions, "PAGE_LIMIT", 2000), mock.patch.object(accessions, "RANGE_SIZE", 3):
            accessions.assign(self.root, initialize=True)
            before = (self.root / accessions.REGISTRY).read_bytes()
            self.assertTrue(accessions.check(self.root)["valid"])
            main = (self.root / accessions.INDEX).read_text(encoding="utf-8")
            self.assertIn("numbered/documents.md", main)
            self.assertIn("numbered/images.md", main)
            self.assertNotIn("hf-img-extra-", main)
            pages = accessions._indexes(self.state())
            self.assertTrue(all(len(body) < 2000 for body in pages.values()))
            self.assertTrue("numbered/images-000001-000003.md" in pages, sorted(pages))
            image = (self.root / "numbered/images-000001-000003.md").read_text(encoding="utf-8")
            self.assertIn("../published/fixture/", image)
            self.assertIn("../registry/public-accessions.json", image)
            accessions.assign(self.root)
            self.assertEqual(before, (self.root / accessions.REGISTRY).read_bytes())
            category = self.root / "numbered/documents.md"
            original = category.read_bytes()
            category.write_text("stale category", encoding="utf-8")
            self.assertFalse(accessions.check(self.root)["valid"])
            self.assertEqual(category.read_text(encoding="utf-8"), "stale category")
            category.write_bytes(original)
            stale = self.root / "numbered/images-999901-999999.md"
            stale.write_text("old generated range", encoding="utf-8")
            self.assertFalse(accessions.check(self.root)["valid"])
            accessions.assign(self.root)
            self.assertFalse(stale.exists())
            self.assertTrue(accessions.check(self.root)["valid"])

    def test_invalid_publisher_version_or_local_metadata_cannot_allocate(self):
        with mock.patch.object(accessions, "check_publications", return_value={"valid": False}):
            with self.assertRaises(ValueError):
                accessions.assign(self.root, initialize=True)
        self.items[0]["files"][0]["path"] = "/Users/private/original.pdf"
        self.manifest()
        with self.assertRaises(ValueError):
            accessions.assign(self.root, initialize=True)
        self.assertFalse((self.root / accessions.REGISTRY).exists())

    @unittest.skipUnless(shutil.which("git"), "Git is needed for main-branch clone test")
    def test_first_main_commit_full_history_passes_and_shallow_ci_fails_closed(self):
        def git(root, *args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=True)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "config", "user.name", "Initial main fixture")
        git(self.root, "config", "core.hooksPath", os.devnull)
        git(self.root, "config", "core.autocrlf", "true")
        # Match the real repository's byte rules before committing the fixture.
        shutil.copyfile(ROOT / ".gitattributes", self.root / ".gitattributes")
        accessions.assign(self.root, initialize=True)
        self.assertTrue(accessions.check(self.root)["valid"])
        bound_bytes = {path: (self.root / path).read_bytes() for path in ("published/manifest.json", accessions.REGISTRY, accessions.INDEX)}
        git(self.root, "add", ".")
        git(self.root, "-c", "commit.gpgsign=false", "commit", "-qm", "First main numbering")
        git(self.root, "branch", "-M", "main")
        committed = accessions.check(self.root)
        self.assertTrue(committed["valid"], committed)
        self.assertEqual(committed["append_only_baseline"], "git_committed_history")
        clone = self.root.parent / "ci-clone"
        subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "core.autocrlf=true", "clone", "--depth=1", self.root.as_uri(), str(clone)], capture_output=True, text=True, encoding="utf-8", check=True)
        for path, expected in bound_bytes.items():
            self.assertEqual((clone / path).read_bytes(), expected, path)
        shallow = accessions.check(clone)
        self.assertFalse(shallow["valid"])
        self.assertTrue(any("shallow" in error for error in shallow["errors"]), shallow)
        git(clone, "-c", "protocol.file.allow=always", "fetch", "--unshallow")
        complete = accessions.check(clone)
        self.assertTrue(complete["valid"], complete)
        with self.assertRaises(ValueError):
            accessions.assign(clone, initialize=True)

    @unittest.skipUnless(shutil.which("git"), "Git is needed for committed-baseline test")
    def test_committed_git_baseline_protects_last_mapping_even_if_counter_rewritten(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, encoding="utf-8", check=True)
        git("init", "-q")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "Numbering fixture")
        git("config", "core.hooksPath", os.devnull)
        accessions.assign(self.root, initialize=True)
        git("add", ".")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "Initial numbering fixture")
        self.assertTrue(accessions.check(self.root)["valid"])
        state = self.state()
        state["items"].pop(); state["counters"]["image"] = 0
        self.items.pop(); self.manifest()
        state["source_manifest_sha256"] = hashlib.sha256((self.root / "published/manifest.json").read_bytes()).hexdigest()
        self.save_state(state)
        self.assertFalse(accessions.check(self.root)["valid"])
        (self.root / accessions.REGISTRY).unlink()
        (self.root / accessions.INDEX).unlink()
        with self.assertRaises(ValueError):
            accessions.assign(self.root, initialize=True)


if __name__ == "__main__":
    unittest.main()
