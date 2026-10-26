#!/usr/bin/env python3
"""Merge a locally reviewed caption release without importing live hash caches.

This copies approved, byte-identical staged SRT versions and projects only the
reviewed metadata. It never modifies original sidecars or publishes to a server.
"""
import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path

from archive_catalog import TABLES, aware_timestamp, digest, pathkey, read_json, render_indexes, srt_metadata, validate, write_json


def merge_release(root, state, options_path, queue_path):
    root, state = Path(root).resolve(), Path(state).resolve()
    if (root / ".p4-public-scope.json").exists():
        raise ValueError("Direct caption merge into a scope-projected public tree is blocked; use private master and official-source scope projection")
    if root == state or root in state.parents:
        raise ValueError("Private state must be outside the public repository")
    options, queue = read_json(options_path), read_json(queue_path)
    reviewed_at = aware_timestamp(options["generated_at"])
    tables = {name: read_json(root / "catalog" / (name + ".json"))["items"] for name in TABLES}
    assets = {x["id"]: x for x in tables["assets"]}
    by_accession = {x["accession"]: x for x in tables["assets"]}
    files = {x["id"]: x for x in tables["files"]}
    subtitles = {x["id"]: x for x in tables["subtitles"]}
    mapping = read_json(state / "mapping.json")
    queue_by_id = {x["asset_id"]: x for x in queue["items"]}
    approved = options["approved_original_exports"]
    if len(approved) != options["scope_approved_original_caption_files"]:
        raise ValueError("Review export count differs from the declared count")
    stage_root = (state / "caption_export_staging").resolve()
    preflight = []
    families = defaultdict(set)
    suffixes = {"zh": ".srt", "en": ".en.srt", "bilingual": ".zh-en.srt"}
    for export in approved:
        sub = subtitles[export["subtitle_id"]]
        asset = assets[export["asset_id"]]
        source_file = files[export["file_id"]]
        staged = Path(export["staged_path"]).resolve()
        if stage_root not in staged.parents:
            raise ValueError("Staged caption is outside the reviewed staging directory")
        declaration = export["public_export"]
        relative = declaration["path"]
        expected_path = "captions/" + sub["accession"] + "/" + sub["version_sha256"] + "/" + sub["accession"] + ".srt"
        if relative != expected_path or declaration["sha256"] != sub["version_sha256"] or export["source_sha256"] != sub["version_sha256"]:
            raise ValueError("Caption identity, version hash or export path mismatch")
        if export["scope_review_status"] != "approved_for_public_archive" or export["quality_label"] != "machine_draft":
            raise ValueError("Only the explicit reviewed machine-draft export is accepted")
        if source_file["asset_id"] != asset["id"] or asset["file_id"] != source_file["id"] or asset["id"] not in sub["asset_ids"]:
            raise ValueError("Source file, asset and caption are not paired")
        source = Path(mapping["files"][source_file["id"]]["path"])
        original_caption = Path(export["source_path"])
        if pathkey(source.with_suffix(suffixes[export["variant"]])) != pathkey(original_caption):
            raise ValueError("Caption variant does not match the source video stem")
        meta = srt_metadata(staged)
        if meta["sha256"] != sub["version_sha256"] or digest(original_caption) != sub["version_sha256"] or meta["format_status"] != "structure_passed":
            raise ValueError("Staged/original caption bytes or SRT structure changed")
        if meta["bytes"] != sub["bytes"] or meta["cue_count"] != sub["cue_count"]:
            raise ValueError("Caption size or cue count differs from its version record")
        families[asset["id"]].add(export["variant"])
        preflight.append((export, sub, asset, staged))
    if any(variants != {"zh", "en", "bilingual"} for variants in families.values()):
        raise ValueError("A reviewed source family must include all three caption variants")

    # Scope review is a text/context review, explicitly separate from listening.
    canonical_items = {x["asset_id"]: x for x in options["all_canonical_caption_options"]}
    variants = defaultdict(list)
    for option in options["all_canonical_caption_options"]:
        variants[option["asset_id"]].append(option)
        sub = subtitles[option["subtitle_id"]]
        sub["content_scope_review"] = {"status": option["scope_review_status"], "reviewed_at": reviewed_at, "method": "three_caption_variant_pattern_screening_and_flagged_context_reading", "scope": "text_and_existing_folder_context_not_full_listening_or_continuous_video_watch"}
        if option["scope_review_status"].startswith("withheld_"):
            sub["public_text_status"] = option["scope_review_status"]
        elif option["empty"]:
            sub["public_text_status"] = "not_exported_empty_no_recognized_text"
    for asset_id, option in canonical_items.items():
        asset, planned = assets[asset_id], queue_by_id[asset_id]
        review = {"status": option["scope_review_status"], "reviewed_at": reviewed_at, "source_file_id": asset["file_id"], "method": "existing_folder_content_analysis_and_three_caption_variant_screening", "human_listening_review": "not_recorded", "human_continuous_watch": "not_recorded"}
        asset["content_scope_review"] = review
        if option["scope_review_status"] == "approved_for_public_archive":
            asset["project_ids"] = list(option["effective_upload_project_ids"])
            asset["content_classification"] = "p4_project_context_supported"
            asset["project_assignment_review"] = {"status": planned["project_assignment"]["status"], "project_ids": asset["project_ids"], "reviewed_at": reviewed_at, "method": "caption_text_and_existing_folder_content_analysis", "is_human_listening_review": False}
            if asset["publication_status"] == "hold_project_assignment_review":
                asset["publication_status"] = "pending_media_and_channel_publication_checks"
            source_name = Path(planned["source_path"]).stem
            if planned["project_assignment"]["status"] == "evidence_supported_animal_farm":
                original = next(x for x in variants[asset_id] if x["variant"] == "zh")
                asset["project_assignment_review"]["evidence"] = [{"subtitle_id": original["subtitle_id"], "clock": "source_zero", "start_seconds": 473 if source_name == "DJI_0002" else 1, "end_seconds": 486 if source_name == "DJI_0002" else 108, "description": "字幕及既有目录分析将内容指向动物农场招募或试演。"}]
                asset.pop("related_project_titles", None)
                asset.pop("project_assignment_note", None)
        elif option["scope_review_status"] == "withheld_possible_device_passcode":
            asset["publication_status"] = "hold_sensitive_content_redaction_required"
        elif option["scope_review_status"] == "withheld_project_attribution_unresolved":
            asset["publication_status"] = "hold_project_attribution_unresolved"
        if planned["planning_stage"] == "deferred_alternate_file_version_comparison":
            other = by_accession["P4-V001069"]
            asset["alternate_representation_candidate_of_asset_id"] = other["id"]
            asset["alternate_representation_review"] = {"status": "pending_content_and_encoding_comparison", "basis": "matching_camera_name_and_duration_in_existing_manifest", "byte_equality_asserted": False}
            asset["publication_status"] = "deferred_alternate_file_version_comparison"

    # Byte identity connects physical locations; it does not merge event IDs.
    byte_groups = read_json(root / "catalog" / "byte-equality-groups.json")["items"]
    for planned in queue["items"]:
        if planned.get("source_selection") != "known_byte_copy":
            continue
        copy = assets[planned["asset_id"]]
        preferred = by_accession[planned["preferred_upload_asset_accession"]]
        copied_file, preferred_file = files[copy["file_id"]], files[preferred["file_id"]]
        group = next((g for g in byte_groups if copy["file_id"] in g["file_ids"] and preferred["file_id"] in g["file_ids"]), None)
        if not group or copied_file["sha256"] != group["sha256"] or preferred_file["sha256"] != group["sha256"]:
            raise ValueError("Queue copy relation lacks matching full-SHA catalogue evidence")
        copy["canonical_byte_source_asset_id"] = preferred["id"]
        copy["canonical_byte_source_file_id"] = preferred["file_id"]
        copy["byte_identity_relation"] = {"status": "full_sha256_equal", "sha256": group["sha256"], "meaning": "same_bytes_at_separate_registered_locations_event_identity_not_merged"}
        preferred.setdefault("byte_copy_asset_ids", [])
        if copy["id"] not in preferred["byte_copy_asset_ids"]:
            preferred["byte_copy_asset_ids"].append(copy["id"])
        copy["publication_status"] = "hold_verified_byte_copy_use_canonical_source"
        if preferred.get("content_scope_review", {}).get("status", "").startswith("withheld_"):
            copy["content_scope_review"] = {"status": preferred["content_scope_review"]["status"], "basis": "same_full_sha256_as_reviewed_canonical_source", "source_file_id": copy["file_id"], "canonical_source_asset_id": preferred["id"], "reviewed_at": reviewed_at, "human_listening_review": "not_recorded"}
        for copy_sub_id in copy["subtitle_ids"]:
            copy_sub = subtitles[copy_sub_id]
            equivalent = next((subtitles[sid] for sid in preferred["subtitle_ids"] if subtitles[sid]["role"] == copy_sub["role"] and subtitles[sid]["version_sha256"] == copy_sub["version_sha256"] and subtitles[sid].get("public_export")), None)
            if equivalent:
                copy_sub["equivalent_public_export_subtitle_id"] = equivalent["id"]

    for export, sub, asset, staged in preflight:
        target = root / export["public_export"]["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, target)
        sub["public_export"] = export["public_export"]
        sub["public_text_status"] = "public_machine_draft"
        sub["source_file_id"] = export["file_id"]
        sub["source_asset_id"] = export["asset_id"]
        sub["pairing_status"] = "source_stem_three_variant_and_manifest_association_reviewed"
        sub["pairing_review"] = {"method": "matching_source_video_stem_and_three_sidecar_variants_in_reviewed_local_manifest", "source_file_id": export["file_id"], "canonical_source_file_id": export["file_id"], "variant": export["variant"], "clock": "source_zero", "scope": "source_association_only_not_human_timing_or_word_accuracy_review"}
        asset.setdefault("public_caption_subtitle_ids", [])
        if sub["id"] not in asset["public_caption_subtitle_ids"]:
            asset["public_caption_subtitle_ids"].append(sub["id"])
    # Link equivalent copied caption records after canonical exports now exist.
    for asset in tables["assets"]:
        if not asset.get("canonical_byte_source_asset_id"):
            continue
        preferred = assets[asset["canonical_byte_source_asset_id"]]
        for sid in asset["subtitle_ids"]:
            sub = subtitles[sid]
            equivalent = next((subtitles[x] for x in preferred["subtitle_ids"] if subtitles[x]["role"] == sub["role"] and subtitles[x]["version_sha256"] == sub["version_sha256"] and subtitles[x].get("public_export")), None)
            if equivalent:
                sub["equivalent_public_export_subtitle_id"] = equivalent["id"]
    # A mixed folder remains a provisional source group, never a merged event.
    changed_project_assets = [a for a in tables["assets"] if a.get("project_assignment_review", {}).get("status") == "evidence_supported_animal_farm"]
    for event in tables["events"]:
        if any(event["id"] in a["event_ids"] for a in changed_project_assets):
            relevant = [a for a in tables["assets"] if event["id"] in a["event_ids"]]
            event["project_ids"] = sorted({pid for a in relevant for pid in a["project_ids"]})
            event["title"] = "荧火空间 · 多项目来源组"
            event["classification_status"] = "mixed_project_folder_group_not_confirmed_single_event"
            event["event_type"] = "unclassified"
            event["description"] = "此暂定目录组包含动物农场招募或试演及玻璃动物园排练素材；目录共置不能证明属于同一项目或同一事件。"
            event["content_scope_review"] = {"status": "mixed_project_context_supported", "reviewed_at": reviewed_at, "method": "caption_text_and_existing_folder_analysis", "is_human_listening_review": False}

    for name in ("assets", "subtitles", "events"):
        write_json(root / "catalog" / (name + ".json"), {"schema_version": 1, "items": tables[name]})
    summary = read_json(root / "catalog" / "summary.json")
    exported = [s for s in tables["subtitles"] if s.get("public_export")]
    summary.update(public_export_policy="metadata_and_explicit_scope_reviewed_machine_draft_captions_no_video", public_caption_export_count=len(exported), public_caption_source_file_count=len({s["source_file_id"] for s in exported}), public_caption_quality="machine_draft_not_full_listening_review")
    write_json(root / "catalog" / "summary.json", summary)
    render_indexes(root, tables, summary)
    render_caption_index(root, tables)
    result = validate(root)
    if not result["valid"]:
        raise ValueError("Merged release did not validate: " + json.dumps(result["errors"], ensure_ascii=False))
    return {"caption_files": len(exported), "caption_source_files": len(families), "byte_copy_relations": sum(bool(a.get("canonical_byte_source_asset_id")) for a in tables["assets"]), "source_originals_modified": False, "live_hash_cache_read_or_modified": False, "valid": True}


def render_caption_index(root, tables):
    projects = {x["id"]: x for x in tables["projects"]}
    files = {x["id"]: x for x in tables["files"]}
    subs = {x["id"]: x for x in tables["subtitles"]}
    publications = {x["id"]: x for x in tables["publications"]}
    families = [(a, [subs[sid] for sid in a["subtitle_ids"] if subs[sid].get("public_export")]) for a in tables["assets"]]
    families = [(a, s) for a, s in families if s]
    lines = ["# 公开字幕与翻译", "", f"当前保存 {sum(len(s) for _, s in families)} 份原样 SRT，关联 {len(families)} 个明确来源文件。当前原语、英文、中英双语三稿与独立保存的旧平台版本分别管理。", "", "三套当前稿标为机器稿，尚未全片听校；旧平台版本的审校程度未知，名称里的 machine reviewed 不是人工听校证据。原字节和版本 SHA-256 保留。线上发布、菜单可选与实际画面渲染分开记录，不能从本地可下载推断线上验证已完成。", "", "| 项目 | 素材 / 物理文件 | 原语 | English | 中英双语 | 已关联公开视频 | 版本关系与状态 |", "|---|---|---|---|---|---|---|"]
    for asset, captions in sorted(families, key=lambda x: x[0]["accession"]):
        project = projects[asset["project_ids"][0]]
        links = {s["role"]: "[" + s["accession"] + "](" + s["public_export"]["path"].removeprefix("captions/") + ")" for s in captions}
        notes = "机器稿；未全片听校"
        old_tracks = [s for s in captions if s["role"] == "platform_original_language_legacy"]
        for old in old_tracks:
            notes += "；旧平台版[" + old["accession"] + "](" + old["public_export"]["path"].removeprefix("captions/") + ")（" + str(old["cue_count"]) + "条，审校未知，未替换新稿）"
        if asset.get("byte_copy_asset_ids"):
            notes += "；另有 " + str(len(asset["byte_copy_asset_ids"])) + " 个同字节位置"
        if asset.get("alternate_representation_review"):
            notes += "；另一编码候选，YouTube 上传暂缓比较"
        videos = [publications[pid] for pid in asset.get("youtube_publication_ids", []) if pid in publications and publications[pid].get("platform_readback_verification") == "verified" and publications[pid].get("visibility") == "public" and publications[pid].get("file_id") == asset["file_id"]]
        video_links = " / ".join("[YouTube](" + pub["url"] + ")" for pub in videos) or "未建立已核验关联"
        lines.append("| [" + project["title"] + "](../projects/" + project["accession"] + "/index.md) | " + asset["accession"] + " / " + files[asset["file_id"]]["accession"] + " | " + links.get("original_asr", "—") + " | " + links.get("machine_translation", "—") + " | " + links.get("bilingual_machine_translation", "—") + " | " + video_links + " | " + notes + " |")
    lines += ["", "当前正文只保留具有官网完整原片 SHA 对应和主档案实质项目上下文的明确来源。其他先前文本已移到私有主目录，等待官网逐项核实；这不把未确认素材判为家庭录像。文本筛查与官方项目关联均不能代替全片听校或连续观看。", ""]
    for asset, _ in families:
        if asset.get("official_media_evidence"):
            lines += ["[" + asset["accession"] + " 官方来源证据](../evidence/" + asset["accession"] + "-official-media.json)", ""]
    lines += ["疑似私人信息的原版、归属未知的材料和空识别版本没有在此提供正文。脱敏稿须另建版本并满足当前范围规则；原版保留于私有保存端。", "", "同字节副本通过编号关联规范来源，原有事件组未合并。当前树移除不等于抹除已有 Git 历史。", "", "引用时请保留项目编号、视频编号、物理文件编号、字幕编号及版本 SHA-256。[返回档案目录](../INDEX.md)。", ""]
    target = root / "captions" / "README.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--queue", required=True)
    args = parser.parse_args()
    print(json.dumps(merge_release(args.root, args.local_state, args.options, args.queue), ensure_ascii=False))


if __name__ == "__main__":
    main()
