#!/usr/bin/env python3
"""Fail-close a private planning queue; this never uploads or controls a browser."""
import argparse
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from archive_catalog import read_json, write_json
from project_public_scope import under_prefix


def apply_guard(queue, policy, official_evidence, fingerprint_check):
    updated = deepcopy(queue)
    valid_policy = bool(policy and policy.get("active") is True and policy.get("fail_closed") is True and all(key in policy for key in ("excluded_asset_ids", "excluded_file_ids", "deny_path_prefixes")))
    excluded_assets = set(policy.get("excluded_asset_ids", [])) if valid_policy else set()
    excluded_files = set(policy.get("excluded_file_ids", [])) if valid_policy else set()
    prefixes = policy.get("deny_path_prefixes", []) if valid_policy else []
    proofs = {}
    for item in official_evidence:
        urls = [x.get("url", "") for x in item.get("evidence", []) if x.get("http_status") == 200 and re.fullmatch(r"[a-f0-9]{64}", x.get("html_utf8_sha256", ""))]
        if item.get("decision") == "confirmed_project_media" and item.get("scope") == "project_media_provenance_only" and re.fullmatch(r"[a-f0-9]{64}", item.get("source_sha256", "")) and any(x.startswith("https://a.p4theater.cn/") for x in urls) and any(x.startswith("https://b.p4theater.cn/") for x in urls):
            proofs[item["asset_id"]] = item
    denied = confirmed = caption_updates = 0
    for item in updated["items"]:
        mandatory_deny = item["asset_id"] in excluded_assets or item["file_id"] in excluded_files or any(under_prefix(item.get("source_path", ""), prefix) for prefix in prefixes)
        proof = proofs.get(item["asset_id"])
        source_matches = bool(proof and proof["file_id"] == item["file_id"] and proof["source_sha256"] == item.get("preflight", {}).get("sha256"))
        fingerprint_matches = bool(source_matches and fingerprint_check(item))
        can_update_existing_captions = bool(valid_policy and not mandatory_deny and source_matches and fingerprint_matches and proof.get("youtube_video_id"))
        # Current confirmed material already has an official archive edition.
        # Even a positive source match does not authorize another upload.
        item["new_public_upload_allowed"] = False
        item["upload_allowed"] = False
        item["public_caption_export_allowed"] = can_update_existing_captions
        item["existing_video_caption_update_allowed"] = can_update_existing_captions
        item.setdefault("proposed_upload", {})["enabled"] = False
        item["scope_guard"] = {"policy_valid": valid_policy, "mandatory_exclusion": mandatory_deny, "official_source_exact_match": source_matches, "source_fingerprint_matches": fingerprint_matches, "new_upload_allowed": False, "existing_video_id_for_caption_update": proof["youtube_video_id"] if can_update_existing_captions else None}
        if not valid_policy:
            item["scope_guard"]["reason"] = "missing_or_invalid_mandatory_policy_fail_closed"
        elif mandatory_deny:
            item["scope_guard"]["reason"] = "mandatory_family_or_official_scope_exclusion"
        elif not source_matches:
            item["scope_guard"]["reason"] = "official_media_affiliation_pending"
        elif not fingerprint_matches:
            item["scope_guard"]["reason"] = "source_fingerprint_mismatch_fail_closed"
        else:
            item["scope_guard"]["reason"] = "official_existing_video_only_no_duplicate_upload"
        denied += int(mandatory_deny)
        confirmed += int(source_matches and not mandatory_deny)
        caption_updates += int(can_update_existing_captions)
    updated["mode"] = "planning_only_scope_locked_no_new_public_uploads"
    updated["scope_guard_updated_at"] = datetime.now(timezone.utc).isoformat()
    updated["historical_pre_scope_summary"] = updated.get("historical_pre_scope_summary", updated.get("summary", {}))
    updated["summary"] = {"physical_video_items": len(updated["items"]), "mandatory_excluded_items": denied, "officially_confirmed_source_items": confirmed, "new_public_uploads_allowed": 0, "existing_video_caption_updates_allowed": caption_updates, "policy_valid": valid_policy}
    updated.setdefault("global_state", {}).update(official_item_source_evidence_required=True, candidate_or_not_denied_does_not_mean_upload_allowed=True, new_public_uploads_currently_disabled=True, existing_caption_update_does_not_authorize_new_upload=True)
    return updated


def actual_fingerprint(item):
    try:
        stat = Path(item["source_path"]).stat()
        return stat.st_size == item["source_bytes"] and stat.st_mtime_ns == item["source_inventory_mtime_ns"]
    except (OSError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--exclusions", required=True)
    parser.add_argument("--official-evidence", action="append", default=[])
    args = parser.parse_args()
    queue = read_json(args.queue)
    try:
        policy = read_json(args.exclusions)
    except (OSError, ValueError):
        policy = None
    proofs = []
    for path in args.official_evidence:
        try:
            proofs.append(read_json(path))
        except (OSError, ValueError):
            pass
    guarded = apply_guard(queue, policy, proofs, actual_fingerprint)
    write_json(args.queue, guarded)
    print(json.dumps(guarded["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
