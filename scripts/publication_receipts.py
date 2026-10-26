#!/usr/bin/env python3
"""Project explicit local historical receipts; never contact a platform or read media."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SHA = re.compile(r"[a-f0-9]{64}\Z")
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
SOURCE = re.compile(r"[a-z][a-z0-9._-]{1,100}\Z")
CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
EXTERNAL = re.compile(r"[A-Za-z0-9_.:-]+\Z")
STATES = {
    "public", "public_processed", "public_processed_verified", "browser_queued",
    "queued", "prepared", "blocked", "uploaded", "processing", "pending",
    "all_three_public_and_playlist_verified", "private", "unlisted", "failed",
    "not_recorded", "other_recorded_state",
}
NAMESPACE = uuid.UUID("c9db1c5c-4a4a-53c6-b67f-d46bdac6fdd7")
TITLES = {
    "vortex": "VORTEX source archive historical publication ledger",
    "human-surrender": "Human Surrender 5.14 historical upload receipt",
    "fa": "He Fa / Fa re-review historical publication receipt",
}
REASONS = {
    "source_version_sha256_missing", "platform_video_id_missing",
    "no_catalogue_file_with_same_sha256_and_bytes",
    "explicit_identity_mapping_required", "project_not_attached_to_current_catalogue",
}


class ReceiptError(ValueError):
    pass


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write(path, value):
    """Validate before calling; explicit UTF-8/LF and deterministic bytes on Windows too."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded(value))
    temporary.replace(path)


def guard_outputs(paths, inputs, catalog_dir):
    paths = [Path(p).resolve() for p in paths if p]
    protected = {Path(p).resolve() for p in inputs if p}
    protected.update(Path(catalog_dir, name + ".json").resolve() for name in (
        "projects", "collections", "events", "assets", "files", "subtitles", "checksums",
        "publications", "summary", "byte-equality-groups"))
    protected.add((Path(catalog_dir).parent / ".p4-public-scope.json").resolve())
    if len(paths) != len(set(paths)) or any(p in protected for p in paths):
        raise ReceiptError("Output would overwrite an input or an existing catalog entity table")


def table(kind, items):
    return {"schema_version": 1, "kind": kind, "items": sorted(items, key=lambda r: r["id"])}


def required_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ReceiptError(f"{label}: missing or non-public fields")


def valid_sha(value, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ReceiptError("Expected a complete lowercase SHA-256")


def valid_bytes(value, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < 0:
        raise ReceiptError("Expected non-negative integer bytes")


def timestamp(value):
    if value is None or value == "":
        return None
    try:
        if type(value) in (int, float):
            result = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError("missing timezone")
        else:
            raise ValueError("invalid timestamp type")
        return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise ReceiptError("Receipt timestamp must have a timezone or be Unix seconds") from None


def state(value):
    return value if isinstance(value, str) and value in STATES else "other_recorded_state"


def reported_upload_time(value):
    """Keep an original local-time claim without inventing its missing timezone."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9T:.+Z-]+", value):
        raise ReceiptError("Invalid reported upload time")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReceiptError("Invalid reported upload time") from None
    return value


def records(document):
    rows = document.get("assets")
    if isinstance(rows, dict):
        # Retain original insertion order for a JSON pointer without exposing private keys.
        return list(rows.values())
    if isinstance(rows, list):
        return rows
    raise ReceiptError("Receipt must contain an assets array or object")


def catalogue(directory):
    directory = Path(directory)
    result = {}
    for name in ("projects", "assets", "files", "checksums"):
        rows = load(directory / (name + ".json")).get("items", [])
        if len({r["id"] for r in rows}) != len(rows):
            raise ReceiptError(f"Duplicate catalog {name} IDs")
        result[name] = {r["id"]: r for r in rows}
    policy_path = directory.parent / ".p4-public-scope.json"
    result["scope_policy"] = load(policy_path) if policy_path.is_file() else None
    if result["scope_policy"] is not None and result["scope_policy"].get("projection_required") is not True:
        raise ReceiptError("Public scope policy must require projection")
    if result["scope_policy"] is None and any("official_media_affiliation_status" in a for a in result["assets"].values()):
        raise ReceiptError("A projected catalog is missing its public scope policy")
    publication_path = directory / "publications.json"
    if result["scope_policy"] is not None and not publication_path.is_file():
        raise ReceiptError("Projected catalog is missing its current publication table")
    result["publications"] = {r["id"]: r for r in load(publication_path)["items"]} if publication_path.is_file() else {}
    return result


def denied_identity(cat, identity):
    policy = cat.get("scope_policy") or {}
    return hashlib.sha256(identity.encode("utf-8")).hexdigest() in policy.get("excluded_identity_sha256s", [])


def scope_header(cat, withheld_count):
    policy = cat.get("scope_policy")
    return {"mode": "mandatory_public_projection" if policy else "no_projection_policy_present",
            "policy_sha256": hashlib.sha256(encoded(policy)).hexdigest() if policy else None,
            "withheld_record_count": withheld_count}


def scope_evaluation(cat, video_id, sha, size):
    """A historical public claim or project title alone cannot enter a scoped export."""
    matches = sorted(f["id"] for f in cat["files"].values()
                     if sha is not None and f.get("sha256") == sha and size is not None and f.get("bytes") == size)
    if not cat.get("scope_policy"):
        return {"basis": "no_projection_policy_present", "related_publication_ids": [], "eligible_file_ids": matches}
    retained_files = {key: f for key, f in cat["files"].items()
                      if f.get("asset_id") in cat["assets"] and not denied_identity(cat, key)
                      and not denied_identity(cat, f["asset_id"])}
    related = [p for p in cat.get("publications", {}).values()
               if video_id is not None and p.get("youtube_video_id") == video_id and p.get("visibility") == "public"
               and p.get("file_id") in retained_files and p.get("asset_id") == retained_files[p["file_id"]]["asset_id"]
               and not denied_identity(cat, p["id"])]
    official = []
    for file_id in matches:
        if file_id not in retained_files:
            continue
        f = retained_files[file_id]
        a = cat["assets"][f["asset_id"]]
        proof = a.get("official_media_evidence", {})
        if (a.get("official_media_affiliation_status") != "confirmed_official_source_and_project_context"
                or proof.get("decision") not in ("confirmed_project_media", "confirmed_p4_practice_media") or proof.get("scope") not in ("project_media_provenance_only", "item_level_project_or_practice_provenance_only", "official_context_plus_actual_media_cross_review")
                or proof.get("file_id") != file_id or proof.get("asset_id") != a["id"] or proof.get("source_sha256") != sha
                or (proof.get("source_bytes") is not None and proof["source_bytes"] != size)):
            continue
        domains = {urlparse(e.get("url", "")).hostname for e in proof.get("evidence", [])
                   if e.get("http_status") == 200 and urlparse(e.get("url", "")).scheme == "https"}
        if proof.get("scope") == "official_context_plus_actual_media_cross_review":
            review = proof.get("actual_content_review", {})
            if review.get("source_sha256") != sha or review.get("completed") is not True or not review.get("reviewed_at") or not review.get("coverage"):
                continue
        if {"a.p4theater.cn", "b.p4theater.cn"}.intersection(domains):
            official.append(file_id)
    publication_files = [p["file_id"] for p in related if p["file_id"] in matches and p.get("source_sha256") == sha]
    eligible = sorted(set(official + publication_files))
    if official:
        basis = "official_source_version_evidence"
    elif related:
        basis = "existing_publication_metadata_only"
    else:
        return None
    return {"basis": basis, "related_publication_ids": sorted(p["id"] for p in related), "eligible_file_ids": eligible}


def check_projects(project_ids, refs, cat):
    if len(set(project_ids)) != len(project_ids):
        raise ReceiptError("Duplicate project IDs")
    if any(p not in cat["projects"] for p in project_ids):
        raise ReceiptError("Unknown catalog project ID; no new project is allocated")
    if any(denied_identity(cat, p) for p in project_ids):
        raise ReceiptError("Project identity is excluded by the current public scope")
    for ref in refs:
        required_keys(ref, ("namespace", "id"), "external project reference")
        if any(not isinstance(v, str) or not EXTERNAL.fullmatch(v) for v in ref.values()):
            raise ReceiptError("External project reference is not a public identifier")


def platform_observation(adapter, row):
    """Only classify evidence already in this receipt; never upgrade operator claims."""
    status, observed, privacy, processing, playback = "not_recorded", None, "not_recorded", "not_recorded", "not_recorded"
    if adapter == "human-surrender" and row.get("remote_observed_at") is not None:
        if row.get("metadata_verified") is True or row.get("privacy_status") in {"public", "unlisted", "private"}:
            status = "receipt_reports_platform_observation"
            observed = timestamp(row["remote_observed_at"])
            privacy = row.get("privacy_status", "not_recorded")
            processing = row.get("upload_status") if row.get("upload_status") in {"processed", "processing", "uploaded", "failed"} else "not_recorded"
    elif adapter == "fa" and isinstance(row.get("metadataReadback"), dict) and row["metadataReadback"]:
        status = "receipt_reports_platform_observation"
        observed = timestamp(row.get("updatedAt"))
        privacy = row.get("actualPrivacyStatus") if row.get("actualPrivacyStatus") in {"public", "unlisted", "private"} else "not_recorded"
        # Evidence strings/raw replies are deliberately not copied or interpreted.
        watched = row.get("publicWatchPlaybackVerified")
        if isinstance(watched, dict) and watched:
            playback = "receipt_contains_playback_evidence"
    return {"status": status, "observed_at": observed, "privacy": privacy,
            "processing": processing, "playback": playback}


def preview(receipt, adapter, source_id, cat, project_ids=(), external_project_refs=()):
    """Return two public tables from one explicit receipt. Does not open source media."""
    if adapter not in TITLES or not SOURCE.fullmatch(source_id):
        raise ReceiptError("Unknown adapter or invalid public source ID")
    project_ids = sorted(project_ids)
    refs = sorted(external_project_refs, key=lambda r: (r["namespace"], r["id"]))
    check_projects(project_ids, refs, cat)
    raw = Path(receipt).read_bytes()
    receipt_sha = hashlib.sha256(raw).hexdigest()
    document = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise ReceiptError("Receipt must be an object")
    rows = records(document)
    channel = document.get("channel_id", document.get("channelId"))
    if channel is not None and (not isinstance(channel, str) or not CHANNEL.fullmatch(channel)):
        raise ReceiptError("Invalid channel ID")
    updated = timestamp(document.get("updated_at", document.get("updatedAt")))
    items, omitted, scope_withheld = [], 0, 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReceiptError("Asset receipt record must be an object")
        claim = state(row.get("state", row.get("status", "not_recorded")))
        privacy = row.get("privacy_status", row.get("actualPrivacyStatus"))
        if claim in {"private", "unlisted"} or privacy in {"private", "unlisted"}:
            omitted += 1
            continue
        # Do not expose a queued/unknown/private video ID as a public destination.
        video_id = (row.get("video_id", row.get("videoId")) or None) if claim in {"public", "public_processed", "public_processed_verified"} else None
        if video_id is not None and (not isinstance(video_id, str) or not VIDEO.fullmatch(video_id)):
            raise ReceiptError("Invalid public YouTube video ID")
        sha = row.get("sha256") or None
        valid_sha(sha, nullable=True)
        size = row.get("bytes")
        valid_bytes(size, nullable=True)
        public_scope = scope_evaluation(cat, video_id, sha, size)
        if public_scope is None:
            scope_withheld += 1
            continue
        version_basis = "explicit_receipt_fields_not_media_rehashed"
        if cat.get("scope_policy") and not public_scope["eligible_file_ids"]:
            # A repeated public video ID relates the source, not an unproven original version.
            sha, size, version_basis = None, None, "version_details_withheld_by_current_public_scope"
        # Never infer a source mapping, even if a filename or hash has a candidate match.
        matches = public_scope["eligible_file_ids"]
        reasons = ["explicit_identity_mapping_required"]
        if not sha:
            reasons.append("source_version_sha256_missing")
        if not video_id:
            reasons.append("platform_video_id_missing")
        if not matches:
            reasons.append("no_catalogue_file_with_same_sha256_and_bytes")
        if not project_ids:
            reasons.append("project_not_attached_to_current_catalogue")
        record_key = f"assets/ordinal/{index}"
        identity = video_id or record_key
        candidate_id = "publication-candidate:" + str(uuid.uuid5(NAMESPACE, source_id + ":" + identity))
        items.append({
            "id": candidate_id, "source_id": source_id, "platform": "youtube", "video_id": video_id,
            "public_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            "project_ids": project_ids, "external_project_refs": refs,
            "receipt_file_version": {"sha256": sha, "bytes": size, "basis": version_basis},
            "public_scope": public_scope,
            "historical_claim": {"state": claim, "recorded_at": timestamp(row.get("updatedAt")) or updated,
                                 "reported_upload_time": reported_upload_time(row.get("uploadedAt"))},
            "historical_platform_observation": platform_observation(adapter, row),
            "current_platform_observation": {"status": "not_performed", "observed_at": None},
            "catalogue_match": {"status": "candidate_only", "asset_id": None, "file_id": None,
                                "same_version_file_candidates": matches, "reason_codes": sorted(reasons)},
            "evidence": {"receipt_sha256": receipt_sha, "record_key": record_key},
        })
    source = {"id": source_id, "adapter": adapter, "title": TITLES[adapter], "receipt_sha256": receipt_sha,
              "channel_id": channel, "receipt_updated_at": updated, "batch_state": state(document.get("state", document.get("status", "not_recorded"))),
              "record_count": len(rows), "public_projection_count": len(items), "nonpublic_records_omitted": omitted,
              "public_scope": scope_header(cat, scope_withheld),
              "historical_state_counts": {key: sum(state(r.get("state", r.get("status", "not_recorded"))) == key for r in rows)
                                          for key in sorted({state(r.get("state", r.get("status", "not_recorded"))) for r in rows})},
              "project_ids": project_ids, "external_project_refs": refs,
              "recheck": "locate_private_receipt_by_sha256_then_repreview_no_platform_access_performed"}
    candidates = table("publication_candidates", items)
    sources = table("publication_sources", [source])
    check(candidates, sources, cat)
    return candidates, sources


def check(candidates, sources, cat):
    """Strict allowlists apply even to edited candidates, not just adapter output."""
    for value, kind in ((candidates, "publication_candidates"), (sources, "publication_sources")):
        required_keys(value, ("schema_version", "kind", "items"), kind)
        if value["schema_version"] != 1 or value["kind"] != kind or not isinstance(value["items"], list):
            raise ReceiptError("Unsupported publication table")
        if len({r["id"] for r in value["items"]}) != len(value["items"]):
            raise ReceiptError("Duplicate publication identity")
    source_map = {s["id"]: s for s in sources["items"]}
    for s in sources["items"]:
        required_keys(s, ("id", "adapter", "title", "receipt_sha256", "channel_id", "receipt_updated_at", "batch_state", "record_count", "public_projection_count", "nonpublic_records_omitted", "historical_state_counts", "public_scope", "project_ids", "external_project_refs", "recheck"), "source")
        if not SOURCE.fullmatch(s["id"]) or s["adapter"] not in TITLES or s["title"] != TITLES[s["adapter"]]:
            raise ReceiptError("Source identity or title was changed")
        if s["channel_id"] is not None and not CHANNEL.fullmatch(s["channel_id"]):
            raise ReceiptError("Invalid source channel")
        valid_sha(s["receipt_sha256"])
        timestamp(s["receipt_updated_at"])
        for key in ("record_count", "public_projection_count", "nonpublic_records_omitted"):
            valid_bytes(s[key])
        required_keys(s["public_scope"], ("mode", "policy_sha256", "withheld_record_count"), "source public scope")
        valid_bytes(s["public_scope"]["withheld_record_count"])
        if s["public_scope"] != scope_header(cat, s["public_scope"]["withheld_record_count"]):
            raise ReceiptError("Source projection does not match the current public scope policy")
        if s["record_count"] != s["public_projection_count"] + s["nonpublic_records_omitted"] + s["public_scope"]["withheld_record_count"] or s["batch_state"] not in STATES:
            raise ReceiptError("Invalid source counts or state")
        if not isinstance(s["historical_state_counts"], dict) or not set(s["historical_state_counts"]).issubset(STATES):
            raise ReceiptError("Invalid historical state counts")
        for count in s["historical_state_counts"].values():
            valid_bytes(count)
        if sum(s["historical_state_counts"].values()) != s["record_count"]:
            raise ReceiptError("Historical state counts differ from source count")
        if s["recheck"] != "locate_private_receipt_by_sha256_then_repreview_no_platform_access_performed":
            raise ReceiptError("Unexpected source recheck text")
        check_projects(s["project_ids"], s["external_project_refs"], cat)
    counts = {key: 0 for key in source_map}
    for r in candidates["items"]:
        required_keys(r, ("id", "source_id", "platform", "video_id", "public_url", "project_ids", "external_project_refs", "receipt_file_version", "public_scope", "historical_claim", "historical_platform_observation", "current_platform_observation", "catalogue_match", "evidence"), "candidate")
        if r["source_id"] not in source_map or r["platform"] != "youtube":
            raise ReceiptError("Unknown source/platform")
        source = source_map[r["source_id"]]
        counts[r["source_id"]] += 1
        if r["project_ids"] != source["project_ids"] or r["external_project_refs"] != source["external_project_refs"]:
            raise ReceiptError("Candidate/source project relationship differs")
        video_id = r["video_id"]
        if video_id is not None and (not isinstance(video_id, str) or not VIDEO.fullmatch(video_id)):
            raise ReceiptError("Invalid video ID")
        if r["public_url"] != (f"https://www.youtube.com/watch?v={video_id}" if video_id else None):
            raise ReceiptError("Public URL must match the public video ID")
        v = r["receipt_file_version"]
        required_keys(v, ("sha256", "bytes", "basis"), "receipt file version")
        valid_sha(v["sha256"], nullable=True)
        valid_bytes(v["bytes"], nullable=True)
        if v["basis"] not in {"explicit_receipt_fields_not_media_rehashed", "version_details_withheld_by_current_public_scope"}:
            raise ReceiptError("Unsupported version evidence")
        expected_scope = scope_evaluation(cat, video_id, v["sha256"], v["bytes"])
        if expected_scope is None or r["public_scope"] != expected_scope:
            raise ReceiptError("Receipt row lacks current item-level public scope evidence")
        if v["basis"] == "version_details_withheld_by_current_public_scope" and (v["sha256"] is not None or v["bytes"] is not None or not cat.get("scope_policy")):
            raise ReceiptError("Withheld source version cannot expose its SHA or size")
        if cat.get("scope_policy") and not expected_scope["eligible_file_ids"] and v["basis"] != "version_details_withheld_by_current_public_scope":
            raise ReceiptError("A source-related video ID does not authorize source version details")
        required_keys(r["historical_claim"], ("state", "recorded_at", "reported_upload_time"), "historical claim")
        if r["historical_claim"]["state"] not in STATES:
            raise ReceiptError("Unknown historical claim")
        timestamp(r["historical_claim"]["recorded_at"])
        reported_upload_time(r["historical_claim"]["reported_upload_time"])
        o = r["historical_platform_observation"]
        required_keys(o, ("status", "observed_at", "privacy", "processing", "playback"), "historical observation")
        if o["status"] not in {"not_recorded", "receipt_reports_platform_observation"} or o["privacy"] not in {"not_recorded", "public", "private", "unlisted"} or o["processing"] not in {"not_recorded", "processed", "processing", "uploaded", "failed"} or o["playback"] not in {"not_recorded", "receipt_contains_playback_evidence"}:
            raise ReceiptError("Unsupported historical observation")
        timestamp(o["observed_at"])
        if o["status"] == "not_recorded" and any(o[k] != "not_recorded" for k in ("privacy", "processing", "playback")):
            raise ReceiptError("Operator claim cannot become platform observation")
        if r["current_platform_observation"] != {"status": "not_performed", "observed_at": None}:
            raise ReceiptError("This offline adapter cannot claim current platform verification")
        m = r["catalogue_match"]
        required_keys(m, ("status", "asset_id", "file_id", "same_version_file_candidates", "reason_codes"), "candidate match")
        expected = expected_scope["eligible_file_ids"]
        if m["status"] != "candidate_only" or m["asset_id"] is not None or m["file_id"] is not None or m["same_version_file_candidates"] != expected:
            raise ReceiptError("Candidate cannot contain an established binding")
        if not isinstance(m["reason_codes"], list) or not set(m["reason_codes"]).issubset(REASONS) or "explicit_identity_mapping_required" not in m["reason_codes"]:
            raise ReceiptError("Invalid unmatched reason")
        e = r["evidence"]
        required_keys(e, ("receipt_sha256", "record_key"), "evidence")
        if e["receipt_sha256"] != source["receipt_sha256"] or not re.fullmatch(r"assets/ordinal/[0-9]+", e["record_key"]):
            raise ReceiptError("Candidate/source receipt evidence differs")
        index = int(e["record_key"].rsplit("/", 1)[1])
        if index >= source["record_count"]:
            raise ReceiptError("Receipt ordinal out of bounds")
        expected_id = "publication-candidate:" + str(uuid.uuid5(NAMESPACE, r["source_id"] + ":" + (video_id or e["record_key"])))
        if r["id"] != expected_id:
            raise ReceiptError("Candidate stable ID does not match source/video")
    if any(counts[k] != s["public_projection_count"] for k, s in source_map.items()):
        raise ReceiptError("Source projection counts do not match candidates")
    return {"passed": True, "candidate_count": len(candidates["items"]), "source_count": len(sources["items"]), "network_access": False}


def export_bindings(candidates, sources, mappings, cat):
    check(candidates, sources, cat)
    required_keys(mappings, ("schema_version", "kind", "items"), "mappings")
    if mappings["schema_version"] != 1 or mappings["kind"] != "publication_mappings" or not isinstance(mappings["items"], list):
        raise ReceiptError("Unsupported explicit mappings")
    rows = {r["id"]: r for r in candidates["items"]}
    exported, seen = [], set()
    for m in mappings["items"]:
        required_keys(m, ("candidate_id", "asset_id", "file_id", "sha256", "bytes", "identity_confirmed"), "identity mapping")
        if m["identity_confirmed"] is not True or m["candidate_id"] in seen:
            raise ReceiptError("Identity mapping must be explicit and unique")
        seen.add(m["candidate_id"])
        valid_sha(m["sha256"])
        valid_bytes(m["bytes"])
        if m["bytes"] == 0:
            raise ReceiptError("An empty file cannot establish a published video identity")
        r, a, f = rows.get(m["candidate_id"]), cat["assets"].get(m["asset_id"]), cat["files"].get(m["file_id"])
        if r is None or a is None or f is None:
            raise ReceiptError("Unknown candidate/asset/file ID")
        if a.get("file_id") != f["id"] or f.get("asset_id") != a["id"]:
            raise ReceiptError("Asset/file relationship differs from the existing catalog")
        if not r["video_id"]:
            raise ReceiptError("No public platform video ID is recorded")
        if f["id"] not in r["public_scope"]["eligible_file_ids"]:
            raise ReceiptError("File identity is not eligible under the current public scope")
        v = r["receipt_file_version"]
        if v["sha256"] != m["sha256"] or f.get("sha256") != m["sha256"] or v["bytes"] != m["bytes"] or f.get("bytes") != m["bytes"]:
            raise ReceiptError("Published receipt version and catalog file SHA/bytes must match")
        if not any(c.get("file_id") == f["id"] and c.get("algorithm") == "sha256" and c.get("value") == m["sha256"] for c in cat["checksums"].values()):
            raise ReceiptError("Catalog has no matching recorded full SHA-256 evidence")
        if not set(r["project_ids"]).issubset(a.get("project_ids", [])):
            raise ReceiptError("Confirmed file does not belong to the candidate's catalog project")
        exported.append({"id": "publication-binding:" + str(uuid.uuid5(NAMESPACE, r["id"] + ":" + f["id"])),
                         "candidate_id": r["id"], "source_id": r["source_id"], "platform": "youtube",
                         "video_id": r["video_id"], "public_url": r["public_url"], "asset_id": a["id"], "file_id": f["id"],
                         "sha256": m["sha256"], "bytes": m["bytes"], "relationship": "same_file_version",
                         "identity_basis": "explicit_mapping_and_matching_recorded_sha256_and_bytes",
                         "public_scope": r["public_scope"],
                         "historical_claim": r["historical_claim"], "historical_platform_observation": r["historical_platform_observation"],
                         "current_platform_observation": r["current_platform_observation"], "evidence": r["evidence"]})
    return table("publication_bindings", exported)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preview", help="Project one explicit private receipt; no media or network access")
    p.add_argument("--receipt", required=True)
    p.add_argument("--adapter", required=True, choices=sorted(TITLES))
    p.add_argument("--source-id", required=True)
    p.add_argument("--project-id", action="append", default=[])
    p.add_argument("--external-project-ref", action="append", default=[], metavar="NAMESPACE=ID")
    p.add_argument("--output")
    p.add_argument("--sources-output")
    p.add_argument("--append", action="store_true", help="Replace this source only in the two explicitly named public tables")
    for command in ("check", "export"):
        q = commands.add_parser(command)
        q.add_argument("--candidates", required=True)
        q.add_argument("--sources", required=True)
        if command == "export":
            q.add_argument("--mappings", required=True)
            q.add_argument("--output", required=True)
    for command in commands.choices.values():
        command.add_argument("--catalog-dir", default=str(Path(__file__).resolve().parents[1] / "catalog"))
    args = parser.parse_args(argv)
    try:
        cat = catalogue(args.catalog_dir)
        if args.command == "preview":
            if bool(args.output) != bool(args.sources_output) or (args.append and not args.output):
                raise ReceiptError("Name both --output and --sources-output, or neither")
            guard_outputs([args.output, args.sources_output], [args.receipt], args.catalog_dir)
            refs = []
            for value in args.external_project_ref:
                if "=" not in value:
                    raise ReceiptError("External project references use NAMESPACE=ID")
                namespace, identity = value.split("=", 1)
                refs.append({"namespace": namespace, "id": identity})
            candidates, sources = preview(args.receipt, args.adapter, args.source_id, cat, args.project_id, refs)
            if args.append:
                old_candidates, old_sources = load(args.output), load(args.sources_output)
                check(old_candidates, old_sources, cat)
                candidates = table("publication_candidates", [r for r in old_candidates["items"] if r["source_id"] != args.source_id] + candidates["items"])
                sources = table("publication_sources", [r for r in old_sources["items"] if r["id"] != args.source_id] + sources["items"])
                check(candidates, sources, cat)
            if args.output:
                write(args.output, candidates)
                write(args.sources_output, sources)
                print(json.dumps(check(candidates, sources, cat)))
            else:
                sys.stdout.buffer.write(encoded({"candidates": candidates, "sources": sources}))
        else:
            candidates, sources = load(args.candidates), load(args.sources)
            if args.command == "check":
                print(json.dumps(check(candidates, sources, cat)))
            else:
                guard_outputs([args.output], [args.candidates, args.sources, args.mappings], args.catalog_dir)
                result = export_bindings(candidates, sources, load(args.mappings), cat)
                write(args.output, result)
                print(json.dumps({"passed": True, "binding_count": len(result["items"]), "uploaded": False, "catalog_modified": False}))
        return 0
    except (ReceiptError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not print receipt data, file paths, sessions, or raw platform responses.
        print("Receipt operation rejected: " + (str(error) if isinstance(error, ReceiptError) else type(error).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
