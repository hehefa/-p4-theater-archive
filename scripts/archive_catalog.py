#!/usr/bin/env python3
"""Build a public metadata catalogue without publishing source media or transcripts.

All source paths and persistent accession allocation live in --local-state, which
must be outside --output. Re-running the same inventory preserves entity IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path, PureWindowsPath

VERSION = 1
PREFIX = {"project": "P", "collection": "C", "event": "E", "asset": "V", "file": "F", "subtitle": "S", "publication": "U", "checksum": "H"}
VIDEO_EXTS = {".mp4", ".mov", ".mts", ".m2ts", ".mpeg", ".mpg", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".ts", ".3gp", ".webm"}
TABLES = tuple(k + "s" for k in PREFIX if k != "publication") + ("publications",)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def import_catalog(args):
    out, state = Path(args.output).resolve(), Path(args.local_state).resolve()
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
        "assets": ("human_review", "rights_status", "youtube_publication_ids", "publication_status"),
        "subtitles": ("human_listening_review", "public_text_status", "public_export", "youtube_caption_status", "pairing_status", "pairing_review", "source_file_id", "source_asset_id", "content_scope_review", "equivalent_public_export_subtitle_id"),
        "files": ("human_continuous_watch", "full_decode_status"),
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
                    reviewed_fields = ("content_scope_review", "project_assignment_review", "project_ids", "content_classification", "canonical_byte_source_asset_id", "canonical_byte_source_file_id", "byte_copy_asset_ids", "byte_identity_relation", "alternate_representation_candidate_of_asset_id", "alternate_representation_review", "public_caption_subtitle_ids")
                    if old.get("content_scope_review") or old.get("byte_identity_relation") or old.get("alternate_representation_review"):
                        for field in reviewed_fields:
                            if field in old:
                                item[field] = old[field]
                if name == "events" and old.get("content_scope_review"):
                    for field in ("content_scope_review", "project_ids", "title", "classification_status", "event_type", "description"):
                        if field in old:
                            item[field] = old[field]
                for field in protected_fields.get(name, ()):
                    value = old.get(field)
                    if value is not None and value not in ("not_recorded", "not_established", "not_newly_probed", "not_uploaded_in_this_catalogue", "withheld_pending_review", "filename_association_requires_review", "edited_product_pairing_pending", "pending_content_and_publication_review") and not (isinstance(value, str) and value.startswith("hold_")):
                        item[field] = value
                if name == "assets" and old.get("file_id") != item.get("file_id"):
                    item["prior_version_review"] = {"file_id": old.get("file_id"), "human_review": old.get("human_review", "not_recorded")}
                    item["human_review"] = "needs_review_source_version_changed"
                    if old.get("youtube_publication_ids"):
                        item["publication_status"] = "previous_version_publication_recorded"
                    if old.get("content_scope_review"):
                        item["prior_version_content_scope_review"] = old["content_scope_review"]
                        item["content_scope_review"] = {"status": "needs_review_source_version_changed", "source_file_id": item["file_id"], "prior_source_file_id": old.get("file_id"), "human_listening_review": "not_recorded"}
                    item.pop("public_caption_subtitle_ids", None)
        for item in older.values():
            item["in_current_inventory"] = False
            items.append(item)
    for pub in tables["publications"]:
        pub["in_current_inventory"] = True
    for name, items in tables.items():
        write_json(out / "catalog" / (name + ".json"), {"schema_version": VERSION, "items": items})
    write_json(out / "catalog" / "byte-equality-groups.json", {"schema_version": VERSION, "items": byte_groups})
    current_files = [x for x in tables["files"] if x["in_current_inventory"]]
    summary = {"schema_version": VERSION, "inventory_snapshot_at": audit["generated_at"], "public_export_policy": "metadata_only_no_video_or_unreviewed_transcript_text", "counts": {name: len(items) for name, items in tables.items()}, "current_inventory_counts": {name: sum(x["in_current_inventory"] for x in items) for name, items in tables.items()}, "video_bytes": sum(x["bytes"] for x in current_files), "full_sha256_recorded": sum(f["sha256"] is not None for f in current_files), "full_sha256_pending": sum(f["sha256"] is None for f in current_files), "full_decode_passed_existing_reports": sum(f["full_decode_status"] == "passed_existing_report" for f in current_files), "subtitle_format_status": dict(Counter(x["format_status"] for x in tables["subtitles"])), "publication_reports_recorded": len(tables["publications"]), "youtube_uploads_recorded": sum(x.get("platform_readback_verification") == "verified" for x in tables["publications"]), "human_review_status": "not_recorded", "scope": "18 selected theatre/event collections; personal/system backup roots excluded; collection inclusion does not establish publication clearance"}
    summary["public_caption_export_count"] = sum(bool(x.get("public_export")) for x in tables["subtitles"])
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
    print(json.dumps(summary, ensure_ascii=False))


def render_indexes(out, tables, summary):
    counts = summary["counts"]
    lines = ["# P4 项目与素材目录", "", f"当前登记 {counts['projects']} 个候选项目、{counts['collections']} 个来源批次、{counts['events']} 个暂定事件组、{counts['files']} 个视频文件位置。", "", "编号为长期引用别名，实体主键为 canonical ID。来源目录与原始文件名的私有映射保存在维护端；公开目录不含未审核台词或原片。", "", "| 项目编号 | 项目 | 文件数 | 事件组 |", "|---|---|---:|---:|"]
    if (out / "registry" / "official-projects.json").exists():
        registry = read_json(out / "registry" / "official-projects.json")
        lines[2:2] = [f"另见 [官方公开项目索引快照](registry/README.md)：当前 {len(registry['items'])} 条公开项目条目。该注册表与本批本地视频目录分开；数量不能相加或视为全部历史项目总数。", ""]
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
        for event in events:
            page += [f"## {event['accession']} · {event['title']}", "", "日期：" + (event["date"]["value"] or "尚未确定") + "；事件边界：暂定目录组。", "", "| 视频编号 | 文件编号 | 格式 | 大小 MB | 字幕版本 | 公开字幕 | 完整解码 | 发布状态 |", "|---|---|---|---:|---:|---|---|---|"]
            for a in assets:
                if event["id"] not in a["event_ids"]:
                    continue
                f = files[a["file_id"]]
                source_asset = all_assets.get(a.get("canonical_byte_source_asset_id"), a)
                available = [subtitles[sid] for sid in source_asset.get("subtitle_ids", []) if sid in subtitles and subtitles[sid].get("public_export") and subtitles[sid].get("source_file_id", source_asset["file_id"]) == source_asset["file_id"]]
                labels = {"original_asr": "原语", "machine_translation": "English", "bilingual_machine_translation": "中英"}
                links = " / ".join("[" + labels.get(s["role"], s["language"]) + "](../../" + s["public_export"]["path"] + ")" for s in available) or "待公开"
                if source_asset["id"] != a["id"]:
                    links += "；同字节来源 " + source_asset["accession"]
                page.append(f"| {a['accession']} | {f['accession']} | {f['extension']} | {f['bytes'] / 1_000_000:.2f} | {len(a['subtitle_ids'])} | {links} | {f['full_decode_status']} | {a['publication_status']} |")
            page.append("")
        page += ["[返回总目录](../../INDEX.md)", ""]
        target = out / "projects" / p["accession"] / "index.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(page), encoding="utf-8")
    lines += ["", "[公开字幕与翻译目录](captions/README.md)：按源素材和版本下载原语、英文、中英双语机器稿。", "", "机器可读数据：[目录摘要](catalog/summary.json)、[项目](catalog/projects.json)、[事件](catalog/events.json)、[视频](catalog/assets.json)、[文件版本](catalog/files.json)、[字幕记录](catalog/subtitles.json)、[YouTube 发布记录](catalog/publications.json)。", ""]
    (out / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")


def validate(root):
    root = Path(root)
    errors = []
    tables = {name: read_json(root / "catalog" / (name + ".json"))["items"] for name in TABLES}
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
    refs = {"project_id": "projects", "project_ids": "projects", "collection_id": "collections", "event_ids": "events", "asset_id": "assets", "asset_ids": "assets", "file_id": "files", "subtitle_ids": "subtitles", "derived_from_file_id": "files", "youtube_publication_ids": "publications", "source_file_id": "files", "source_asset_id": "assets", "canonical_byte_source_asset_id": "assets", "canonical_byte_source_file_id": "files", "byte_copy_asset_ids": "assets", "alternate_representation_candidate_of_asset_id": "assets", "public_caption_subtitle_ids": "subtitles", "equivalent_public_export_subtitle_id": "subtitles"}
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
    for pub in tables["publications"]:
        if pub.get("channel_id") != "UCp_LyzepcrZX6Cgzv2Qit4w":
            errors.append("Unexpected archive YouTube channel: " + pub["accession"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", pub.get("youtube_video_id", "")):
            errors.append("Invalid YouTube video ID: " + pub["accession"])
        if pub.get("visibility") not in ("public", "unlisted", "private", "unknown"):
            errors.append("Invalid YouTube visibility: " + pub["accession"])
        if pub.get("record_basis") != "operator_reported" or pub.get("platform_readback_verification") != "not_recorded":
            errors.append("Publication report cannot assert platform verification: " + pub["accession"])
        try:
            aware_timestamp(pub.get("reported_observation_at"))
        except ValueError:
            errors.append("Invalid publication report timestamp: " + pub["accession"])
        if pub.get("reported_caption_status") != "not_recorded" and not pub.get("subtitle_ids"):
            errors.append("Reported caption status requires subtitle references: " + pub["accession"])
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
        approved = (export.get("scope_review_status") == "approved_for_public_archive" and export.get("quality_label") == "machine_draft" and subtitle.get("public_text_status") == "public_machine_draft")
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
    summary = read_json(root / "catalog" / "summary.json")
    for name in TABLES:
        if summary["counts"][name] != len(tables[name]):
            errors.append("Summary count mismatch: " + name)
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
    old = next((i for i, x in enumerate(table["items"]) if x["id"] == publication["id"]), None)
    if old is None:
        table["items"].append(publication)
    else:
        if table["items"][old]["asset_id"] != asset["id"] or table["items"][old]["file_id"] != asset["file_id"]:
            raise ValueError("An existing channel/video association cannot be silently reassigned to another asset or file version")
        table["items"][old] = publication
    if publication["id"] not in asset["youtube_publication_ids"]:
        asset["youtube_publication_ids"].append(publication["id"])
    write_json(root / "catalog" / "publications.json", table)
    write_json(root / "catalog" / "assets.json", assets)
    summary = read_json(root / "catalog" / "summary.json")
    summary["counts"]["publications"] = len(table["items"])
    summary["current_inventory_counts"]["publications"] = len(table["items"])
    summary["publication_reports_recorded"] = len(table["items"])
    summary["youtube_uploads_recorded"] = 0
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
