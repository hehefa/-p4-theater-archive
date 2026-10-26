#!/usr/bin/env python3
"""Preserve an observed platform caption version in the private master first."""
import argparse
import json
import shutil
from pathlib import Path

from archive_catalog import Registry, aware_timestamp, digest, publication_summary, read_json, srt_metadata, write_json


def preserve(master_root, state_root, receipt_path, source_evidence_path, extra_evidence_paths):
    master, state = Path(master_root).resolve(), Path(state_root).resolve()
    if (master / ".p4-public-scope.json").exists():
        raise ValueError("Update the private master first; public scope projection remains mandatory")
    receipt, source_proof = read_json(receipt_path), read_json(source_evidence_path)
    recorded_at = aware_timestamp(receipt["recorded_at"])
    if receipt["video_id"] != source_proof["youtube_video_id"] or receipt["source_sha256_recorded"] != source_proof["source_sha256"] or receipt["target_channel_id"] != "UCp_LyzepcrZX6Cgzv2Qit4w" or receipt["new_video_uploads_performed"] != 0:
        raise ValueError("Caption receipt does not bind the existing official source")
    asset_table, file_table = read_json(master / "catalog/assets.json"), read_json(master / "catalog/files.json")
    subtitle_table, publication_table = read_json(master / "catalog/subtitles.json"), read_json(master / "catalog/publications.json")
    checksum_table = read_json(master / "catalog/checksums.json")
    assets, files, subtitles = ({x["id"]: x for x in table["items"]} for table in (asset_table, file_table, subtitle_table))
    owner = assets[source_proof["asset_id"]]
    if owner["file_id"] != source_proof["file_id"] or files[owner["file_id"]]["sha256"] != source_proof["source_sha256"]:
        raise ValueError("Official original source differs from the private master")
    registry = Registry(state)
    publication_identity = registry.get("publication", receipt["target_channel_id"] + "|" + receipt["video_id"])
    old = receipt["existing_manual_caption"]
    if not old["unchanged_online"] or not all(receipt["preservation"][key] is False for key in ("old_track_deleted", "old_track_overwritten", "old_track_renamed")):
        raise ValueError("Existing-track preservation was not observed")
    downloaded = Path(old["before_copy"]["path"]).resolve()
    if state not in downloaded.parents:
        raise ValueError("Downloaded pre-existing version must remain in private state")
    meta = srt_metadata(downloaded)
    expected = old["before_copy"]
    if meta["sha256"] != expected["sha256"] or meta["bytes"] != expected["bytes"] or meta["cue_count"] != expected["cue_count"] or meta["format_status"] != "structure_passed":
        raise ValueError("Downloaded old-platform caption bytes/format differ from receipt")
    key = "platform|" + receipt["target_channel_id"] + "|" + receipt["video_id"] + "|" + old["platform_language"] + "|" + old["actual_name"] + "|" + meta["sha256"]
    legacy_id = registry.get("subtitle", key)
    relative = "captions/" + legacy_id["accession"] + "/" + meta["sha256"] + "/" + legacy_id["accession"] + ".srt"
    legacy = {**legacy_id, "kind": "subtitle", "title": "既有平台原语字幕版本（审校程度未知）", "asset_ids": [owner["id"]], "source_asset_id": owner["id"], "source_file_id": owner["file_id"], "language": "zh", "role": "platform_original_language_legacy", "clock": "source_zero", "version_sha256": meta["sha256"], "bytes": meta["bytes"], "cue_count": meta["cue_count"], "format_status": meta["format_status"], "quality_label": "platform_review_level_unknown", "human_listening_review": "not_recorded", "public_text_status": "public_platform_version_review_unknown", "youtube_caption_status": "pre_existing_published_track_preserved_menu_observed", "pairing_status": "same_video_language_and_actual_track_name_download_observed", "platform_origin": "downloaded_pre_existing_published_track", "platform_video_id": receipt["video_id"], "platform_language_label": old["platform_language"], "platform_track_actual_name": old["actual_name"], "existing_platform_label_is_not_review_evidence": True, "platform_track_observation": {"video_id": receipt["video_id"], "actual_name": old["actual_name"], "language_label": old["platform_language"], "download_sha256": meta["sha256"], "unchanged_online": True, "viewer_menu_selectable": True}, "public_export": {"path": relative, "sha256": meta["sha256"], "scope_review_status": "approved_same_official_source_platform_version_preservation", "quality_label": "platform_review_level_unknown", "official_evidence_source_sha256": source_proof["source_sha256"]}, "in_current_inventory": True}
    target = master / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(downloaded, target)
    existing = next((i for i, x in enumerate(subtitle_table["items"]) if x["id"] == legacy["id"]), None)
    if existing is None:
        subtitle_table["items"].append(legacy)
    else:
        subtitle_table["items"][existing] = legacy
    if legacy["id"] not in owner["subtitle_ids"]:
        owner["subtitle_ids"].append(legacy["id"])
    owner["platform_subtitle_ids"] = list(dict.fromkeys(owner.get("platform_subtitle_ids", []) + [legacy["id"]]))
    owner["public_caption_subtitle_ids"] = list(dict.fromkeys(owner.get("public_caption_subtitle_ids", []) + [legacy["id"]]))
    tracks = []
    for observed in receipt["tracks"]:
        sub = subtitles[observed["subtitle_id"]]
        if sub["version_sha256"] != observed["sha256"] or sub["cue_count"] != observed["cue_count"] or sub["source_file_id"] != owner["file_id"] or observed["studio_publication_status"] != "published_verified_in_language_details" or observed["viewer_menu_selectable"] != "verified":
            raise ValueError("New caption version identity/publication observation mismatch")
        track = {"subtitle_id": sub["id"], "version_sha256": sub["version_sha256"], "actual_track_name": observed["actual_track_name"], "platform_language_label": observed["actual_platform_language"], "platform_track_id": None, "published_this_update": True, "studio_published": True, "viewer_menu_selectable": True, "quality_label": "machine_draft", "human_line_by_line_listening_review": "not_performed", "render_sample_status": observed["viewer_sample_display"]}
        tracks.append(track)
        sub["quality_label"] = "machine_draft"
        sub["youtube_caption_status"] = "published_menu_verified_render_sampling_incomplete"
        sub["platform_track_observations"] = [{"publication_id": publication_identity["id"], **track}]
    tracks.append({"subtitle_id": legacy["id"], "version_sha256": legacy["version_sha256"], "actual_track_name": old["actual_name"], "platform_language_label": old["platform_language"], "platform_track_id": None, "published_this_update": False, "legacy_platform_version": True, "unchanged_online": True, "studio_published": True, "viewer_menu_selectable": True, "quality_label": "platform_review_level_unknown", "human_line_by_line_listening_review": "not_performed", "render_sample_status": "not_checked"})
    original = next(x for x in receipt["tracks"] if x["variant"] == "zh")
    if receipt["viewer_sample_observation"]["variant"] != "zh" or receipt["viewer_sample_observation"]["time"] != "00:00:12" or receipt["viewer_sample_observation"]["caption_text_visible"] is not True:
        raise ValueError("Expected limited original-language render observation is absent")
    pub = {**publication_identity, "kind": "publication", "title": owner["accession"] + " · 既有官方视频与独立字幕版本", "asset_id": owner["id"], "file_id": owner["file_id"], "channel_id": receipt["target_channel_id"], "youtube_video_id": receipt["video_id"], "url": receipt["video_url"], "visibility": "public", "record_basis": "youtube_studio_and_viewer_caption_observation", "platform_readback_verification": "verified", "verification_scope": "metadata_and_caption_menu_with_limited_render_sample", "source_sha256": source_proof["source_sha256"], "source_mapping_status": "exact_official_archive_source_sha256_match", "uploaded_stream_hash_verified": False, "publication_origin": "pre_existing", "new_upload_this_batch": False, "new_upload_completed": False, "playback_status": "not_checked", "caption_publication_status": "published_menu_verified_partial_render_only", "subtitle_ids": [x["subtitle_id"] for x in tracks], "caption_tracks": tracks, "caption_render_samples": [{"subtitle_id": original["subtitle_id"], "at_seconds": 12, "caption_text_visible": True, "scope": "visual_original_language_caption_only_not_line_by_line_listening"}], "official_source_evidence": {"source_sha256": source_proof["source_sha256"], "youtube_video_id": source_proof["youtube_video_id"], "evidence_path": "evidence/" + owner["accession"] + "-official-media.json"}, "recorded_at": recorded_at, "platform_observed_at": None, "observation_receipt": {"sha256": digest(receipt_path), "interface": "YouTube Studio caption editor and watch-player menu", "screenshots_retained": False}, "incomplete_checks": list(receipt["incomplete_checks"]), "human_continuous_watch": "not_recorded", "human_line_by_line_listening_review": "not_performed", "in_current_inventory": True}
    publication_table["items"] = [x for x in publication_table["items"] if x["id"] != pub["id"]] + [pub]
    owner["youtube_publication_ids"] = list(dict.fromkeys(owner["youtube_publication_ids"] + [pub["id"]]))
    mapping = read_json(state / "mapping.json")["files"]
    for path in extra_evidence_paths:
        proof = read_json(path)
        f = files[proof["file_id"]]
        a = assets[proof["asset_id"]]
        fp = proof["local_source_fingerprint"]
        stat = Path(mapping[f["id"]]["path"]).stat()
        if proof["family_exclusion_guard"]["matched"] is not False or fp["current_size_mtime_match_complete_hash_record"] is not True or fp["sha256"] != proof["source_sha256"] or stat.st_size != proof["source_bytes"] or stat.st_size != mapping[f["id"]]["bytes"] or stat.st_mtime_ns != mapping[f["id"]]["mtime_ns"] or a["file_id"] != f["id"]:
            raise ValueError("Independent official-source evidence fingerprint does not match")
        f.update(sha256=proof["source_sha256"], checksum_status="recorded_full_sha256", independent_checksum_evidence="official_item_review_complete_source_hash_with_unchanged_fingerprint")
        checksum = {**registry.get("checksum", f["id"] + "|" + f["sha256"]), "kind": "checksum", "title": "SHA-256", "file_id": f["id"], "algorithm": "sha256", "value": f["sha256"], "verification_basis": fp["verification_basis"], "verified_at": aware_timestamp(fp["hash_verified_at"])}
        checksum_table["items"] = [x for x in checksum_table["items"] if x["id"] != checksum["id"]] + [checksum]
        a.update(official_media_affiliation_status="confirmed_official_source_and_project_context", official_media_evidence=proof, new_public_upload_allowed=False, public_caption_export_allowed=False)
    for name, table in (("assets", asset_table), ("files", file_table), ("subtitles", subtitle_table), ("publications", publication_table), ("checksums", checksum_table)):
        write_json(master / "catalog" / (name + ".json"), table)
    summary = read_json(master / "catalog/summary.json")
    for name, table in (("subtitles", subtitle_table), ("publications", publication_table), ("checksums", checksum_table)):
        summary["counts"][name] = len(table["items"])
    summary.update(publication_summary(publication_table["items"]))
    write_json(master / "catalog/summary.json", summary)
    registry.save()
    write_json(state / "final_caption_version_registration.json", {"legacy_subtitle_id": legacy["id"], "legacy_subtitle_accession": legacy["accession"], "legacy_version_sha256": legacy["version_sha256"], "old_cues": legacy["cue_count"], "new_original_cues": original["cue_count"], "old_track_overwritten": False, "source_originals_modified": False, "live_hash_cache_read_or_modified": False, "publication_id": pub["id"]})
    return {"legacy_subtitle_id": legacy["id"], "legacy_subtitle_accession": legacy["accession"], "old_cues": legacy["cue_count"], "new_original_cues": original["cue_count"], "publication_associations": len(publication_table["items"]), "additional_metadata_evidence": len(extra_evidence_paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", required=True)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--source-evidence", required=True)
    parser.add_argument("--extra-official-evidence", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(preserve(args.master_root, args.local_state, args.receipt, args.source_evidence, args.extra_official_evidence), ensure_ascii=False))


if __name__ == "__main__":
    main()
