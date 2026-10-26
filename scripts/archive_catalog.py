#!/usr/bin/env python3
"""Build a public metadata catalogue without publishing source media or transcripts.

All source paths and persistent accession allocation live in --local-state, which
must be outside --output. Re-running the same inventory preserves entity IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

# Support both CLI execution and the existing importlib-based test harness.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog_transaction import staged_update

VERSION = 1
PREFIX = {"project": "P", "collection": "C", "event": "E", "asset": "V", "file": "F", "subtitle": "S", "publication": "U", "checksum": "H"}
VIDEO_EXTS = {".mp4", ".mov", ".mts", ".m2ts", ".mpeg", ".mpg", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".ts", ".3gp", ".webm"}
TABLES = tuple(k + "s" for k in PREFIX if k != "publication") + ("publications",)
REFS = {"project_id": "projects", "project_ids": "projects", "collection_id": "collections", "event_ids": "events", "asset_id": "assets", "asset_ids": "assets", "file_id": "files", "subtitle_ids": "subtitles", "derived_from_file_id": "files", "youtube_publication_ids": "publications", "source_file_id": "files", "source_asset_id": "assets", "canonical_byte_source_asset_id": "assets", "canonical_byte_source_file_id": "files", "byte_copy_asset_ids": "assets", "alternate_representation_candidate_of_asset_id": "assets", "public_caption_subtitle_ids": "subtitles", "equivalent_public_export_subtitle_id": "subtitles", "platform_subtitle_ids": "subtitles"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def pathkey(path):
    return str(PureWindowsPath(str(path))).casefold()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def aware_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Timestamp must be an ISO 8601 datetime with timezone") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone offset")
    return stamp.isoformat()


def publication_summary(records):
    metadata_verified = [x for x in records if x.get("platform_readback_verification") == "verified" and x.get("verification_scope") in ("metadata_only", "metadata_and_caption_menu_with_limited_render_sample")]
    existing = [x for x in metadata_verified if x.get("publication_origin") == "pre_existing"]
    new = [x for x in records if x.get("new_upload_this_batch") is True and x.get("new_upload_completed") is True]
    return {
        "publication_associations_recorded": len(records),
        "publication_reports_recorded": sum(x.get("record_basis") == "operator_reported" for x in records),
        "platform_metadata_associations_verified": len(metadata_verified),
        "pre_existing_video_associations_verified": len(existing),
        "public_video_associations_verified": sum(x.get("visibility") == "public" for x in metadata_verified),
        "new_uploads_completed_this_batch": len(new),
        "youtube_uploads_recorded": len(new),
        "playback_verifications_recorded": sum(x.get("playback_status") == "verified" for x in records),
        "youtube_caption_verifications_recorded": sum(x.get("caption_publication_status") == "verified" for x in records),
        "new_caption_tracks_published_and_menu_verified": sum(t.get("published_this_update") is True and t.get("viewer_menu_selectable") is True for x in records for t in x.get("caption_tracks", [])),
        "legacy_platform_tracks_preserved": sum(t.get("legacy_platform_version") is True and t.get("unchanged_online") is True for x in records for t in x.get("caption_tracks", [])),
        "viewer_menu_verified_caption_tracks": sum(t.get("viewer_menu_selectable") is True for x in records for t in x.get("caption_tracks", [])),
        "caption_render_sample_observations": sum(len(x.get("caption_render_samples", [])) for x in records),
    }


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "registry.json"
        self.data = read_json(self.path) if self.path.exists() else {"schema_version": VERSION, "counters": {}, "entities": {}, "locations": {}}

    def get(self, kind, key, legacy_id=None):
        token = kind + "|" + key
        if token not in self.data["entities"]:
            n = self.data["counters"].get(kind, 0) + 1
            self.data["counters"][kind] = n
            self.data["entities"][token] = {"id": legacy_id or kind + ":" + str(uuid.uuid4()), "accession": "P4-" + PREFIX[kind] + f"{n:06d}"}
        entry = self.data["entities"][token]
        # Legacy IDs are aliases until an explicit reconciliation, never replace
        # an already published identifier during an unattended re-import.
        return {"id": entry["id"], "accession": entry["accession"]}

    def save(self):
        write_json(self.path, self.data)


def known_project(label, reviewed_titles=None):
    if reviewed_titles and label in reviewed_titles:
        title = reviewed_titles[label]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Reviewed public project title must be a nonempty string")
        return title
    for token in ("让时间过去", "外向的独白", "旋转楼梯", "玻璃动物园", "动物农场", "波菲利娅", "中间派", "紫色行动", "北风剧社旋涡", "死物骑士", "爱丽丝湖", "致命玩笑"):
        if token in label:
            return token
    if "3P" in label:
        return "3P小组"
    if "510" in label:
        return "510讨论出路"
    if "虫洞" in label and "相逢" in label:
        return "相逢（虫洞剧团记录批次）"
    if "相逢" in label:
        return "相逢（另一独立记录批次）"
    raise ValueError("New project requires a reviewed public title: " + label)


def label_date(text):
    matches = re.findall(r"(?<!\d)(20\d{2})[-_.年]?(0[1-9]|1[0-2])[-_.月]?([0-2]\d|3[01])(?!\d)", text)
    for y, m, d in matches:
        try:
            value = date(int(y), int(m), int(d)).isoformat()
            return {"value": value, "precision": "day", "basis": "source_folder_label", "verification": "unverified"}
        except ValueError:
            pass
    return {"value": None, "precision": "unknown", "basis": "not_established", "verification": "unverified"}


def event_type(label):
    if any(x in label for x in ("排练", "读剧本", "试读")):
        return "rehearsal"
    if any(x in label for x in ("公演", "演出", "展演", "表演")):
        return "performance"
    if any(x in label for x in ("讨论", "会议", "控诉")):
        return "discussion"
    if any(x in label for x in ("工作坊", "workshop")):
        return "workshop"
    return "unclassified"


def safe_camera_name(name):
    # Names outside these camera/time conventions stay in the private mapping.
    if re.fullmatch(r"(?i)(?:DJI|IMG|MVI|DSC|PXL)[_-][0-9A-Z_. -]+\.(?:mp4|mov|mts|m2ts|mpeg|mpg)", name):
        return name
    if re.fullmatch(r"(?:20\d{2}[-_ ]?\d{2}[-_ ]?\d{2})[0-9_. -]*\.[a-zA-Z0-9]+", name):
        return name
    return None


def srt_metadata(path):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "format_status": "encoding_needs_review", "cue_count": None}
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()) if text.strip() else []
    issues = []
    previous_end = 0
    for n, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or int(lines[0]) != n:
            issues.append("number_or_body")
            continue
        match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})", lines[1])
        if not match:
            issues.append("timecode")
            continue
        values = list(map(int, match.groups()))
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        if any(values[i] >= 60 for i in (1, 2, 5, 6)) or end <= start:
            issues.append("time_range")
        if start < previous_end:
            issues.append("overlap")
        previous_end = end
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "format_status": "empty_no_recognized_text" if not blocks else ("needs_review" if issues else "structure_passed"), "cue_count": len(blocks), "issue_counts": dict(Counter(issues))}


def reusable_hashes(audit, registry):
    result = {}
    for collection in audit["projects"]:
        if "玻璃动物园" not in collection["directory_label"]:
            continue
        job = Path(collection["project_root"]) / "_处理记录_20261026"
        dedup, manifest = job / "deduplication.json", job / "manifest.json"
        if not dedup.exists() or not manifest.exists():
            continue
        info = read_json(dedup)
        if digest(manifest) != info.get("manifest_sha256"):
            continue
        fingerprints = {pathkey(x["path"]): x for x in read_json(manifest)["sources"]}
        for group in info["groups"]:
            for item in group["members"]:
                source = Path(item)
                entry = fingerprints.get(pathkey(item))
                if not entry or not source.exists():
                    continue
                stat = source.stat()
                if stat.st_size == group["bytes"] == entry["bytes"] and stat.st_mtime_ns == entry["mtime_ns"]:
                    result[pathkey(item)] = {"sha256": group["sha256"], "basis": "prior_full_sha256_with_unchanged_size_and_mtime", "verified_at": info["verified_at"]}
    cached = registry.directory / "hash_results.json"
    if cached.exists():
        for key, value in read_json(cached).get("files", {}).items():
            source = Path(value["path"])
            if source.exists() and source.stat().st_size == value["bytes"] and source.stat().st_mtime_ns == value["mtime_ns"]:
                result[key] = {"sha256": value["sha256"], "basis": "full_sha256_with_unchanged_size_and_mtime", "verified_at": value["verified_at"]}
    return result


def _import_catalog(args):
    out, state = Path(args.output).resolve(), Path(args.local_state).resolve()
    if (out / ".p4-public-scope.json").exists():
        raise ValueError("Direct import into a scope-projected public tree is blocked; import into the private master and run project_public_scope.py")
    if out == state or out in state.parents:
        raise ValueError("Private local state must be outside public output")
    audit = read_json(args.audit)
    registry = Registry(state)
    hashes = reusable_hashes(audit, registry)
    quality = read_json(args.quality).get("records", []) if args.quality else []
    probe_cache = read_json(state / "probe_results.json").get("files", {}) if (state / "probe_results.json").exists() else {}
    repaired = {}
    originals = {}
    for record in quality:
        if record.get("type") == "repair_validation":
            for item in record["files"]:
                repaired[pathkey(item["preferred_playback"])] = item
                originals[pathkey(item["original"])] = item
    crosswalk = read_json(args.crosswalk) if args.crosswalk and Path(args.crosswalk).exists() else {}
    reviewed_titles = read_json(args.project_titles) if getattr(args, "project_titles", None) else {}
    tables = {name: [] for name in TABLES}
    previous = {name: read_json(out / "catalog" / (name + ".json"))["items"] if (out / "catalog" / (name + ".json")).exists() else [] for name in TABLES}
    project_by_title = {}
    event_by_key = {}
    subtitle_by_path = {}
    file_by_path = {}
    pending_derivation = []
    private = {"schema_version": VERSION, "files": {}, "subtitles": {}, "collections": {}, "events": {}}
    # Explicit mapping input is intentionally narrow. Unknown legacy formats are
    # retained privately for reconciliation, never guessed into canonical IDs.
    legacy_projects = crosswalk.get("projects", []) if isinstance(crosswalk, dict) else []
    if isinstance(legacy_projects, dict):
        legacy_projects = list(legacy_projects.values())
    for collection in audit["projects"]:
        title = known_project(collection["directory_label"], reviewed_titles)
        try:
            category = known_project(collection["directory_label"])
        except ValueError:
            category = title
        project_keys = registry.data.setdefault("project_keys_by_root", {})
        project_key = project_keys.setdefault(pathkey(collection["project_root"]), title)
        if project_key not in project_by_title:
            matches = [p for p in legacy_projects if isinstance(p, dict) and p.get("public_title", p.get("title")) == title and p.get("canonical_id")]
            legacy = matches[0]["canonical_id"] if len(matches) == 1 else None
            p = {**registry.get("project", project_key, legacy), "kind": "project", "title": title, "classification_status": "candidate_from_archive_inventory", "legacy_reconciliation": "matched_existing_id" if legacy else "pending", "aliases": [legacy] if legacy else [], "date": {"value": None, "precision": "unknown", "basis": "not_established"}}
            project_by_title[project_key] = p
            tables["projects"].append(p)
        project = project_by_title[project_key]
        links = [x for x in crosswalk.get("directory_crosswalk", []) if x.get("local_directory_label") == collection["directory_label"] and x.get("existing_project_id")]
        if links:
            linked = links[0]
            project["legacy_reconciliation"] = "existing_registry_project_identified_from_directory_label"
            project["existing_registry_identifiers"] = [{"namespace": linked["existing_project_namespace"], "value": linked["existing_project_id"], "mapping_basis": "directory_title_match_only", "asset_membership_verified": False}]
            old = next((x for x in crosswalk.get("existing_projects", []) if x["id"] == linked["existing_project_id"]), {})
            for code in old.get("legacy_project_codes", []):
                if code["value"] not in project["aliases"]:
                    project["aliases"].append(code["value"])
            url = old.get("linked_project_page_document", {}).get("url")
            if url:
                project["related_public_project_page"] = url
        root = Path(collection["project_root"])
        c = {**registry.get("collection", pathkey(root)), "kind": "collection", "title": title + " · 记录批次", "project_id": project["id"], "date": label_date(collection["directory_label"]), "inventory_file_count": len(collection["videos"]), "classification_status": "candidate"}
        tables["collections"].append(c)
        private["collections"][c["id"]] = {"path": str(root), "original_label": collection["directory_label"]}
        for video in collection["videos"]:
            source = Path(video["path"])
            stat = source.stat()
            key = pathkey(source)
            folder_key = pathkey(source.parent)
            if folder_key not in event_by_key:
                phase = event_type(str(source.parent.relative_to(root)) + " " + collection["directory_label"])
                e = {**registry.get("event", folder_key), "kind": "event", "title": title + " · " + {"rehearsal": "排练", "performance": "演出", "discussion": "讨论", "workshop": "工作坊", "unclassified": "待辨识事件"}[phase], "project_ids": [project["id"]], "collection_id": c["id"], "event_type": phase, "classification_status": "provisional_folder_group", "date": label_date(str(source.parent.relative_to(root)) + " " + collection["directory_label"]), "description": "按来源目录建立的暂定事件组；需结合录像与外部资料复核事件边界。"}
                event_by_key[folder_key] = e
                tables["events"].append(e)
                private["events"][e["id"]] = {"path": str(source.parent)}
            event = event_by_key[folder_key]
            asset_identity = registry.get("asset", key)
            file_identity = registry.get("file", key + f"|{stat.st_size}|{stat.st_mtime_ns}")
            classification, hold = "project_candidate", "pending_content_and_publication_review"
            relative = video["relative_path"].replace("\\", "/")
            if category == "紫色行动" and relative.casefold().startswith("camera2/"):
                classification, hold = "mixed_personal_and_project_material", "hold_private_content_triage"
            elif "奈宝视频" in relative:
                classification, hold = "third_party_reference_material", "hold_source_and_publication_review"
            elif category == "玻璃动物园" and "荧火空间" in relative:
                classification, hold = "mixed_project_context", "hold_project_assignment_review"
            if video["bytes"] == 0:
                hold = "hold_zero_byte_source"
            elif key in originals:
                hold = "hold_damaged_original_use_preferred_derivative"
            elif video["project_media_copy_candidate"]:
                hold = "hold_project_copy_candidate"
            elif video["derived_candidate"] and key not in repaired:
                hold = "hold_derivative_relationship_review"
            asset = {**asset_identity, "kind": "asset", "title": "视频 " + asset_identity["accession"], "project_ids": [project["id"]], "collection_id": c["id"], "event_ids": [event["id"]], "file_id": file_identity["id"], "content_classification": classification, "publication_status": hold, "human_review": "not_recorded", "rights_status": "not_established", "youtube_publication_ids": [], "subtitle_ids": []}
            if classification == "mixed_project_context":
                if source.stem in ("DJI_0002", "DJI_0005"):
                    asset["project_assignment_note"] = "目录内既有内容索引提示与《动物农场》招募或试演相关，需逐条复核。"
                    asset["related_project_titles"] = ["动物农场"]
                elif source.stem in ("DJI_0006", "DJI_0007", "DJI_0008", "DJI_0009"):
                    asset["project_assignment_note"] = "目录内既有内容索引提示与《玻璃动物园》排练相关，需逐条复核。"
            f = {**file_identity, "kind": "file", "title": file_identity["accession"], "asset_id": asset["id"], "extension": video["extension"].lower(), "bytes": video["bytes"], "sha256": hashes.get(key, {}).get("sha256"), "checksum_status": "recorded_full_sha256" if key in hashes else "pending_full_sha256", "storage_status": "local_master_location_recorded_privately", "media_probe_status": "not_newly_probed", "full_decode_status": "not_recorded", "duration_seconds": None, "human_continuous_watch": "not_recorded", "derivative_candidate": video["derived_candidate"], "project_copy_candidate": video["project_media_copy_candidate"]}
            safe_name = safe_camera_name(source.name)
            if safe_name:
                f["camera_filename"] = safe_name
            if key in repaired:
                q = repaired[key]
                f.update(full_decode_status="passed_existing_report" if q["full_decode_exit"] == 0 else "failed_existing_report", duration_seconds=float(q["duration_seconds"]), media_probe_status="readable_existing_report")
                asset["publication_status"] = "pending_content_and_publication_review"
                pending_derivation.append((f, pathkey(q["original"])))
            prior_probe = probe_cache.get(key)
            if prior_probe and prior_probe["bytes"] == stat.st_size and prior_probe["mtime_ns"] == stat.st_mtime_ns:
                f["media_probe_status"] = prior_probe["status"]
                f["media_probe_scope"] = "ffprobe_metadata_only_not_full_decode"
                f["metadata_probe_at"] = prior_probe["verified_at"]
                f["duration_seconds"] = prior_probe.get("duration_seconds")
                f["streams"] = prior_probe.get("streams", [])
            file_by_path[key] = f
            private["files"][f["id"]] = {"path": str(source), "original_filename": source.name, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "asset_id": asset["id"]}
            tables["files"].append(f)
            tables["assets"].append(asset)
            for suffix, lang, role in ((".srt", "und", "original_asr"), (".en.srt", "en", "machine_translation"), (".zh-en.srt", "zh-en", "bilingual_machine_translation")):
                candidates = [source.with_suffix(suffix), Path(str(source) + suffix)]
                caption = next((p for p in candidates if p.exists()), None)
                if caption is None:
                    continue
                caption_key = pathkey(caption)
                if caption_key not in subtitle_by_path:
                    meta = srt_metadata(caption)
                    s = {**registry.get("subtitle", caption_key + "|" + meta["sha256"]), "kind": "subtitle", "title": "字幕记录", "asset_ids": [], "language": lang, "role": role, "clock": "source_zero", "version_sha256": meta.pop("sha256"), **meta, "human_listening_review": "not_recorded", "public_text_status": "withheld_pending_review", "youtube_caption_status": "not_uploaded_in_this_catalogue", "pairing_status": "filename_association_requires_review"}
                    subtitle_by_path[caption_key] = s
                    tables["subtitles"].append(s)
                    private["subtitles"][s["id"]] = {"path": str(caption)}
                s = subtitle_by_path[caption_key]
                s["asset_ids"].append(asset["id"])
                asset["subtitle_ids"].append(s["id"])
        # Existing edited caption products are version records too. They are
        # registered without copying text and without guessing an asset pairing.
        edited = root / "字幕成片输出"
        if edited.exists():
            for caption in sorted(edited.rglob("*")):
                if caption.suffix.lower() not in {".srt", ".ass"} or pathkey(caption) in subtitle_by_path:
                    continue
                if caption.suffix.lower() == ".srt":
                    meta = srt_metadata(caption)
                else:
                    raw = caption.read_bytes()
                    meta = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "format_status": "not_parsed_ass", "cue_count": None}
                role = "bilingual_machine_translation" if "bilingual" in caption.name else ("machine_translation" if ".en." in caption.name else "original_asr")
                lang = "zh-en" if "bilingual" in caption.name else ("en" if ".en." in caption.name else "zh")
                s = {**registry.get("subtitle", pathkey(caption) + "|" + meta["sha256"]), "kind": "subtitle", "title": "既有字幕成片版本记录", "asset_ids": [], "project_id": project["id"], "language": lang, "role": role, "format": caption.suffix.lower().lstrip("."), "clock": "source_zero", "version_sha256": meta.pop("sha256"), **meta, "human_listening_review": "not_recorded", "public_text_status": "withheld_pending_review", "youtube_caption_status": "not_uploaded_in_this_catalogue", "pairing_status": "edited_product_pairing_pending"}
                subtitle_by_path[pathkey(caption)] = s
                tables["subtitles"].append(s)
                private["subtitles"][s["id"]] = {"path": str(caption)}
    for f, original_key in pending_derivation:
        if original_key in file_by_path:
            f["derived_from_file_id"] = file_by_path[original_key]["id"]
    groups = defaultdict(list)
    for key, f in file_by_path.items():
        if f["sha256"]:
            h = {**registry.get("checksum", f["id"] + "|" + f["sha256"]), "kind": "checksum", "title": "SHA-256", "file_id": f["id"], "algorithm": "sha256", "value": f["sha256"], "verification_basis": hashes[key]["basis"], "verified_at": hashes[key]["verified_at"]}
            tables["checksums"].append(h)
            groups[f["sha256"]].append(f["id"])
    byte_groups = [{"sha256": sha, "file_ids": ids, "meaning": "byte_identical_candidate_only_semantic_entities_not_merged"} for sha, ids in groups.items() if len(ids) > 1]
    # Keep published history and human annotations when a fresh machine inventory
    # is imported. Current source absence is not a request to erase an entity.
    protected_fields = {
        'assets': ('human_review', 'rights_status', 'youtube_publication_ids', 'publication_status', 'youtube_metadata_association_status', 'platform_subtitle_ids', 'version_history', 'relation_history', 'prior_version_review', 'prior_version_content_scope_review'),
        'subtitles': ('human_listening_review', 'public_text_status', 'public_export', 'youtube_caption_status', 'pairing_status', 'pairing_review', 'source_file_id', 'source_asset_id', 'content_scope_review', 'equivalent_public_export_subtitle_id', 'quality_label', 'platform_track_observations', 'platform_track_observation', 'platform_origin', 'platform_video_id', 'platform_language_label', 'platform_track_actual_name', 'existing_platform_label_is_not_review_evidence', 'equivalent_export_history'),
        'files': ('human_continuous_watch', 'full_decode_status', 'sha256', 'checksum_status', 'independent_checksum_evidence'),
    }
    for name, items in tables.items():
        older = {x["id"]: x for x in previous[name]}
        for item in items:
            item["in_current_inventory"] = True
            old = older.pop(item["id"], None)
            if old:
                if name == "projects":
                    item["aliases"] = list(dict.fromkeys(item.get("aliases", []) + old.get("aliases", [])))
                    if old.get("official_registry_references"):
                        item["official_registry_references"] = old["official_registry_references"]
                if name == "assets":
                    reviewed_fields = ("content_scope_review", "project_assignment_review", "project_ids", "content_classification", "canonical_byte_source_asset_id", "canonical_byte_source_file_id", "byte_copy_asset_ids", "byte_identity_relation", "alternate_representation_candidate_of_asset_id", "alternate_representation_review", "public_caption_subtitle_ids", "official_project_association_pending", "official_project_context_candidates", "source_collection_assignment_role", "content_role", "original_p4_event_recording_confirmed")
                    if old.get("content_scope_review") or old.get("byte_identity_relation") or old.get("alternate_representation_review") or old.get("official_project_context_candidates") or old.get("content_role"):
                        for field in reviewed_fields:
                            if field in old:
                                item[field] = old[field]
                if name == "events" and old.get("content_scope_review"):
                    for field in ("content_scope_review", "project_ids", "title", "classification_status", "event_type", "description"):
                        if field in old:
                            item[field] = old[field]
                for field in protected_fields.get(name, ()):
                    value = old.get(field)
                    if name == "files" and field == "sha256" and value is not None and item.get("sha256") is not None and value != item["sha256"]:
                        raise ValueError("Conflicting complete hashes for an unchanged source fingerprint")
                    if value is not None and value not in ("not_recorded", "not_established", "not_newly_probed", "not_uploaded_in_this_catalogue", "withheld_pending_review", "filename_association_requires_review", "edited_product_pairing_pending", "pending_content_and_publication_review") and not (isinstance(value, str) and value.startswith("hold_")):
                        item[field] = value
                if name == "assets" and old.get("file_id") != item.get("file_id"):
                    history = item.setdefault("version_history", [])
                    if not any(x.get("file_id") == old.get("file_id") for x in history):
                        fields = ("file_id", "human_review", "content_scope_review", "project_assignment_review", "content_classification", "canonical_byte_source_asset_id", "canonical_byte_source_file_id", "byte_identity_relation", "alternate_representation_candidate_of_asset_id", "alternate_representation_review", "public_caption_subtitle_ids")
                        history.append({k: old[k] for k in fields if k in old})
                    item["prior_version_review"] = {"file_id": old.get("file_id"), "human_review": old.get("human_review", "not_recorded")}
                    item["human_review"] = "needs_review_source_version_changed"
                    if old.get("youtube_publication_ids"):
                        item["publication_status"] = "previous_version_publication_recorded"
                        item["youtube_metadata_association_status"] = "previous_file_version_only"
                    if old.get("content_scope_review"):
                        item["prior_version_content_scope_review"] = old["content_scope_review"]
                        item["content_scope_review"] = {"status": "needs_review_source_version_changed", "source_file_id": item["file_id"], "prior_source_file_id": old.get("file_id"), "human_listening_review": "not_recorded"}
                    if old.get("project_assignment_review"):
                        item["project_assignment_review"] = {"status": "needs_review_source_version_changed", "source_file_id": item["file_id"], "prior_source_file_id": old.get("file_id")}
                    item.pop("alternate_representation_candidate_of_asset_id", None)
                    item.pop("alternate_representation_review", None)
                    item.pop("public_caption_subtitle_ids", None)
        for item in older.values():
            item["in_current_inventory"] = False
            items.append(item)
    for pub in tables["publications"]:
        pub["in_current_inventory"] = True
    _refresh_version_relations(tables, previous["assets"])
    subtitle_map = {s["id"]: s for s in tables["subtitles"]}
    for asset in tables["assets"]:
        for sid in asset.get("platform_subtitle_ids", []):
            subtitle = subtitle_map.get(sid)
            if subtitle and subtitle.get("source_file_id") == asset["file_id"] and sid not in asset["subtitle_ids"]:
                asset["subtitle_ids"].append(sid)
    for name, items in tables.items():
        write_json(out / "catalog" / (name + ".json"), {"schema_version": VERSION, "items": items})
    write_json(out / "catalog" / "byte-equality-groups.json", {"schema_version": VERSION, "items": byte_groups})
    current_files = [x for x in tables["files"] if x["in_current_inventory"]]
    summary = {"schema_version": VERSION, "inventory_snapshot_at": audit["generated_at"], "public_export_policy": "metadata_only_no_video_or_unreviewed_transcript_text", "counts": {name: len(items) for name, items in tables.items()}, "current_inventory_counts": {name: sum(x["in_current_inventory"] for x in items) for name, items in tables.items()}, "video_bytes": sum(x["bytes"] for x in current_files), "full_sha256_recorded": sum(f["sha256"] is not None for f in current_files), "full_sha256_pending": sum(f["sha256"] is None for f in current_files), "full_decode_passed_existing_reports": sum(f["full_decode_status"] == "passed_existing_report" for f in current_files), "subtitle_format_status": dict(Counter(x["format_status"] for x in tables["subtitles"])), "publication_reports_recorded": len(tables["publications"]), "youtube_uploads_recorded": sum(x.get("platform_readback_verification") == "verified" for x in tables["publications"]), "human_review_status": "not_recorded", "scope": "18 selected theatre/event collections; personal/system backup roots excluded; collection inclusion does not establish publication clearance"}
    summary["public_caption_export_count"] = sum(bool(x.get("public_export")) for x in tables["subtitles"])
    summary.update(publication_summary(tables["publications"]))
    if summary["public_caption_export_count"]:
        summary["public_export_policy"] = "metadata_and_explicit_scope_reviewed_machine_draft_captions_no_video"
        summary["public_caption_source_file_count"] = len({x["source_file_id"] for x in tables["subtitles"] if x.get("public_export") and x.get("source_file_id")})
        summary["public_caption_quality"] = "machine_draft_not_full_listening_review"
    if (out / "registry" / "official-projects.json").exists():
        summary["official_public_registry_count"] = len(read_json(out / "registry" / "official-projects.json")["items"])
    write_json(out / "catalog" / "summary.json", summary)
    if (state / "mapping.json").exists():
        old_private = read_json(state / "mapping.json")
        for name in ("files", "subtitles", "collections", "events"):
            private[name] = {**old_private.get(name, {}), **private[name]}
    write_json(state / "mapping.json", private)
    write_json(state / "hash_plan.json", {"schema_version": VERSION, "files": [{"file_id": f["id"], "accession": f["accession"], **private["files"][f["id"]]} for f in current_files if f["sha256"] is None]})
    registry.save()
    render_indexes(out, tables, summary)
    return summary


def _refresh_version_relations(tables, previous_assets):
    """Keep byte-copy assertions only when the current file versions prove them."""
    assets = {x["id"]: x for x in tables["assets"]}
    files = {x["id"]: x for x in tables["files"]}
    older = {x["id"]: x for x in previous_assets}
    for asset in tables["assets"]:
        canonical_id = asset.get("canonical_byte_source_asset_id")
        if not canonical_id:
            continue
        canonical = assets.get(canonical_id)
        copied = files.get(asset.get("file_id"))
        original = files.get(canonical.get("file_id")) if canonical else None
        valid = (copied and original and copied.get("sha256") and copied.get("sha256") == original.get("sha256") and copied.get("asset_id") == asset["id"] and original.get("asset_id") == canonical_id)
        if valid:
            asset["canonical_byte_source_file_id"] = original["id"]
            continue
        old = older.get(asset["id"], asset)
        record = {k: old[k] for k in ("file_id", "canonical_byte_source_asset_id", "canonical_byte_source_file_id", "byte_identity_relation") if k in old}
        record["invalidation_reason"] = "current_file_versions_no_longer_prove_byte_identity"
        history = asset.setdefault("relation_history", [])
        if record not in history:
            history.append(record)
        for key in ("canonical_byte_source_asset_id", "canonical_byte_source_file_id", "byte_identity_relation"):
            asset.pop(key, None)
    for asset in tables["assets"]:
        copies = [x["id"] for x in tables["assets"] if x.get("canonical_byte_source_asset_id") == asset["id"]]
        if copies:
            asset["byte_copy_asset_ids"] = copies
        else:
            asset.pop("byte_copy_asset_ids", None)
    subtitles = {x["id"]: x for x in tables["subtitles"]}
    for sub in tables["subtitles"]:
        equivalent = subtitles.get(sub.get("equivalent_public_export_subtitle_id"))
        if not equivalent:
            continue
        valid = any(assets.get(aid, {}).get("canonical_byte_source_asset_id") == equivalent.get("source_asset_id") for aid in sub.get("asset_ids", []))
        if not valid:
            sub.setdefault("equivalent_export_history", []).append(sub.pop("equivalent_public_export_subtitle_id"))


def import_catalog(args):
    if (Path(args.output) / ".p4-public-scope.json").exists():
        raise ValueError("Direct import into a scope-projected public tree is blocked; import into the private master and run project_public_scope.py")
    def transform(public, private):
        values = {**vars(args), "output": str(public), "local_state": str(private)}
        return _import_catalog(SimpleNamespace(**values))
    summary = staged_update(args.output, transform, validate, local_state=args.local_state)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def render_indexes(out, tables, summary):
    counts = summary["counts"]
    lines = ["# P4 项目与素材目录", "", f"当前登记 {counts['projects']} 个候选项目、{counts['collections']} 个来源批次、{counts['events']} 个暂定事件组、{counts['files']} 个视频文件位置。", "", "编号为长期引用别名，实体主键为 canonical ID。来源目录与原始文件名的私有映射保存在维护端；公开目录不含未审核台词或原片。", "", "| 项目编号 | 项目 | 文件数 | 事件组 |", "|---|---|---:|---:|"]
    if (out / "registry" / "official-projects.json").exists():
        registry = read_json(out / "registry" / "official-projects.json")
        lines[2:2] = [f"另见 [官方公开项目索引快照](registry/README.md)：当前 {len(registry['items'])} 条公开项目条目。该注册表与本批本地视频目录分开；数量不能相加或视为全部历史项目总数。", ""]
    if "private_master_inventory_video_files" in summary:
        lines[2:2] = [f"私有主盘点保留 {summary['private_master_inventory_video_files']} 个视频文件；当前公开候选投影为 {summary['public_selected_video_files']} 个，排除 {summary['public_excluded_video_files']} 个。候选目录不是上传授权；正文只按官方精确来源白名单公开。", ""]
    for p in tables["projects"]:
        assets = [a for a in tables["assets"] if p["id"] in a["project_ids"]]
        events = [e for e in tables["events"] if p["id"] in e["project_ids"]]
        lines.append(f"| {p['accession']} | [{p['title']}](projects/{p['accession']}/index.md) | {len(assets)} | {len(events)} |")
        page = [f"# {p['title']}", "", f"项目编号：`{p['accession']}`  ", f"Canonical ID：`{p['id']}`  ", f"既有档案 ID 对齐：`{p['legacy_reconciliation']}`", "", "本页按暂定事件组编目。日期只保留目录中明确的年月日标记，均待核实；未从修改时间推断。公开字幕另有文本范围筛查记录，均标为机器稿；文本范围筛查不能替代全片观看或逐句听校。", "", "[公开字幕目录](../../captions/README.md)（已开放的版本提供直达链接）。", ""]
        if p.get("aliases"):
            page[6:6] = ["既有或官方编号别名：" + "、".join("`" + x + "`" for x in p["aliases"]) + "。", ""]
        if p.get("official_registry_references"):
            page += ["官方索引：" + " / ".join("[" + x["official_project_code"] + "](" + x["project_url_zh"] + ")" for x in p["official_registry_references"]) + "。此项目级目录对照不证明各视频的内容归属。", ""]
        files = {f["id"]: f for f in tables["files"]}
        subtitles = {s["id"]: s for s in tables["subtitles"]}
        all_assets = {a["id"]: a for a in tables["assets"]}
        publications = {u["id"]: u for u in tables["publications"]}
        for event in events:
            page += [f"## {event['accession']} · {event['title']}", "", "日期：" + (event["date"]["value"] or "尚未确定") + "；事件边界：暂定目录组。", "", "| 视频编号 | 文件编号 | 格式 | 大小 MB | 字幕版本 | 公开字幕 | 已关联公开视频 | 完整解码 | 内容与发布待办 |", "|---|---|---|---:|---:|---|---|---|---|"]
            for a in assets:
                if event["id"] not in a["event_ids"]:
                    continue
                f = files[a["file_id"]]
                source_asset = all_assets.get(a.get("canonical_byte_source_asset_id"), a)
                available = [subtitles[sid] for sid in source_asset.get("subtitle_ids", []) if sid in subtitles and subtitles[sid].get("public_export") and subtitles[sid].get("source_file_id", source_asset["file_id"]) == source_asset["file_id"]]
                labels = {"original_asr": "原语", "machine_translation": "English", "bilingual_machine_translation": "中英", "platform_original_language_legacy": "旧平台版（审校未知）"}
                links = " / ".join("[" + labels.get(s["role"], s["language"]) + "](../../" + s["public_export"]["path"] + ")" for s in available) or "待公开"
                if source_asset["id"] != a["id"]:
                    links += "；同字节来源 " + source_asset["accession"]
                public_videos = [publications[pid] for pid in source_asset.get("youtube_publication_ids", []) if pid in publications and publications[pid].get("platform_readback_verification") == "verified" and publications[pid].get("visibility") == "public" and publications[pid].get("file_id") == source_asset["file_id"]]
                video_links = " / ".join("[YouTube](" + u["url"] + ")" for u in public_videos) or "未建立已核验关联"
                if public_videos and source_asset["id"] != a["id"]:
                    video_links += "（规范来源）"
                affiliation = "官网来源已对应" if a.get("official_media_affiliation_status") == "confirmed_official_source_and_project_context" else "官网逐项关联待核实"
                page.append(f"| {a['accession']} | {f['accession']} | {f['extension']} | {f['bytes'] / 1_000_000:.2f} | {len(a['subtitle_ids'])} | {links} | {video_links} | {f['full_decode_status']} | {a['publication_status']}; {affiliation} |")
            page.append("")
        page += ["[返回总目录](../../INDEX.md)", ""]
        target = out / "projects" / p["accession"] / "index.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(page), encoding="utf-8")
    lines += ["", "[公开字幕与翻译目录](captions/README.md)：按源素材和版本下载原语、英文、中英双语机器稿。", "", "机器可读数据：[目录摘要](catalog/summary.json)、[项目](catalog/projects.json)、[事件](catalog/events.json)、[视频](catalog/assets.json)、[文件版本](catalog/files.json)、[字幕记录](catalog/subtitles.json)、[YouTube 发布记录](catalog/publications.json)。", ""]
    if tables["publications"]:
        lines += ["[已关联的既有 YouTube 视频](publications/README.md)：记录元数据回读范围，播放与线上字幕检查单独保留状态。", ""]
    (out / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    from export_identity_crosswalk import build as build_identity_crosswalk
    build_identity_crosswalk(out)
    # Private intake masters may retain older caption annotations without a
    # public scope policy. Only the explicit projection creates public reading.
    if (out / ".p4-public-scope.json").exists() or not any(s.get("public_export") for s in tables["subtitles"]):
        from public_reading import build as build_public_reading
        build_public_reading(out)


def _load_validatable_tables(root):
    """Enforce the declared catalogue shapes before traversing relationships.

    These are the catalogue's explicit contracts, not a general JSON Schema
    implementation. Additional editorial/provenance fields remain allowed.
    """
    tables, errors = {}, []
    required = {"assets": ("project_ids", "collection_id", "event_ids", "file_id", "subtitle_ids"), "files": ("asset_id", "bytes", "sha256"), "subtitles": ("asset_ids", "version_sha256", "bytes", "language", "role", "clock"), "collections": ("project_id",), "events": ("project_ids", "collection_id"), "checksums": ("file_id", "algorithm", "value"), "publications": ("asset_id", "file_id", "subtitle_ids")}
    for name in TABLES:
        try:
            table = read_json(root / "catalog" / (name + ".json"))
        except (OSError, ValueError) as exc:
            errors.append("Cannot read catalogue table " + name + ": " + type(exc).__name__)
            tables[name] = []
            continue
        if not isinstance(table, dict) or type(table.get("schema_version")) is not int or table.get("schema_version") != VERSION or not isinstance(table.get("items"), list):
            errors.append("Invalid table shape or schema_version: " + name)
            tables[name] = []
            continue
        if set(table) != {"schema_version", "items"}:
            errors.append("Unexpected catalogue table properties: " + name)
        tables[name] = table["items"]
        expected_kind = "publication" if name == "publications" else name[:-1]
        for n, item in enumerate(table["items"]):
            label = name + "[" + str(n) + "]"
            if not isinstance(item, dict):
                errors.append("Entity must be an object: " + label)
                continue
            for field in ("id", "accession", "kind", "title"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append("Entity field must be a nonempty string: " + label + ":" + field)
            if item.get("kind") != expected_kind:
                errors.append("Entity kind does not match its table: " + label)
            for field in required.get(name, ()):
                if field not in item:
                    errors.append("Required entity field missing: " + label + ":" + field)
            for field in REFS:
                if field not in item:
                    continue
                value = item[field]
                valid = isinstance(value, list) and all(isinstance(x, str) and x for x in value) if field.endswith("_ids") else isinstance(value, str) and bool(value)
                if not valid:
                    errors.append("Invalid reference field type: " + label + ":" + field)
            for field in ("language", "role"):
                if field in item and (not isinstance(item[field], str) or not item[field]):
                    errors.append("Invalid subtitle string field: " + label + ":" + field)
            if "bytes" in item and (type(item["bytes"]) is not int or item["bytes"] < 0):
                errors.append("File/subtitle bytes must be a nonnegative integer: " + label)
            for field in ("sha256", "version_sha256"):
                if field in item and not (field == "sha256" and item[field] is None) and (not isinstance(item[field], str) or not re.fullmatch(r"[a-f0-9]{64}", item[field])):
                    errors.append("Invalid SHA-256 field: " + label + ":" + field)
            if "clock" in item and item["clock"] != "source_zero":
                errors.append("Invalid source clock: " + label)
            if "rights_status" in item and item["rights_status"] not in ("not_established", "documented"):
                errors.append("Invalid rights_status: " + label)
            if "in_current_inventory" in item and type(item["in_current_inventory"]) is not bool:
                errors.append("Invalid inventory presence flag: " + label)
            duration = item.get("duration_seconds")
            if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
                errors.append("Invalid media duration: " + label)
            for field in ("content_scope_review", "public_export"):
                if field in item and not isinstance(item[field], dict):
                    errors.append("Invalid metadata object: " + label + ":" + field)
            if name == "publications":
                for field in ("channel_id", "youtube_video_id", "url", "record_basis", "source_sha256", "description_source_sha256"):
                    if field in item and not isinstance(item[field], str):
                        errors.append("Invalid publication string field: " + label + ":" + field)
                receipt = item.get("observation_receipt")
                if receipt is not None and (not isinstance(receipt, dict) or not isinstance(receipt.get("sha256"), str)):
                    errors.append("Invalid publication receipt: " + label)
            review = item.get("content_scope_review")
            if isinstance(review, dict) and "status" in review and not isinstance(review["status"], str):
                errors.append("Invalid review status: " + label)
            export = item.get("public_export")
            if isinstance(export, dict) and not all(isinstance(export.get(k), str) and export[k] for k in ("path", "sha256", "scope_review_status", "quality_label")):
                errors.append("Incomplete public caption export: " + label)
    return tables, errors


def validate(root):
    root = Path(root)
    tables, errors = _load_validatable_tables(root)
    if errors:
        return {"valid": False, "entity_count": sum(len(v) for v in tables.values()), "counts": {k: len(v) for k, v in tables.items()}, "errors": errors}
    all_items = [item for items in tables.values() for item in items]
    ids, accessions = set(), set()
    for item in all_items:
        if not all(k in item for k in ("id", "accession", "kind", "title")):
            errors.append("Entity missing common fields")
        if item["id"] in ids or item["accession"] in accessions:
            errors.append("Duplicate identity or accession: " + item["accession"])
        ids.add(item["id"])
        accessions.add(item["accession"])
        if item["kind"] not in PREFIX or not re.fullmatch("P4-" + PREFIX.get(item["kind"], "?") + r"\d{6}", item["accession"]):
            errors.append("Invalid kind/accession pairing: " + item["accession"])
    by_table = {name: {item["id"] for item in items} for name, items in tables.items()}
    refs = REFS
    for item in all_items:
        for key, target in refs.items():
            if key not in item:
                continue
            values = item[key] if isinstance(item[key], list) else [item[key]]
            for value in values:
                if value not in by_table[target]:
                    errors.append("Broken reference " + item["accession"] + ":" + key)
    for f in tables["files"]:
        if f["sha256"] is not None and not re.fullmatch(r"[a-f0-9]{64}", f["sha256"]):
            errors.append("Invalid SHA-256: " + f["accession"])
    files_by_id = {x["id"]: x for x in tables["files"]}
    for asset in tables["assets"]:
        current_file = files_by_id.get(asset["file_id"])
        if current_file and current_file.get("asset_id") != asset["id"]:
            errors.append("Current file belongs to another asset: " + asset["accession"])
    for checksum in tables["checksums"]:
        source = files_by_id.get(checksum["file_id"])
        if checksum["algorithm"] != "sha256" or not isinstance(checksum["value"], str) or not re.fullmatch(r"[a-f0-9]{64}", checksum["value"]) or (source and checksum["value"] != source.get("sha256")):
            errors.append("Checksum does not match its file version: " + checksum["accession"])
    seen_channel_videos = set()
    for pub in tables["publications"]:
        channel_video = (pub.get("channel_id"), pub.get("youtube_video_id"))
        if channel_video in seen_channel_videos:
            errors.append("Duplicate channel/video publication association: " + pub["accession"])
        seen_channel_videos.add(channel_video)
        if pub.get("channel_id") != "UCp_LyzepcrZX6Cgzv2Qit4w":
            errors.append("Unexpected archive YouTube channel: " + pub["accession"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", pub.get("youtube_video_id", "")):
            errors.append("Invalid YouTube video ID: " + pub["accession"])
        if pub.get("visibility") not in ("public", "unlisted", "private", "unknown"):
            errors.append("Invalid YouTube visibility: " + pub["accession"])
        linked_file = next((x for x in tables["files"] if x["id"] == pub.get("file_id")), None)
        if not linked_file or linked_file["asset_id"] != pub.get("asset_id"):
            errors.append("Publication source file/asset mismatch: " + pub["accession"])
        if pub.get("url") != "https://www.youtube.com/watch?v=" + pub.get("youtube_video_id", ""):
            errors.append("Publication URL/video-ID mismatch: " + pub["accession"])
        basis = pub.get("record_basis")
        if basis == "operator_reported":
            if pub.get("platform_readback_verification") != "not_recorded":
                errors.append("Publication report cannot assert platform verification: " + pub["accession"])
            try:
                aware_timestamp(pub.get("reported_observation_at"))
            except ValueError:
                errors.append("Invalid publication report timestamp: " + pub["accession"])
            if pub.get("reported_caption_status") != "not_recorded" and not pub.get("subtitle_ids"):
                errors.append("Reported caption status requires subtitle references: " + pub["accession"])
        elif basis == "youtube_studio_metadata_readback":
            source = next((x for x in tables["files"] if x["id"] == pub.get("file_id")), None)
            sha = pub.get("source_sha256")
            if pub.get("platform_readback_verification") != "verified" or pub.get("verification_scope") != "metadata_only":
                errors.append("Invalid Studio metadata verification scope: " + pub["accession"])
            if not source or not re.fullmatch(r"[a-f0-9]{64}", sha or "") or source.get("sha256") != sha or pub.get("description_source_sha256") != sha:
                errors.append("Studio source-description hash does not match catalogued source: " + pub["accession"])
            if pub.get("source_mapping_status") != "exact_description_source_sha256_match" or pub.get("uploaded_stream_hash_verified") is not False:
                errors.append("Overstated Studio source mapping: " + pub["accession"])
            if pub.get("publication_origin") != "pre_existing" or pub.get("new_upload_this_batch") is not False or pub.get("new_upload_completed") is not False:
                errors.append("Pre-existing mapping cannot count as a new upload: " + pub["accession"])
            if pub.get("playback_status") != "not_checked" or pub.get("caption_publication_status") != "not_checked" or pub.get("subtitle_ids"):
                errors.append("Metadata-only receipt cannot assert playback or caption verification: " + pub["accession"])
            receipt = pub.get("observation_receipt", {})
            if not re.fullmatch(r"[a-f0-9]{64}", receipt.get("sha256", "")) or receipt.get("interface") != "YouTube Studio videos and shorts table DOM":
                errors.append("Invalid Studio observation receipt provenance: " + pub["accession"])
            try:
                aware_timestamp(pub.get("recorded_at"))
                if pub.get("platform_observed_at") is not None:
                    aware_timestamp(pub["platform_observed_at"])
                elif pub.get("observation_timestamp_status") != "not_supplied_by_source_receipt":
                    raise ValueError("Missing observation timestamp status")
            except ValueError:
                errors.append("Invalid Studio observation/record timestamp: " + pub["accession"])
        elif basis == "youtube_studio_and_viewer_caption_observation":
            source = linked_file
            if pub.get("platform_readback_verification") != "verified" or pub.get("verification_scope") != "metadata_and_caption_menu_with_limited_render_sample":
                errors.append("Invalid caption observation scope: " + pub["accession"])
            if not source or pub.get("source_sha256") != source.get("sha256") or pub.get("publication_origin") != "pre_existing" or pub.get("new_upload_this_batch") is not False or pub.get("new_upload_completed") is not False:
                errors.append("Caption operation cannot become a new video upload: " + pub["accession"])
            if pub.get("playback_status") != "not_checked" or pub.get("caption_publication_status") != "published_menu_verified_partial_render_only":
                errors.append("Caption menu observations cannot assert full listening or playback: " + pub["accession"])
            proof = pub.get("official_source_evidence", {})
            if proof.get("source_sha256") != pub.get("source_sha256") or proof.get("youtube_video_id") != pub.get("youtube_video_id"):
                errors.append("Caption publication lacks matching official video/source evidence")
            receipt = pub.get("observation_receipt", {})
            if not re.fullmatch(r"[a-f0-9]{64}", receipt.get("sha256", "")) or receipt.get("interface") != "YouTube Studio caption editor and watch-player menu":
                errors.append("Caption observation receipt provenance is missing")
            try:
                aware_timestamp(pub.get("recorded_at"))
                if pub.get("platform_observed_at") is not None:
                    aware_timestamp(pub["platform_observed_at"])
            except ValueError:
                errors.append("Invalid caption observation timestamp")
            track_ids = {t.get("subtitle_id") for t in pub.get("caption_tracks", [])}
            if track_ids != set(pub.get("subtitle_ids", [])):
                errors.append("Caption observation track references disagree")
            for track in pub.get("caption_tracks", []):
                sub = next((s for s in tables["subtitles"] if s["id"] == track.get("subtitle_id")), None)
                if not sub or sub.get("version_sha256") != track.get("version_sha256") or track.get("human_line_by_line_listening_review") != "not_performed" or track.get("viewer_menu_selectable") is not True or track.get("studio_published") is not True:
                    errors.append("Caption observation version or review scope mismatch")
            for sample in pub.get("caption_render_samples", []):
                if sample.get("subtitle_id") not in track_ids or not isinstance(sample.get("at_seconds"), (int, float)) or sample["at_seconds"] < 0 or sample.get("caption_text_visible") is not True:
                    errors.append("Invalid limited caption render sample")
        else:
            errors.append("Unknown publication evidence basis: " + pub["accession"])
        asset = next((x for x in tables["assets"] if x["id"] == pub.get("asset_id")), None)
        for subtitle_id in pub.get("subtitle_ids", []):
            sub = next((x for x in tables["subtitles"] if x["id"] == subtitle_id), None)
            if not asset or not sub or asset["id"] not in sub.get("asset_ids", []):
                errors.append("Publication caption/asset pairing mismatch: " + pub["accession"])
    groups = read_json(root / "catalog" / "byte-equality-groups.json")["items"] if (root / "catalog" / "byte-equality-groups.json").exists() else []
    files_by_id = {x["id"]: x for x in tables["files"]}
    for group in groups:
        for reference in group.get("file_ids", []):
            if reference not in files_by_id or files_by_id[reference].get("sha256") != group.get("sha256"):
                errors.append("Invalid byte-equality group reference or hash")
    assets_by_id = {x["id"]: x for x in tables["assets"]}
    subtitles_by_id = {x["id"]: x for x in tables["subtitles"]}
    for asset in tables["assets"]:
        canonical_id = asset.get("canonical_byte_source_asset_id")
        if canonical_id:
            canonical = assets_by_id.get(canonical_id)
            original = files_by_id.get(asset.get("canonical_byte_source_file_id"))
            copied = files_by_id.get(asset["file_id"])
            if not canonical or not original or not copied or canonical["file_id"] != original["id"] or not original.get("sha256") or copied.get("sha256") != original["sha256"]:
                errors.append("Invalid canonical byte-copy relation: " + asset["accession"])
    for subtitle in tables["subtitles"]:
        if subtitle.get("source_file_id"):
            source_file = files_by_id.get(subtitle["source_file_id"])
            source_asset = assets_by_id.get(subtitle.get("source_asset_id"))
            if not source_file or not source_asset or source_file["asset_id"] != source_asset["id"] or source_asset["id"] not in subtitle["asset_ids"]:
                errors.append("Invalid subtitle source-file association: " + subtitle["accession"])
        equivalent_id = subtitle.get("equivalent_public_export_subtitle_id")
        if equivalent_id:
            equivalent = subtitles_by_id.get(equivalent_id)
            valid_source = equivalent and any(assets_by_id[aid].get("canonical_byte_source_asset_id") == equivalent.get("source_asset_id") for aid in subtitle["asset_ids"] if aid in assets_by_id)
            if not equivalent or not equivalent.get("public_export") or equivalent.get("version_sha256") != subtitle.get("version_sha256") or equivalent.get("role") != subtitle.get("role") or not valid_source:
                errors.append("Invalid equivalent public caption relation: " + subtitle["accession"])
    caption_exports = {}
    for subtitle in tables["subtitles"]:
        export = subtitle.get("public_export")
        if not export:
            continue
        relative = export.get("path", "")
        parts = relative.split("/")
        valid_path = (len(parts) == 4 and parts[0] == "captions" and parts[1] == subtitle["accession"] and parts[2] == subtitle.get("version_sha256") and re.fullmatch(r"[A-Za-z0-9_.-]+\.srt", parts[3]) and ".." not in parts)
        approved = (export.get("scope_review_status") in ("approved_for_public_archive", "approved_official_source_and_text_scope") and export.get("quality_label") == "machine_draft" and subtitle.get("public_text_status") == "public_machine_draft")
        platform_unknown = (export.get("scope_review_status") == "approved_same_official_source_platform_version_preservation" and export.get("quality_label") == "platform_review_level_unknown" and subtitle.get("public_text_status") == "public_platform_version_review_unknown" and subtitle.get("role") == "platform_original_language_legacy" and subtitle.get("human_listening_review") == "not_recorded")
        if platform_unknown:
            observed = subtitle.get("platform_track_observation", {})
            if observed.get("download_sha256") != subtitle.get("version_sha256") or not observed.get("actual_name") or not observed.get("video_id") or observed.get("unchanged_online") is not True or subtitle.get("existing_platform_label_is_not_review_evidence") is not True:
                platform_unknown = False
        approved = approved or platform_unknown
        if subtitle.get("content_scope_review", {}).get("status", "").startswith("withheld_"):
            approved = False
        expected_hash = export.get("sha256")
        if not valid_path or not approved or expected_hash != subtitle.get("version_sha256") or not re.fullmatch(r"[a-f0-9]{64}", expected_hash or ""):
            errors.append("Invalid public caption export declaration: " + subtitle["accession"])
            continue
        path = root / relative
        if not path.is_file() or digest(path) != expected_hash:
            errors.append("Public caption missing or version hash mismatch: " + subtitle["accession"])
            continue
        if relative in caption_exports:
            errors.append("Duplicate public caption export path: " + relative)
        caption_exports[relative] = expected_hash
    # Additional official editions must pass their own exact-version and
    # exclusion checks before any SRT is admitted by the global file guard.
    # A directory name alone must never whitelist arbitrary transcripts.
    if (root / "published").exists():
        from official_publications import _load_verified
        try:
            unused, editions = _load_verified(root)
            for item, record, unused_text, unused_cues in editions:
                if item["kind"] == "caption":
                    caption_exports[record["path"]] = record["sha256"]
        except (ValueError, OSError, TypeError, KeyError, UnicodeError) as exc:
            errors.append("Invalid official editions: " + str(exc))
    absolute = re.compile(r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|file://|\\\\[^\\\s]+\\|/Users/|/home/)")
    sensitive = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        prohibited = path.suffix.lower() in VIDEO_EXTS or path.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".aac", ".srt", ".vtt", ".ass", ".sub", ".sqlite", ".sqlite3", ".db", ".zip", ".7z", ".tar", ".gz"}
        if prohibited and not (path.suffix.lower() == ".srt" and relative in caption_exports):
            errors.append("Prohibited media/transcript/database file: " + relative)
        if path.stat().st_size > 20 * 1024 * 1024:
            errors.append("Unexpected large public file: " + relative)
        if path.suffix in {".json", ".md", ".yml", ".yaml", ".txt", ".srt"}:
            text = path.read_text(encoding="utf-8-sig")
            if absolute.search(text):
                errors.append("Absolute local path in public file: " + relative)
            if sensitive.search(text):
                errors.append("Credential-like content in public file: " + relative)
    try:
        summary = read_json(root / "catalog" / "summary.json")
    except (OSError, ValueError):
        summary = {}
    if not isinstance(summary, dict) or not isinstance(summary.get("counts"), dict) or any(type(summary["counts"].get(name)) is not int for name in TABLES):
        errors.append("Invalid catalogue summary counts")
        summary = {"counts": {name: None for name in TABLES}}
    for name in TABLES:
        if summary["counts"][name] != len(tables[name]):
            errors.append("Summary count mismatch: " + name)
    for field, expected in publication_summary(tables["publications"]).items():
        if field in summary and summary[field] != expected:
            errors.append("Publication summary count mismatch: " + field)
    registry_path = root / "registry" / "official-projects.json"
    if registry_path.exists():
        registry = read_json(registry_path)
        codes, slugs = set(), set()
        projects_by_id = {x["id"]: x for x in tables["projects"]}
        source_urls = {x["url"] for x in registry.get("sources", [])}
        for source in registry.get("sources", []):
            if not source["url"].startswith("https://a.p4theater.cn/") or source.get("http_status") != 200 or not re.fullmatch(r"[a-f0-9]{64}", source.get("html_utf8_sha256", "")):
                errors.append("Invalid official registry source provenance")
        for entry in registry.get("items", []):
            code, slug = entry.get("official_project_code", ""), entry.get("official_project_slug", "")
            if code in codes or not re.fullmatch(r"P4[CE]-\d{3}", code):
                errors.append("Duplicate or invalid official project code: " + code)
            if slug in slugs or not re.fullmatch(r"[a-z0-9-]+", slug):
                errors.append("Duplicate or invalid official project slug: " + slug)
            codes.add(code)
            slugs.add(slug)
            if entry.get("official_code_namespace") != "a.p4theater.cn:project-code" or entry.get("official_slug_namespace") != "a.p4theater.cn:project-slug":
                errors.append("Invalid official identifier namespace: " + code)
            if entry.get("project_url_zh") not in ("https://a.p4theater.cn/wechat/projects/" + slug, "https://a.p4theater.cn/works/" + slug) or entry.get("project_url_en") != "https://a.p4theater.cn/en/projects/" + slug:
                errors.append("Invalid official project source URL: " + code)
            if not set(entry.get("source_index_urls", [])).issubset(source_urls) or not entry.get("source_index_urls"):
                errors.append("Official project source-index reference missing: " + code)
            matched = entry.get("current_video_catalogue_matches", [])
            candidates = entry.get("candidate_current_video_catalogue_matches", [])
            if entry.get("video_absence_claim") is not False or entry.get("current_video_inventory_attached") != bool(matched):
                errors.append("Invalid official registry media coverage claim: " + code)
            for link in matched + candidates:
                project = projects_by_id.get(link.get("id"))
                if not project or project["accession"] != link.get("accession") or link.get("asset_membership_verified") is not False:
                    errors.append("Broken or overstated official registry project reference: " + code)
                    continue
                if link in matched:
                    existing_slugs = {x["value"] for x in project.get("existing_registry_identifiers", [])}
                    if slug not in existing_slugs or code not in project.get("aliases", []):
                        errors.append("Official registry direct-match evidence or alias mismatch: " + code)
                elif link.get("same_as_established") is not False:
                    errors.append("Official registry title candidate cannot assert identity: " + code)
        if registry.get("summary", {}).get("official_public_index_items") != len(registry.get("items", [])) or summary.get("official_public_registry_count") != len(registry.get("items", [])):
            errors.append("Official registry summary count mismatch")
    scope_path = root / ".p4-public-scope.json"
    if scope_path.exists():
        policy = read_json(scope_path)
        forbidden = set(policy.get("excluded_identity_sha256s", []))
        if policy.get("projection_required") is not True:
            errors.append("Public scope projection guard must remain enabled")
        for item in all_items:
            if hashlib.sha256(item["id"].encode()).hexdigest() in forbidden:
                errors.append("Excluded private identity revived in public catalogue")
        allow_captions = set(policy.get("allowed_caption_subtitle_ids", []))
        versions = policy.get("allowed_caption_versions")
        if not isinstance(versions, dict) or set(versions) != allow_captions or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in versions.values()):
            errors.append("Public caption scope lacks exact approved version hashes")
            versions = {}
        exports = {x["id"] for x in tables["subtitles"] if x.get("public_export")}
        if exports != allow_captions:
            errors.append("Public captions differ from the explicit official-source allow list")
        for sub in tables["subtitles"]:
            if not sub.get("public_export"):
                continue
            if versions.get(sub["id"]) != sub.get("version_sha256"):
                errors.append("Public caption version differs from its explicit approval: " + sub["accession"])
            owner = assets_by_id.get(sub.get("source_asset_id"))
            proof = owner.get("official_media_evidence", {}) if owner else {}
            source = files_by_id.get(proof.get("file_id"))
            if not owner or owner.get("official_media_affiliation_status") != "confirmed_official_source_and_project_context" or not source or source.get("sha256") != proof.get("source_sha256") or owner["file_id"] != source["id"] or proof.get("asset_id") != owner["id"]:
                errors.append("Caption export lacks confirmed official media affiliation")
            if sub["public_export"].get("scope_review_status") not in ("approved_official_source_and_text_scope", "approved_same_official_source_platform_version_preservation") or sub["public_export"].get("official_evidence_source_sha256") != proof.get("source_sha256"):
                errors.append("Caption export is not bound to its official source evidence")
        for asset in tables["assets"]:
            status = asset.get("official_media_affiliation_status")
            if status not in ("confirmed_official_source_and_project_context", "official_media_affiliation_pending"):
                errors.append("Public candidate is missing its official-affiliation status")
            if status != "confirmed_official_source_and_project_context" and asset.get("new_public_upload_allowed") is not False:
                errors.append("Pending official affiliation cannot authorize a new public upload")
        if summary.get("private_master_inventory_video_files", 0) < summary.get("public_selected_video_files", 0) or summary.get("public_selected_video_files") != len(tables["assets"]):
            errors.append("Private inventory and public selected counts are inconsistent")
    return {"valid": not errors, "entity_count": len(all_items), "counts": {k: len(v) for k, v in tables.items()}, "errors": errors}


def record_publication(args):
    root = Path(args.root)
    state = Path(args.local_state).resolve()
    if root.resolve() == state or root.resolve() in state.parents:
        raise ValueError("Private local state must be outside public output")
    if args.channel != "UCp_LyzepcrZX6Cgzv2Qit4w":
        raise ValueError("Target channel differs from this archive's registered P4 Archive channel")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video):
        raise ValueError("A real 11-character YouTube video ID is required")
    reported_at = aware_timestamp(args.observed_at)
    if args.playback_status not in ("not_recorded", "operator_reported_playable"):
        raise ValueError("Manual reports cannot assert verified playback")
    if args.caption_status not in ("not_recorded", "operator_reported_uploaded", "operator_reported_visible"):
        raise ValueError("Manual reports cannot assert verified caption visibility")
    registry = Registry(state)
    assets = read_json(root / "catalog" / "assets.json")
    asset = next((x for x in assets["items"] if args.asset in (x["id"], x["accession"])), None)
    if asset is None:
        raise ValueError("Unknown asset accession or ID")
    current_file = next((x for x in read_json(root / "catalog" / "files.json")["items"] if x["id"] == asset["file_id"]), None)
    if not current_file or current_file["bytes"] == 0:
        raise ValueError("Cannot associate a YouTube publication with an empty or missing source version")
    subtitle_table = read_json(root / "catalog" / "subtitles.json")["items"]
    subtitle_ids = []
    for reference in args.subtitle:
        subtitle = next((x for x in subtitle_table if reference in (x["id"], x["accession"])), None)
        if subtitle is None or asset["id"] not in subtitle["asset_ids"]:
            raise ValueError("Subtitle must already be explicitly paired with the asset")
        subtitle_ids.append(subtitle["id"])
    if args.caption_status != "not_recorded" and not subtitle_ids:
        raise ValueError("A reported caption publication requires explicit subtitle references")
    publication = {**registry.get("publication", args.channel + "|" + args.video), "kind": "publication", "title": asset["accession"] + " · YouTube 人工声明", "asset_id": asset["id"], "file_id": asset["file_id"], "channel_id": args.channel, "youtube_video_id": args.video, "url": "https://www.youtube.com/watch?v=" + args.video, "visibility": args.visibility, "reported_observation_at": reported_at, "record_basis": "operator_reported", "reported_playback_status": args.playback_status, "subtitle_ids": subtitle_ids, "reported_caption_status": args.caption_status, "platform_readback_verification": "not_recorded", "in_current_inventory": True}
    table = read_json(root / "catalog" / "publications.json")
    old = next((i for i, x in enumerate(table["items"]) if x["id"] == publication["id"] or (x.get("channel_id") == args.channel and x.get("youtube_video_id") == args.video)), None)
    if old is None:
        table["items"].append(publication)
    else:
        if table["items"][old]["asset_id"] != asset["id"] or table["items"][old]["file_id"] != asset["file_id"]:
            raise ValueError("An existing channel/video association cannot be silently reassigned to another asset or file version")
        if table["items"][old].get("platform_readback_verification") == "verified":
            raise ValueError("An operator report cannot overwrite an existing platform-readback receipt")
        if table["items"][old]["id"] != publication["id"]:
            raise ValueError("Private publication registry is not aligned with the existing channel/video record")
        table["items"][old] = publication
    if publication["id"] not in asset["youtube_publication_ids"]:
        asset["youtube_publication_ids"].append(publication["id"])
    write_json(root / "catalog" / "publications.json", table)
    write_json(root / "catalog" / "assets.json", assets)
    summary = read_json(root / "catalog" / "summary.json")
    summary["counts"]["publications"] = len(table["items"])
    summary["current_inventory_counts"]["publications"] = len(table["items"])
    summary.update(publication_summary(table["items"]))
    write_json(root / "catalog" / "summary.json", summary)
    registry.save()
    print(json.dumps(publication, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import")
    importer.add_argument("--audit", required=True)
    importer.add_argument("--quality")
    importer.add_argument("--crosswalk")
    importer.add_argument("--project-titles", help="Private JSON map of inventory directory labels to reviewed public titles; required for an unknown new project")
    importer.add_argument("--local-state", required=True)
    importer.add_argument("--output", required=True)
    checker = commands.add_parser("validate")
    checker.add_argument("--root", required=True)
    plan = commands.add_parser("hash-plan")
    plan.add_argument("--root", required=True)
    plan.add_argument("--local-state", required=True)
    publication = commands.add_parser("record-publication", help="Record an operator report; does not verify, upload or change YouTube")
    publication.add_argument("--root", required=True)
    publication.add_argument("--local-state", required=True)
    publication.add_argument("--asset", required=True)
    publication.add_argument("--channel", required=True)
    publication.add_argument("--video", required=True)
    publication.add_argument("--visibility", choices=("public", "unlisted", "private", "unknown"), required=True)
    publication.add_argument("--reported-at", "--observed-at", dest="observed_at", required=True, help="Timezone-aware ISO timestamp reported by the operator; not a platform verification receipt")
    publication.add_argument("--playback-status", choices=("not_recorded", "operator_reported_playable"), default="not_recorded")
    publication.add_argument("--caption-status", choices=("not_recorded", "operator_reported_uploaded", "operator_reported_visible"), default="not_recorded")
    publication.add_argument("--subtitle", action="append", default=[])
    args = parser.parse_args()
    if args.command == "import":
        import_catalog(args)
    elif args.command == "record-publication":
        record_publication(args)
    elif args.command == "validate":
        result = validate(args.root)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["valid"] else 1
    else:
        summary = read_json(Path(args.root) / "catalog" / "summary.json")
        queue = read_json(Path(args.local_state) / "hash_plan.json")
        print(json.dumps({"pending_files": len(queue["files"]), "pending_bytes": sum(x["bytes"] for x in queue["files"]), "recorded_full_sha256": summary["full_sha256_recorded"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
