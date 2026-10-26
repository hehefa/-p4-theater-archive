#!/usr/bin/env python3
"""Project a private master catalogue through mandatory exclusions and evidence.

Original media, private IDs, Git history and running verification caches are not
modified. Caption text is fail-closed: an exact official source and an explicit
subtitle-version allow list are both required.
"""
import argparse
import hashlib
import json
import ntpath
import shutil
from collections import Counter
from pathlib import Path

from archive_catalog import TABLES, digest, publication_summary, read_json, render_indexes, validate, write_json
from catalog_transaction import staged_update
from merge_caption_release import render_caption_index
from merge_youtube_receipts import render_publications


def windows_key(value):
    return ntpath.normcase(ntpath.normpath(str(value)))


def under_prefix(path, prefix):
    source, parent = windows_key(path), windows_key(prefix).rstrip("\\")
    return source == parent or source.startswith(parent + "\\")


def explicit_new_upload_authorization(asset_id, file_id, source_sha, source_path, policy, proof, fingerprint_matches):
    """Optional future item authorization; current policy has no such entries."""
    if not policy or policy.get("active") is not True or policy.get("fail_closed") is not True or not proof or not fingerprint_matches:
        return False
    if asset_id in policy.get("excluded_asset_ids", []) or file_id in policy.get("excluded_file_ids", []) or any(under_prefix(source_path, p) for p in policy.get("deny_path_prefixes", [])):
        return False
    if proof.get("asset_id") != asset_id or proof.get("file_id") != file_id or proof.get("source_sha256") != source_sha or proof.get("youtube_video_id"):
        return False
    for item in policy.get("new_upload_authorizations", []):
        if item.get("asset_id") == asset_id and item.get("file_id") == file_id and item.get("source_sha256") == source_sha and item.get("decision") == "approved_for_new_upload" and all(item.get(key) is True for key in ("official_context_verified", "actual_content_review_completed", "family_privacy_review_passed", "duplicate_check_completed")):
            return True
    return False


def scrub_removed_ids(value, removed):
    if isinstance(value, dict):
        return {key: scrub_removed_ids(item, removed) for key, item in value.items() if not (isinstance(item, str) and item in removed)}
    if isinstance(value, list):
        return [scrub_removed_ids(item, removed) for item in value if not (isinstance(item, str) and item in removed)]
    return value


def _project(public_root, master_root, state, exclusions_path, evidence_paths, allowed_caption_ids):
    public, master, state = Path(public_root).resolve(), Path(master_root).resolve(), Path(state).resolve()
    if master == public or public in master.parents or state == public or public in state.parents:
        raise ValueError("Private master/state must be outside the public tree")
    deny = read_json(exclusions_path)
    if deny.get("active") is not True or deny.get("fail_closed") is not True:
        raise ValueError("Mandatory exclusion policy is not active and fail-closed")
    tables = {name: read_json(master / "catalog" / (name + ".json"))["items"] for name in TABLES}
    original_summary = read_json(master / "catalog/summary.json")
    mapping = read_json(state / "mapping.json")["files"]
    denied_assets = set(deny["excluded_asset_ids"])
    denied_files = set(deny["excluded_file_ids"])
    denied_subtitles = set(deny["excluded_subtitle_ids"])
    prefixes = deny.get("deny_path_prefixes", [])
    for source in tables["files"]:
        location = mapping.get(source["id"], {}).get("path")
        if location and any(under_prefix(location, p) for p in prefixes):
            denied_files.add(source["id"])
            denied_assets.add(source["asset_id"])
    # A known byte-identical copy cannot evade a family/source exclusion by
    # moving outside the original path or acquiring another accession.
    denied_hashes = {x["sha256"] for x in tables["files"] if x["id"] in denied_files and x.get("sha256")}
    for source in tables["files"]:
        if source["asset_id"] in denied_assets or (source.get("sha256") and source["sha256"] in denied_hashes):
            denied_files.add(source["id"])
            denied_assets.add(source["asset_id"])
    for sub in tables["subtitles"]:
        if sub.get("source_file_id") in denied_files or any(a in denied_assets for a in sub.get("asset_ids", [])):
            denied_subtitles.add(sub["id"])
    selected_assets = [x for x in tables["assets"] if x["id"] not in denied_assets and x["file_id"] not in denied_files]
    selected_asset_ids = {x["id"] for x in selected_assets}
    selected_files = [x for x in tables["files"] if x["id"] not in denied_files and x["asset_id"] in selected_asset_ids]
    selected_file_ids = {x["id"] for x in selected_files}
    selected_subtitles = [x for x in tables["subtitles"] if x["id"] not in denied_subtitles and (not x.get("asset_ids") or all(a in selected_asset_ids for a in x["asset_ids"]))]
    selected_subtitle_ids = {x["id"] for x in selected_subtitles}
    selected_publications = [x for x in tables["publications"] if x["asset_id"] in selected_asset_ids and x["file_id"] in selected_file_ids]
    selected_publication_ids = {x["id"] for x in selected_publications}
    asset_by_id = {x["id"]: x for x in selected_assets}
    file_by_id = {x["id"]: x for x in selected_files}
    evidence = {}
    for evidence_path in evidence_paths:
        proof = read_json(evidence_path)
        if proof.get("decision") not in ("confirmed_project_media", "confirmed_p4_practice_media") or proof.get("scope") not in ("project_media_provenance_only", "item_level_project_or_practice_provenance_only", "official_context_plus_actual_media_cross_review"):
            raise ValueError("Official evidence must establish source-level project provenance")
        source, owner = file_by_id.get(proof["file_id"]), asset_by_id.get(proof["asset_id"])
        if not source or not owner or source["asset_id"] != owner["id"] or source.get("sha256") != proof["source_sha256"]:
            raise ValueError("Official evidence does not match a retained full-hash source")
        urls = [x["url"] for x in proof["evidence"] if x.get("http_status") == 200]
        if not any(x.startswith(("https://a.p4theater.cn/", "https://b.p4theater.cn/")) for x in urls):
            raise ValueError("Expected item-level official archive evidence")
        if proof.get("scope") == "official_context_plus_actual_media_cross_review":
            review = proof.get("actual_content_review", {})
            if review.get("source_sha256") != proof["source_sha256"] or review.get("completed") is not True or not review.get("reviewed_at") or not review.get("coverage"):
                raise ValueError("New-media affiliation needs a source-bound actual-content review")
        evidence[owner["id"]] = proof
    allowed = set()
    for reference in allowed_caption_ids:
        match = next((x for x in selected_subtitles if reference in (x["id"], x["accession"])), None)
        if not match:
            raise ValueError("Caption allow-list entry is excluded or unknown")
        allowed.add(match["id"])
    caption_copies = []
    for sub in selected_subtitles:
        owner_id = sub.get("source_asset_id")
        proof = evidence.get(owner_id)
        sensitive = "passcode" in sub.get("public_text_status", "") or "sensitive" in sub.get("public_text_status", "") or "passcode" in sub.get("content_scope_review", {}).get("status", "")
        permitted = sub["id"] in allowed and proof and sub.get("source_file_id") == proof["file_id"] and not sensitive
        if permitted:
            export = sub.get("public_export")
            if not export or digest(master / export["path"]) != sub["version_sha256"]:
                raise ValueError("Allowed caption version is missing from the private master")
            legacy = sub.get("role") == "platform_original_language_legacy"
            export["scope_review_status"] = "approved_same_official_source_platform_version_preservation" if legacy else "approved_official_source_and_text_scope"
            export["official_evidence_source_sha256"] = proof["source_sha256"]
            sub["public_text_status"] = "public_platform_version_review_unknown" if legacy else "public_machine_draft"
            sub["official_media_affiliation_status"] = "confirmed_official_source_and_project_context"
            caption_copies.append((master / export["path"], public / export["path"]))
        else:
            sub.pop("public_export", None)
            sub.pop("equivalent_public_export_subtitle_id", None)
            if proof and sub.get("role") == "platform_original_language_legacy":
                sub["public_text_status"] = "withheld_platform_legacy_text_context_review_pending"
                sub["official_media_affiliation_status"] = "confirmed_official_source_and_project_context"
                sub["privacy_screening_status"] = "initial_pattern_screen_no_identifying_format_hits_context_review_pending_not_full_review"
            else:
                sub["public_text_status"] = "withheld_sensitive_detail_and_official_evidence_pending" if sensitive else "withheld_pending_official_item_evidence"
                sub["official_media_affiliation_status"] = "official_media_affiliation_pending"
    exported_ids = {s["id"] for s in selected_subtitles if s.get("public_export")}
    if exported_ids != allowed:
        raise ValueError("Every explicit allowed subtitle must survive exclusions and have exact official source evidence")
    for asset in selected_assets:
        asset["subtitle_ids"] = [x for x in asset["subtitle_ids"] if x in selected_subtitle_ids]
        asset["youtube_publication_ids"] = [x for x in asset["youtube_publication_ids"] if x in selected_publication_ids]
        asset["public_caption_subtitle_ids"] = [x for x in asset.get("public_caption_subtitle_ids", []) if x in exported_ids]
        asset["byte_copy_asset_ids"] = [x for x in asset.get("byte_copy_asset_ids", []) if x in selected_asset_ids]
        asset["new_public_upload_allowed"] = False
        if asset["id"] in evidence:
            asset["official_media_affiliation_status"] = "confirmed_official_source_and_project_context"
            asset["official_media_evidence"] = evidence[asset["id"]]
            asset["content_classification"] = "official_p4_practice_media" if evidence[asset["id"]].get("decision") == "confirmed_p4_practice_media" else "official_project_media_confirmed"
            asset["public_caption_export_allowed"] = bool(asset["public_caption_subtitle_ids"])
            location = mapping.get(asset["file_id"], {})
            fingerprint_matches = False
            if deny.get("new_upload_authorizations") and location.get("path"):
                try:
                    stat = Path(location["path"]).stat()
                    fingerprint_matches = stat.st_size == location["bytes"] and stat.st_mtime_ns == location["mtime_ns"]
                except OSError:
                    pass
            asset["new_public_upload_allowed"] = explicit_new_upload_authorization(asset["id"], asset["file_id"], file_by_id[asset["file_id"]].get("sha256"), location.get("path", ""), deny, evidence[asset["id"]], fingerprint_matches)
        else:
            asset["official_media_affiliation_status"] = "official_media_affiliation_pending"
            asset["content_classification"] = "p4_candidate_official_media_affiliation_pending"
            asset["public_caption_export_allowed"] = False
    for pub in selected_publications:
        pub["subtitle_ids"] = [x for x in pub.get("subtitle_ids", []) if x in selected_subtitle_ids]
        pub["content_affiliation_claim"] = "platform_metadata_does_not_establish_project_content_relevance"
    event_ids = {eid for a in selected_assets for eid in a["event_ids"]}
    selected_events = [x for x in tables["events"] if x["id"] in event_ids]
    for event in selected_events:
        event["project_ids"] = sorted({pid for a in selected_assets if event["id"] in a["event_ids"] for pid in a["project_ids"]})
        event["official_media_affiliation_status"] = "asset_level_review_required_not_folder_inferred"
    collection_counts = Counter(a["collection_id"] for a in selected_assets)
    selected_collections = [x for x in tables["collections"] if x["id"] in collection_counts]
    for collection in selected_collections:
        collection["inventory_file_count"] = collection_counts[collection["id"]]
        collection["count_scope"] = "public_selected_candidates_only"
    selected_checksums = [x for x in tables["checksums"] if x["file_id"] in selected_file_ids]
    removed = denied_assets | denied_files | denied_subtitles
    for name, items in (("assets", selected_assets), ("files", selected_files), ("subtitles", selected_subtitles), ("events", selected_events), ("collections", selected_collections), ("publications", selected_publications), ("checksums", selected_checksums)):
        tables[name] = scrub_removed_ids(items, removed)
    # Equivalent caption links survive only when their exact public version does.
    for sub in tables["subtitles"]:
        if sub.get("equivalent_public_export_subtitle_id") not in exported_ids:
            sub.pop("equivalent_public_export_subtitle_id", None)
    groups = read_json(master / "catalog/byte-equality-groups.json")
    for group in groups["items"]:
        group["file_ids"] = [x for x in group["file_ids"] if x in selected_file_ids]
    groups["items"] = [x for x in groups["items"] if len(x["file_ids"]) > 1]
    master_count = original_summary.get("private_master_inventory_video_files", len([x for x in read_json(master / "catalog/files.json")["items"] if x.get("in_current_inventory", True)]))
    summary = dict(original_summary)
    summary.update(counts={name: len(items) for name, items in tables.items()}, current_inventory_counts={name: sum(x.get("in_current_inventory", True) for x in items) for name, items in tables.items()}, private_master_inventory_video_files=master_count, public_selected_video_files=len(tables["assets"]), public_excluded_video_files=master_count-len(tables["assets"]), public_candidate_catalogue_is_upload_authorization=False, video_bytes=sum(x["bytes"] for x in tables["files"]), full_sha256_recorded=sum(x.get("sha256") is not None for x in tables["files"]), full_sha256_pending=sum(x.get("sha256") is None for x in tables["files"]), full_decode_passed_existing_reports=sum(x.get("full_decode_status") == "passed_existing_report" for x in tables["files"]), subtitle_format_status=dict(Counter(x["format_status"] for x in tables["subtitles"])), public_caption_export_count=len(exported_ids), public_caption_source_file_count=len({x["source_file_id"] for x in tables["subtitles"] if x.get("public_export")}), official_media_affiliation_confirmed_assets=len(evidence), official_media_affiliation_pending_assets=len(tables["assets"])-len(evidence), public_export_policy="mandatory_exclusion_projection_and_explicit_official_source_caption_allow_list", scope="Public candidate metadata only; family/mixed candidates and separate official-scope holds excluded. New uploads and caption export require source-level official evidence and must pass family exclusion guards.")
    summary.update(publication_summary(tables["publications"]))
    if any(s.get("quality_label") == "platform_review_level_unknown" and s.get("public_export") for s in tables["subtitles"]):
        summary["public_caption_quality"] = "machine_drafts_and_distinct_legacy_platform_review_unknown_no_full_listening"
    policy = {"schema_version": 1, "projection_required": True, "decision_source": deny["decision_source"], "exclusions_sha256": digest(exclusions_path), "excluded_identity_sha256s": sorted(hashlib.sha256(x.encode()).hexdigest() for x in removed), "allowed_caption_subtitle_ids": sorted(exported_ids), "allowed_caption_versions": {s["id"]: s["version_sha256"] for s in tables["subtitles"] if s["id"] in exported_ids}, "new_material_default": "official_media_affiliation_pending_no_new_public_upload_or_caption_export", "private_master_inventory_video_files": master_count, "public_selected_video_files": len(tables["assets"]), "git_history_action": "unchanged_current_tree_projection_only"}
    # This is already a private, complete candidate supplied by staged_update.
    # Remove withdrawn text before rendering/validation, preserving SRT bytes.
    staging = public
    desired_srt_paths = {destination.resolve() for _, destination in caption_copies}
    removed_text_paths = []
    for old in (public / "captions").rglob("*.srt") if (public / "captions").exists() else []:
        if old.resolve() not in desired_srt_paths:
            if public not in old.resolve().parents:
                raise ValueError("Refusing deletion outside public tree")
            removed_text_paths.append(old.relative_to(public).as_posix())
            old.unlink()
    for name, items in tables.items():
        write_json(staging / "catalog" / (name + ".json"), {"schema_version": 1, "items": items})
    write_json(staging / "catalog/byte-equality-groups.json", groups)
    write_json(staging / "catalog/summary.json", summary)
    write_json(staging / ".p4-public-scope.json", policy)
    for owner_id, proof in evidence.items():
        safe = {key: proof[key] for key in ("schema_version", "decision", "scope", "asset_id", "asset_accession", "file_id", "file_accession", "source_filename", "source_sha256", "project_code", "project_slug", "official_context", "formal_project_claim", "official_event_description", "official_event_date", "youtube_video_id", "evidence", "unsupported_claims") if key in proof}
        write_json(staging / "evidence" / (asset_by_id[owner_id]["accession"] + "-official-media.json"), safe)
    for publication in tables["publications"]:
        if publication.get("record_basis") == "youtube_studio_and_viewer_caption_observation":
            safe = {key: publication[key] for key in ("channel_id", "youtube_video_id", "url", "asset_id", "file_id", "source_sha256", "record_basis", "verification_scope", "publication_origin", "new_upload_this_batch", "recorded_at", "platform_observed_at", "observation_receipt", "caption_tracks", "caption_render_samples", "incomplete_checks", "human_continuous_watch", "human_line_by_line_listening_review") if key in publication}
            write_json(staging / "evidence" / (publication["youtube_video_id"] + "-caption-observation.json"), safe)
    for subtitle in tables["subtitles"]:
        if subtitle.get("role") == "platform_original_language_legacy":
            safe = {key: subtitle[key] for key in ("id", "accession", "title", "source_asset_id", "source_file_id", "language", "role", "version_sha256", "bytes", "cue_count", "format_status", "quality_label", "human_listening_review", "public_text_status", "platform_video_id", "platform_language_label", "platform_track_actual_name", "existing_platform_label_is_not_review_evidence", "platform_track_observation", "privacy_screening_status") if key in subtitle}
            write_json(staging / "evidence" / (subtitle["accession"] + "-platform-version-metadata.json"), safe)
    if (master / "registry").exists():
        shutil.copytree(master / "registry", staging / "registry", dirs_exist_ok=True)
    for source, destination in caption_copies:
        staged = staging / destination.relative_to(public)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, staged)
    render_indexes(staging, tables, summary)
    render_caption_index(staging, tables)
    render_publications(staging, tables)
    result = validate(staging)
    if not result["valid"]:
        raise ValueError("Scope projection is invalid: " + json.dumps(result["errors"], ensure_ascii=False))
    old_export_ids = {s["id"] for s in read_json(master / "catalog/subtitles.json")["items"] if s.get("public_export") and s.get("role") != "platform_original_language_legacy"}
    report = {"private_master_inventory_video_files": master_count, "public_selected_video_files": len(tables["assets"]), "excluded_video_files": master_count-len(tables["assets"]), "public_subtitle_records": len(tables["subtitles"]), "public_caption_files": len(exported_ids), "current_tree_srt_files_removed_this_run": len(removed_text_paths), "previously_exported_captions_now_withheld": len(old_export_ids-exported_ids), "excluded_identity_ids_retained_privately": True, "original_media_or_sidecars_modified": False, "git_history_modified": False, "live_hash_cache_read_or_modified": False, "validation": validate(public)}
    write_json(state / "public_scope_projection_report.json", report)
    return report


def project(public_root, master_root, state, exclusions_path, evidence_paths, allowed_caption_ids):
    public, master, private = Path(public_root).resolve(), Path(master_root).resolve(), Path(state).resolve()
    if master == public or public in master.parents or private == public or public in private.parents:
        raise ValueError("Private master/state must be outside the public tree")
    return staged_update(public, lambda candidate, candidate_state: _project(candidate, master, candidate_state, exclusions_path, evidence_paths, allowed_caption_ids), validate, local_state=private)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-root", required=True)
    parser.add_argument("--master-root", required=True)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--exclusions", required=True)
    parser.add_argument("--official-evidence", action="append", default=[])
    parser.add_argument("--allow-caption", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(project(args.public_root, args.master_root, args.local_state, args.exclusions, args.official_evidence, args.allow_caption), ensure_ascii=False))


if __name__ == "__main__":
    main()
