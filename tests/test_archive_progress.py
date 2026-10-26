import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from archive_catalog import write_json
import archive_progress as progress

URL = "https://github.com/hehefa/-p4-theater-archive/commit/example"


class ArchiveProgressTests(unittest.TestCase):
    def fixture(self, root):
        def stage(count):
            return {"count": count, "evidence_urls": [URL]}
        batch = {"id": "fixed-caption-release", "title": "已核对字幕批次", "scope": "固定发行版字幕，非全库", "total": 2, "unit": "份SRT", "stages": {"prepared": stage(2), "github_published": stage(2), "platform_published": stage(None)}, "status": "github_published", "next_action": "平台状态按新证据补充", "links": [URL], "snapshot": {"kind": "caption", "measure": "files", "stage": "github_published", "ids": ["edition:a", "edition:b"]}}
        data = {"schema_version": 1, "as_of": "2026-10-26T18:00:00Z", "scope_note": "明确批次分别记录；家庭范围排除不作待发布量。", "batches": [batch], "leads": [{"id": "new-lead", "title": "新增材料待核定", "state": "unknown", "evidence_urls": [URL], "next_action": "明确来源与数量后新建批次"}]}
        manifest = {"schema_version": 1, "items": [{"id": "edition:a", "kind": "caption", "youtube_id": "Fixture_001", "files": [{"path": "published/captions/a/en.srt"}]}, {"id": "edition:b", "kind": "caption", "youtube_id": "Fixture_001", "files": [{"path": "published/captions/a/zh.srt"}]}, {"id": "document:c", "kind": "document", "files": [{"path": "published/materials/c.md"}]}]}
        write_json(root / "progress/work-items.json", data)
        write_json(root / "published/manifest.json", manifest)
        write_json(root / "catalog/summary.json", {"schema_version": 1, "public_selected_video_files": 1445, "public_excluded_video_files": 295, "full_sha256_recorded": 49, "full_decode_passed_existing_reports": 10, "official_media_affiliation_confirmed_assets": 8})
        return data, manifest

    def save(self, root, data):
        write_json(root / "progress/work-items.json", data)

    def summary(self, root):
        return json.loads((root / "progress/summary.json").read_text(encoding="utf-8"))

    def test_build_check_independent_stages_metrics_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            self.assertTrue(progress.build(root)["valid"])
            self.assertTrue(progress.check(root)["valid"])
            summary = self.summary(root)
            stage = summary["batches"][0]["stages"]
            self.assertEqual(stage["github_published"]["percent"], 100.0)
            self.assertIsNone(stage["platform_published"]["count"])
            self.assertIsNone(stage["platform_published"]["percent"])
            self.assertFalse(stage["platform_published"]["complete"])
            metric = summary["catalogue_metrics"]
            self.assertEqual(metric["selected"], 1445)
            self.assertEqual(metric["excluded"], 295)
            self.assertEqual([r["percent"] for r in metric["metrics"].values()], [3.4, 0.7, 0.6])
            self.assertEqual(summary["overall_progress"]["status"], "not_calculated")
            page = (root / "PROGRESS.md").read_text(encoding="utf-8")
            self.assertIn("[x] GitHub已发布：2/2（100.0%）", page)
            self.assertLess(page.index("## 批次速览"), page.index("## 当前原片工序"))
            self.assertIn("| 批次 | 已整理 | GitHub归档 | 平台回执 |", page)
            self.assertIn("| 已核对字幕批次（份SRT） | 2/2（100.0%） | 2/2（100.0%） | 未核定/2（未核定） |", page)
            self.assertIn("[ ] 平台已发布：未核定/2（未核定）", page)
            self.assertIn("295项；单独保留，不计入待发布分母", page)
            self.assertNotIn(b"\r", (root / "PROGRESS.md").read_bytes())
            cli = subprocess.run([sys.executable, str(ROOT / "scripts/archive_progress.py"), "check", "--root", str(root)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertIsNone(json.loads(cli.stdout)["overall_percent"])

    def test_unknown_total_and_missing_metric_remain_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            batch = data["batches"][0]
            batch.pop("snapshot")
            batch["total"] = None
            batch["stages"]["prepared"]["count"] = 5
            self.save(root, data)
            write_json(root / "catalog/summary.json", {"schema_version": 1, "full_sha256_recorded": 49})
            progress.build(root)
            summary = self.summary(root)
            self.assertIsNone(summary["catalogue_metrics"]["selected"])
            self.assertIsNone(summary["catalogue_metrics"]["metrics"]["full_sha256_recorded"]["percent"])
            self.assertIsNone(summary["catalogue_metrics"]["metrics"]["full_decode_passed_existing_reports"]["count"])
            self.assertIsNone(summary["batches"][0]["stages"]["prepared"]["percent"])
            self.assertIn("5/未核定（未核定）", (root / "PROGRESS.md").read_text(encoding="utf-8"))

    def test_image_materials_and_file_locations_use_separate_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            manifest['items'].append({'id': 'image:one', 'kind': 'image', 'files': [
                {'path': 'published/images/one.jpg'},
                {'path': 'published/packaged-images/bundle/one.jpg'},
                {'path': 'published/packaged-images/bundle/one-tile.jpg'}]})
            write_json(root / 'published/manifest.json', manifest)
            batch = data['batches'][0]
            batch.update(total=1, unit='条图像材料')
            batch['stages']['prepared']['count'] = batch['stages']['github_published']['count'] = 1
            batch['snapshot'].update(kind='image', measure='items', ids=['image:one'])
            self.save(root, data)
            self.assertTrue(progress.build(root)['valid'])
            self.assertEqual(self.summary(root)['batches'][0]['total'], 1)
            batch['snapshot']['measure'] = 'files'
            self.save(root, data)
            self.assertFalse(progress.check(root)['valid'])
            batch['total'] = 3
            batch['stages']['prepared']['count'] = batch['stages']['github_published']['count'] = 3
            self.save(root, data)
            self.assertTrue(progress.build(root)['valid'])

    def test_snapshot_ids_freeze_cohort_when_new_items_arrive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            progress.build(root)
            manifest["items"].append({"id": "edition:new", "kind": "caption", "youtube_id": "Fixture_002", "files": [{"path": "published/captions/new/en.srt"}]})
            write_json(root / "published/manifest.json", manifest)
            self.assertFalse(progress.check(root)["valid"])  # Source hash changed; rebuild expected.
            progress.build(root)
            self.assertEqual(self.summary(root)["batches"][0]["total"], 2)
            self.assertEqual(self.summary(root)["batches"][0]["snapshot"]["actual_count"], 2)
            data["batches"][0]["snapshot"].pop("ids")
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])

    def test_snapshot_rejects_invented_totals_missing_duplicate_or_wrong_kind_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            mutations = [lambda b: b.update(total=3), lambda b: b["stages"]["github_published"].update(count=1), lambda b: b["snapshot"].update(ids=["edition:missing"]), lambda b: b["snapshot"].update(ids=["edition:a", "edition:a"]), lambda b: b["snapshot"].update(ids=["document:c"])]
            for mutate in mutations:
                value = copy.deepcopy(data)
                mutate(value["batches"][0])
                self.save(root, value)
                with self.subTest(value=value["batches"][0]):
                    self.assertFalse(progress.check(root)["valid"])

    def test_items_files_and_video_version_measures_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            batch = data["batches"][0]
            for measure, count in (("items", 2), ("files", 2), ("video_versions", 1)):
                batch["snapshot"]["measure"] = measure
                batch["total"] = count
                batch["stages"]["prepared"]["count"] = None
                batch["stages"]["github_published"]["count"] = count
                self.save(root, data)
                progress.build(root)
                self.assertEqual(self.summary(root)["batches"][0]["snapshot"]["actual_count"], count)
                self.assertFalse(self.summary(root)["batches"][0]["stages"]["prepared"]["complete"])

    def test_body_completion_filter_keeps_full_article_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            manifest["items"] = [{"id": "article:" + str(i), "kind": "document", "content_status": "captured_text" if i < 538 else "metadata_only", "files": [{"path": "published/materials/article-" + str(i) + ".md"}]} for i in range(554)]
            write_json(root / "published/manifest.json", manifest)
            batch = data["batches"][0]
            batch.update(total=554, unit="篇文章", snapshot={"kind": "document", "measure": "items", "stage": "github_published", "ids": [x["id"] for x in manifest["items"]], "completed_where": {"content_status": ["captured_text", "full_text", "edited_public_excerpt"]}})
            batch["stages"]["prepared"]["count"] = 554
            batch["stages"]["github_published"]["count"] = 538
            self.save(root, data)
            progress.build(root)
            result = self.summary(root)["batches"][0]
            self.assertEqual(result["snapshot"]["actual_total"], 554)
            self.assertEqual(result["snapshot"]["actual_stage_count"], 538)
            self.assertEqual(result["snapshot"]["actual_count"], 554)
            self.assertEqual(result["stages"]["github_published"]["percent"], 97.1)
            self.assertFalse(result["stages"]["github_published"]["complete"])
            self.assertIn("538/554（97.1%）", (root / "PROGRESS.md").read_text(encoding="utf-8"))
            batch["stages"]["github_published"]["count"] = 554
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])
            batch["snapshot"].pop("completed_where")
            self.save(root, data)
            progress.build(root)
            self.assertEqual(self.summary(root)["batches"][0]["stages"]["github_published"]["percent"], 100.0)

    def test_completed_filter_rejects_other_fields_invalid_values_and_nonitem_measure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            batch = data["batches"][0]
            for condition in ({"kind": ["caption"]}, {"content_status": []}, {"content_status": [""]}, {"content_status": [1]}, {"content_status": ["captured_text"], "kind": ["caption"]}):
                batch["snapshot"].update(measure="items", completed_where=condition)
                self.save(root, data)
                self.assertFalse(progress.check(root)["valid"])
            batch["snapshot"].update(measure="files", completed_where={"content_status": ["captured_text"]})
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])

    def test_caption_coverage_binds_counts_entities_languages_and_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            manifest["items"][0]["language"] = "en"
            manifest["items"][1]["language"] = "zh"
            write_json(root / "published/manifest.json", manifest)
            coverage_path = "progress/caption-top10-20261024.json"
            coverage = {"schema_version": 1, "target_language_slots": 3, "github_versions_recorded": 2, "source_version_unresolved": 1, "items": [{"youtube_id": "Fixture_001", "language": "en", "state": "github_version_recorded", "caption_item_id": "edition:a"}, {"youtube_id": "Fixture_001", "language": "zh", "state": "github_version_recorded", "caption_item_id": "edition:b"}, {"youtube_id": "Fixture_001", "language": "de", "state": "source_version_unresolved"}]}
            write_json(root / coverage_path, coverage)
            batch = data["batches"][0]
            batch.pop("snapshot")
            batch.update(total=3, unit="语言槽", coverage_source=coverage_path)
            self.save(root, data)
            progress.build(root)
            result = self.summary(root)
            self.assertEqual(result["batches"][0]["coverage"]["actual_stage_count"], 2)
            self.assertIn(coverage_path, result["source_sha256"])
            coverage["items"][2]["reason"] = "Updated source-version explanation"
            write_json(root / coverage_path, coverage)
            self.assertFalse(progress.check(root)["valid"])
            progress.build(root)
            batch["stages"]["github_published"]["count"] = 3
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])
            batch["stages"]["github_published"]["count"] = 2
            self.save(root, data)
            for mutate in (lambda c: c["items"][0].update(caption_item_id="edition:missing"), lambda c: c["items"][0].update(language="es"), lambda c: c["items"][0].update(state="uploaded"), lambda c: c["items"].append(copy.deepcopy(c["items"][0]))):
                changed = copy.deepcopy(coverage)
                mutate(changed)
                write_json(root / coverage_path, changed)
                self.assertFalse(progress.check(root)["valid"])
            write_json(root / coverage_path, coverage)
            for relative in ("progress/../escape.json", "progress/C:/escape.json", "published/manifest.json", "progress\\escape.json"):
                batch["coverage_source"] = relative
                self.save(root, data)
                self.assertFalse(progress.check(root)["valid"])
            batch["coverage_source"] = coverage_path
            batch["snapshot"] = {"kind": "caption", "measure": "items", "stage": "github_published"}
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])

    def test_invalid_counts_empty_evidence_bad_url_and_excluded_batch_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            mutations = [lambda b: b["stages"]["prepared"].update(count=-1), lambda b: b["stages"]["prepared"].update(count=True), lambda b: b["stages"]["prepared"].update(count=3), lambda b: b["stages"]["prepared"].update(evidence_urls=[]), lambda b: b["stages"]["prepared"].update(evidence_urls=["file:///private/source.json"]), lambda b: b.update(status="excluded"), lambda b: b.update(scope="family_excluded")]
            for mutate in mutations:
                value = copy.deepcopy(data)
                mutate(value["batches"][0])
                self.save(root, value)
                self.assertFalse(progress.check(root)["valid"])
            data["overall_percent"] = 100
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])

    def test_history_note_and_observation_time_do_not_become_current_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            entry = data["batches"][0]["stages"]["platform_published"]
            entry.update(count=1, note="历史回执声明，不是当前live复核", observed_at="2026-10-24T10:00:00Z")
            self.save(root, data)
            progress.build(root)
            summary = self.summary(root)["batches"][0]["stages"]["platform_published"]
            self.assertEqual(summary["percent"], 50.0)
            self.assertEqual(summary["observed_at"], "2026-10-24T10:00:00Z")
            page = (root / "PROGRESS.md").read_text(encoding="utf-8")
            self.assertIn("历史回执声明，不是当前live复核", page)
            self.assertIn("不自动视为当前平台复核", page)
            entry["observed_at"] = "2026-10-24T10:00:00"
            self.save(root, data)
            self.assertFalse(progress.check(root)["valid"])

    def test_rejected_input_keeps_generated_outputs_and_check_does_not_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, manifest = self.fixture(root)
            progress.build(root)
            names = ("PROGRESS.md", "progress/summary.json")
            before = {n: (root / n).read_bytes() for n in names}
            data["batches"][0]["stages"]["prepared"]["count"] = -1
            self.save(root, data)
            with self.assertRaises(ValueError):
                progress.build(root)
            self.assertEqual(before, {n: (root / n).read_bytes() for n in names})
            self.assertFalse(progress.check(root)["valid"])
            self.assertEqual(before, {n: (root / n).read_bytes() for n in names})


if __name__ == "__main__":
    unittest.main()
