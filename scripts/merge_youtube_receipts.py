#!/usr/bin/env python3
"""Ingest scoped existing-video Studio metadata receipts, without browser access.

This path is separate from operator reports. Source SHA describes the Studio
description/local-master association; it does not verify YouTube stream bytes,
video playback, online caption availability, or a new upload in this batch.
"""
import argparse
import json
from pathlib import Path

from archive_catalog import Registry, TABLES, aware_timestamp, digest, publication_summary, read_json, render_indexes, validate, write_json
from merge_caption_release import render_caption_index


def merge_receipts(root, state, receipt_path):
    root, state = Path(root).resolve(), Path(state).resolve()
    if root == state or root in state.parents:
        raise ValueError("Private state must be outside the public repository")
    receipt = read_json(receipt_path)
    if receipt.get("schema_version") != "p4-existing-youtube-mapping-receipts-v1":
        raise ValueError("Unexpected Studio receipt schema")
    provenance = receipt["observation_provenance"]
    if provenance["target_channel_id"] != "UCp_LyzepcrZX6Cgzv2Qit4w" or provenance["interface"] != "YouTube Studio videos and shorts table DOM":
        raise ValueError("Receipt is not for this archive's target Studio interface")
    recorded_at = aware_timestamp(receipt["recorded_at"])
    observed_at = provenance.get("platform_observed_at")
    if observed_at is not None:
        observed_at = aware_timestamp(observed_at)
    elif provenance.get("recorded_at_is_not_substituted_for_observed_at") is not True:
        raise ValueError("A missing observation time cannot be replaced by record time")
    local_provenance = receipt["trusted_local_hash_provenance"]
    if local_provenance.get("manifest_integrity_verified") is not True:
        raise ValueError("Trusted local hash manifest was not verified")
    for path_key, sha_key in (("manifest_path", "manifest_sha256"), ("deduplication_path", "deduplication_sha256")):
        if digest(local_provenance[path_key]) != local_provenance[sha_key]:
            raise ValueError("The prior complete-hash evidence changed after this receipt")
    tables = {name: read_json(root / "catalog" / (name + ".json"))["items"] for name in TABLES}
    assets = {x["id"]: x for x in tables["assets"]}
    files = {x["id"]: x for x in tables["files"]}
    mapping = read_json(state / "mapping.json")["files"]
    registry = Registry(state)
    receipt_sha = digest(receipt_path)
    seen_videos, seen_assets = set(), set()
    prepared = []
    for row in receipt["exact_existing_mappings"]:
        asset, source = assets[row["asset_id"]], files[row["file_id"]]
        youtube = row["youtube"]
        if asset["accession"] != row["asset_accession"] or source["accession"] != row["file_accession"] or source["asset_id"] != asset["id"] or asset["file_id"] != source["id"]:
            raise ValueError("Receipt source/file/asset identity differs from this catalogue")
        if youtube["video_id"] in seen_videos or asset["id"] in seen_assets:
            raise ValueError("Duplicate video or asset in exact-mapping receipt")
        seen_videos.add(youtube["video_id"])
        seen_assets.add(asset["id"])
        sha = row["source_sha256"]
        if source.get("sha256") != sha or youtube["description_source_sha256"] != sha:
            raise ValueError("Description and catalogued complete source SHA-256 differ")
        checks = row["local_source_verification"]["checks"]
        if not checks or any(value is not True for value in checks.values()):
            raise ValueError("Local fingerprint/hash provenance has an unresolved check")
        location = mapping[source["id"]]
        stat = Path(location["path"]).stat()
        if stat.st_size != row["source_bytes"] or stat.st_mtime_ns != row["source_mtime_ns"] or stat.st_size != location["bytes"] or stat.st_mtime_ns != location["mtime_ns"]:
            raise ValueError("Source changed since the reviewed fingerprint receipt")
        if youtube["channel_id"] != provenance["target_channel_id"] or youtube["visibility"] != "public" or youtube["source_mapping_status"] != "exact_description_source_sha256_match":
            raise ValueError("Receipt does not contain the requested exact public Studio mapping")
        if youtube.get("existing_video_not_new_upload") is not True or youtube.get("uploaded_stream_hash_verified") is not False or youtube.get("playback_status") != "not_checked" or youtube.get("caption_status") != "not_checked":
            raise ValueError("Receipt overstates new-upload, stream, playback or caption verification")
        identity = registry.get("publication", youtube["channel_id"] + "|" + youtube["video_id"])
        publication = {**identity, "kind": "publication", "title": asset["accession"] + " · 既有 YouTube 视频", "asset_id": asset["id"], "file_id": source["id"], "channel_id": youtube["channel_id"], "youtube_video_id": youtube["video_id"], "url": youtube["url"], "visibility": youtube["visibility"], "record_basis": "youtube_studio_metadata_readback", "platform_readback_verification": "verified", "verification_scope": "metadata_only", "source_mapping_status": youtube["source_mapping_status"], "source_mapping_basis": "studio_description_source_sha256_matches_locally_revalidated_previous_full_hash", "source_sha256": sha, "description_source_sha256": youtube["description_source_sha256"], "uploaded_stream_hash_verified": False, "publication_origin": "pre_existing", "new_upload_this_batch": False, "new_upload_completed": False, "playback_status": "not_checked", "caption_publication_status": "not_checked", "subtitle_ids": [], "recorded_at": recorded_at, "platform_observed_at": observed_at, "observation_timestamp_status": "recorded" if observed_at else "not_supplied_by_source_receipt", "observation_receipt": {"sha256": receipt_sha, "schema_version": receipt["schema_version"], "interface": provenance["interface"], "raw_studio_rows_persisted": provenance["raw_studio_rows_persisted_with_this_receipt"]}, "in_current_inventory": True}
        old = next((x for x in tables["publications"] if x["id"] == publication["id"]), None)
        if old and (old["asset_id"] != asset["id"] or old["file_id"] != source["id"]):
            raise ValueError("Existing video association cannot be silently rebound")
        prepared.append((asset, publication))
    if len(prepared) != receipt["summary"]["exact_existing_source_mappings"]:
        raise ValueError("Receipt exact-match summary disagrees with its rows")
    for asset, publication in prepared:
        tables["publications"] = [x for x in tables["publications"] if x["id"] != publication["id"]] + [publication]
        if publication["id"] not in asset["youtube_publication_ids"]:
            asset["youtube_publication_ids"].append(publication["id"])
        asset["youtube_metadata_association_status"] = "pre_existing_public_video_source_description_sha256_matched"
    for name in ("publications", "assets"):
        write_json(root / "catalog" / (name + ".json"), {"schema_version": 1, "items": tables[name]})
    summary = read_json(root / "catalog/summary.json")
    summary["counts"]["publications"] = len(tables["publications"])
    summary["current_inventory_counts"]["publications"] = len(tables["publications"])
    summary.update(publication_summary(tables["publications"]))
    write_json(root / "catalog/summary.json", summary)
    registry.save()
    render_indexes(root, tables, summary)
    render_caption_index(root, tables)
    render_publications(root, tables)
    result = validate(root)
    if not result["valid"]:
        raise ValueError("Platform metadata release failed validation: " + json.dumps(result["errors"], ensure_ascii=False))
    return {**publication_summary(tables["publications"]), "source_holds_and_content_reviews_changed": False, "live_hash_cache_read_or_modified": False, "valid": True}


def render_publications(root, tables):
    assets = {x["id"]: x for x in tables["assets"]}
    files = {x["id"]: x for x in tables["files"]}
    verified = [x for x in tables["publications"] if x.get("platform_readback_verification") == "verified"]
    lines = ["# 已关联的既有 YouTube 视频", "", f"当前有 {len(verified)} 个既有公开视频，已从目标频道的 YouTube Studio 表格回读可见性，并把描述中的完整源 SHA-256 与本地可信完整哈希核对。", "", "这只是频道、视频 ID、可见性及描述源哈希的元数据关联，不是对线上转码流做哈希，也不是全片播放或字幕可用性核验。本轮新上传完成数仍为 0。精确观测时刻未随原始回读材料保存，因此条目保留空值；记录时间没有代替观测时间。", "", "| 视频档案 | 物理文件 | 公共播放页 | 来源 | 播放 / 在线字幕 |", "|---|---|---|---|---|"]
    for pub in sorted(verified, key=lambda x: assets[x["asset_id"]]["accession"]):
        asset, source = assets[pub["asset_id"]], files[pub["file_id"]]
        lines.append("| " + asset["accession"] + " | " + source["accession"] + " | [" + pub["youtube_video_id"] + "](" + pub["url"] + ") | 频道既有视频；描述源 SHA 匹配 | 均未检查 |")
    lines += ["", "这些既有平台元数据关联不证明视频内容属于某个 P4 项目，不能作为新的上传白名单。官网实质项目关联仍须逐项核实，家庭排除规则优先。", "", "机器可读证据、回读范围和私有来源收据的 SHA-256 见 [发布记录](../catalog/publications.json)。这些链接不自动解除素材的内容审阅或其他保留状态。", "", "[返回档案目录](../INDEX.md) · [本地公开字幕](../captions/README.md)", ""]
    target = root / "publications/README.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--receipts", required=True)
    args = parser.parse_args()
    print(json.dumps(merge_receipts(args.root, args.local_state, args.receipts), ensure_ascii=False))


if __name__ == "__main__":
    main()
