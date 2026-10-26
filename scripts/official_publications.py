#!/usr/bin/env python3
"""Check separately approved official editions without rebinding raw inventory IDs."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import urlsplit

from public_reading import _parse_srt

OFFICIAL_HOSTS = frozenset(("a.p4theater.cn", "b.p4theater.cn", "hefa.p4theater.cn"))
DECISIONS = frozenset(("mirror_verified_official_public_edition", "publish_prepared_caption_for_verified_public_work"))
HEX = re.compile(r"[a-f0-9]{64}\Z")
# Existing raw-text approvals remain independent of the additional editions.
RAW_APPROVED_VERSIONS = {
    "subtitle:3ce0010f-c786-4350-a404-000396e16903": "0deadae5a57560a048565b79c3bb6ebac290dc688faaf903114d13229b282bb8",
    "subtitle:f12d1eca-79f8-4c2a-9da1-db408c8132cc": "7d46674002a04e5cfd1311eae555b675bf28fea752201bdc0dfc09aa62e41cd0",
    "subtitle:b5ffc023-03ac-40bc-8687-6896f71a8537": "19d8cbc069b148fb046fd62abf2158c9dc06669aad61d3410e4216870149975f",
}
BINDINGS = ("source_asset_ids", "source_file_ids", "source_subtitle_ids", "source_media_sha256")
INDEXES = frozenset(("published/README.md", "published/captions.md", "published/materials.md"))
PRIVATE_KEY = re.compile(r"private|local_path|access_token|refresh_token|client_secret|upload_session|upload_url|session_uri|cookie", re.I)
LOCAL_VALUE = re.compile(r"(?i)(?:/Users/|/Volumes/|/home/|file://|(?<![a-z0-9])[a-z]:[\\/])")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _public_metadata(value):
    if isinstance(value, dict):
        for key, child in value.items():
            _require(not PRIVATE_KEY.search(key), "Private operational metadata cannot enter public editions")
            _public_metadata(child)
    elif isinstance(value, list):
        for child in value:
            _public_metadata(child)
    elif isinstance(value, str):
        _require(not LOCAL_VALUE.search(value), "Local source paths cannot enter public editions")


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _path(root, relative, prefix):
    _require(isinstance(relative, str) and "\\" not in relative and ":" not in relative and "\x00" not in relative, "Invalid public path")
    parts = relative.split("/")
    _require(parts and parts[0] == prefix and all(p not in ("", ".", "..") for p in parts) and not PurePosixPath(relative).is_absolute(), "Path must remain inside " + prefix)
    target = root
    for part in parts:
        target = target / part
        _require(not target.is_symlink(), "Symlinks are not accepted in public editions")
    _require(target.is_file(), "Registered public file is missing: " + relative)
    return target


def _json(path):
    def reject(unused):
        raise ValueError("Non-finite JSON value is not accepted")
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject)


def _table(value, label):
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1 and isinstance(value.get("items"), list), "Invalid table: " + label)
    _require(all(isinstance(x, dict) for x in value["items"]), "Invalid table items: " + label)
    return value["items"]


def _url(value):
    _require(isinstance(value, str), "Official source URL must be a string")
    parsed = urlsplit(value)
    _require(parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS and parsed.netloc == parsed.hostname and not parsed.username and not parsed.password, "Official source requires an exact HTTPS official host")
    return value


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("Approval timestamp must include a timezone") from None
    _require(stamp.tzinfo is not None and stamp.utcoffset() is not None, "Approval timestamp must include a timezone")


def _raw_scope(root):
    policy_path = root / ".p4-public-scope.json"
    _require(policy_path.is_file() and not policy_path.is_symlink(), "Raw public scope marker must remain present")
    policy = _json(policy_path)
    _require(isinstance(policy, dict) and policy.get("projection_required") is True, "Raw public scope projection must remain enabled")
    allowed = policy.get("allowed_caption_versions")
    _require(isinstance(allowed, dict) and set(policy.get("allowed_caption_subtitle_ids", [])) == set(allowed), "Raw public caption scope is inconsistent")
    _require(all(allowed.get(k) == v for k, v in RAW_APPROVED_VERSIONS.items()), "Existing three raw caption approvals must be preserved")
    subtitles = _table(_json(_path(root, "catalog/subtitles.json", "catalog")), "raw subtitles")
    files = _table(_json(_path(root, "catalog/files.json", "catalog")), "raw files")
    assets = _table(_json(_path(root, "catalog/assets.json", "catalog")), "raw assets")
    subs = {s.get("id"): s for s in subtitles}
    for identity, digest in RAW_APPROVED_VERSIONS.items():
        sub = subs.get(identity, {})
        export = sub.get("public_export", {})
        _require(isinstance(export, dict) and sub.get("version_sha256") == digest and export.get("sha256") == digest, "Existing raw caption metadata was changed")
        raw = _path(root, export.get("path"), "captions")
        _require(_sha(raw.read_bytes()) == digest, "Existing raw caption bytes were changed")
    excluded = set(policy.get("excluded_identity_sha256s", []))
    _require(all(_hash(x) for x in excluded), "Invalid excluded identity fingerprints")
    def denied(identity):
        return isinstance(identity, str) and _sha(identity.encode("utf-8")) in excluded
    hard_media = {f["sha256"] for f in files if _hash(f.get("sha256")) and (denied(f.get("id")) or denied(f.get("asset_id")))}
    withheld = [s for s in subtitles if not s.get("public_export")]
    withheld_versions = {s["version_sha256"] for s in withheld if _hash(s.get("version_sha256"))}
    withheld_file_ids = {s.get("source_file_id") for s in withheld if s.get("source_file_id")}
    asset_files = {a.get("id"): a.get("file_id") for a in assets}
    for sub in withheld:
        for identity in sub.get("asset_ids", []):
            if asset_files.get(identity):
                withheld_file_ids.add(asset_files[identity])
    withheld_media = {f["sha256"] for f in files if f.get("id") in withheld_file_ids and _hash(f.get("sha256"))}
    maps = {"source_asset_ids": {a.get("id"): a for a in assets}, "source_file_ids": {f.get("id"): f for f in files}, "source_subtitle_ids": subs}
    return denied, hard_media, withheld_versions, withheld_media, maps


def _load_verified(root):
    root = Path(root).resolve()
    denied, hard_media, withheld_versions, withheld_media, maps = _raw_scope(root)
    manifest = _table(_json(_path(root, "published/manifest.json", "published")), "official edition manifest")
    approvals = _table(_json(_path(root, "published/approvals.json", "published")), "official edition approvals")
    _public_metadata(manifest)
    _public_metadata(approvals)
    approved = {}
    for approval in approvals:
        identity = approval.get("id")
        _require(isinstance(identity, str) and identity and identity not in approved, "Duplicate or missing approval identity")
        approved[identity] = approval
    seen, paths, folded_paths, verified, cue_count = set(), set(), set(), [], 0
    for item in manifest:
        identity = item.get("id")
        _require(isinstance(identity, str) and identity and identity not in seen, "Duplicate or missing edition identity")
        seen.add(identity)
        approval = approved.get(identity)
        _require(approval is not None, "Edition lacks a matching approval: " + identity)
        _require(isinstance(item.get("title"), str) and item["title"].strip(), "Edition title is required")
        kind = item.get("kind")
        _require(kind in ("caption", "document", "research"), "Unsupported official edition kind")
        for field in ("id", "kind", "source_url", "official_context_url", "files", "clock", *BINDINGS):
            _require(item.get(field) == approval.get(field), "Manifest/approval fields differ: " + field)
        _url(item.get("source_url")); _url(item.get("official_context_url"))
        if kind == "caption":
            _require(item.get("clock", "edition_zero") in ("edition_zero", "source_zero"), "Unsupported edition clock")
        evidence = approval.get("evidence")
        _require(isinstance(evidence, list) and evidence, "Edition approval requires a current official HTTP observation")
        valid_observation = False
        for observation in evidence:
            _require(isinstance(observation, dict), "Invalid official HTTP observation")
            _url(observation.get("url"))
            hashes = [observation[k] for k in ("sha256", "response_sha256", "html_utf8_sha256") if k in observation]
            _require(hashes and all(_hash(h) for h in hashes) and len(set(hashes)) == 1, "Official HTTP observation requires its response SHA-256")
            _timestamp(observation.get("observed_at"))
            if observation.get("url") in (item["source_url"], item["official_context_url"]) and type(observation.get("http_status")) is int and observation["http_status"] == 200:
                valid_observation = True
        _require(valid_observation, "Approval lacks a successful HTTP observation of its exact official source/context")
        decision = approval.get("decision")
        _require(decision in DECISIONS and (decision != "publish_prepared_caption_for_verified_public_work" or kind == "caption"), "Unsupported edition approval decision")
        _timestamp(approval.get("observed_at"))
        if decision == "publish_prepared_caption_for_verified_public_work":
            _require(item.get("quality_label") == "local_machine_draft_unreviewed", "Prepared caption must retain its unreviewed local machine-draft label")
        _require(not denied(identity), "Excluded identity cannot enter official editions")
        for field in BINDINGS[:3]:
            ids = item.get(field, [])
            _require(isinstance(ids, list) and all(isinstance(v, str) and v for v in ids), "Invalid source identity references")
            _require(not any(denied(v) for v in ids), "Edition references an excluded raw identity")
            _require(len(set(ids)) == len(ids) and all(v in maps[field] for v in ids), "Edition references an unknown or duplicate raw identity")
        declared_assets = set(item.get("source_asset_ids", []))
        declared_files = set(item.get("source_file_ids", []))
        referenced_files = set(declared_files)
        for identity in declared_assets:
            referenced_files.add(maps["source_asset_ids"][identity].get("file_id"))
        for identity in declared_files:
            owner = maps["source_file_ids"][identity].get("asset_id")
            _require(owner in maps["source_asset_ids"] and (not declared_assets or owner in declared_assets), "Edition source file/asset ownership disagrees")
        for identity in item.get("source_subtitle_ids", []):
            sub = maps["source_subtitle_ids"][identity]
            owner_ids = {sub["source_asset_id"]} if sub.get("source_asset_id") else set(sub.get("asset_ids", []))
            _require(owner_ids and owner_ids.issubset(maps["source_asset_ids"]) and (not declared_assets or owner_ids.issubset(declared_assets)), "Edition source subtitle/asset ownership disagrees")
            if sub.get("source_file_id"):
                source_file = sub["source_file_id"]
                _require(source_file in maps["source_file_ids"] and maps["source_file_ids"][source_file].get("asset_id") in owner_ids and (not declared_files or source_file in declared_files), "Edition source subtitle/file ownership disagrees")
                referenced_files.add(source_file)
            else:
                referenced_files.update(maps["source_asset_ids"][a].get("file_id") for a in owner_ids)
        media_sha = item.get("source_media_sha256")
        if media_sha is not None:
            _require(_hash(media_sha), "Invalid edition source media hash")
            _require(media_sha not in hard_media, "Excluded source media cannot enter official editions")
            _require(all(f in maps["source_file_ids"] and maps["source_file_ids"][f].get("sha256") == media_sha for f in referenced_files), "Edition source media SHA disagrees with declared raw identities")
            if media_sha in withheld_media:
                review = approval.get("source_media_review", {})
                _require(isinstance(review, dict) and review.get("decision") == "approved_official_public_edition_after_scope_review" and review.get("source_media_sha256") == media_sha and review.get("official_context_url") == item["official_context_url"], "Withheld source media requires an explicit edition scope review")
                _timestamp(review.get("reviewed_at"))
        records = item.get("files")
        _require(isinstance(records, list) and records, "Edition must register at least one versioned file")
        for record in records:
            _require(isinstance(record, dict) and _hash(record.get("sha256")) and type(record.get("bytes")) is int and record["bytes"] > 0, "Invalid edition file hash or bytes")
            relative = record.get("path")
            target = _path(root, relative, "published")
            _require(relative.casefold() not in folded_paths and relative not in INDEXES, "Duplicate file or index registered as edition text")
            suffix = target.suffix.lower()
            expected = "published/captions/" if kind == "caption" else "published/materials/"
            _require(relative.startswith(expected) and suffix in ((".srt",) if kind == "caption" else (".md", ".txt")), "Edition file kind and path disagree")
            body = target.read_bytes()
            _require(len(body) == record["bytes"] and _sha(body) == record["sha256"], "Edition bytes/hash differ from approval: " + relative)
            text = body.decode("utf-8-sig")
            _require(bool(text.strip()), "Edition text is empty")
            _require(record["sha256"] not in withheld_versions, "Withheld raw caption version cannot enter official editions")
            cues = []
            if kind == "caption":
                cues = _parse_srt(body, identity)
                _require(bool(cues), "Caption edition must contain at least one cue")
                cue_count += len(cues)
            paths.add(relative)
            folded_paths.add(relative.casefold())
            verified.append((item, record, text, cues))
    _require(seen == set(approved), "Manifest and approval identity sets differ")
    for path in (root / "published").rglob("*"):
        _require(not path.is_symlink(), "Symlinks are not accepted in public editions")
        if path.is_file() and path.suffix.lower() in (".srt", ".md", ".txt"):
            relative = path.relative_to(root).as_posix()
            _require(relative in paths or relative in INDEXES, "Unregistered official edition text: " + relative)
    return {"valid": True, "errors": [], "item_count": len(manifest), "file_count": len(paths), "caption_cue_count": cue_count, "preserved_raw_caption_count": len(RAW_APPROVED_VERSIONS)}, verified


def check(root):
    try:
        result, unused = _load_verified(root)
        return result
    except (ValueError, OSError, UnicodeError) as exc:
        return {"valid": False, "errors": [str(exc)]}
    except (TypeError, KeyError):
        return {"valid": False, "errors": ["Invalid official publication data shape"]}


def search(root, query, limit=20):
    _require(isinstance(query, str) and query.strip() and type(limit) is int and 0 < limit <= 100, "Search needs text and a limit from 1 to 100")
    result, editions = _load_verified(root)
    normalize = lambda s: unicodedata.normalize("NFKC", s).casefold()
    needle, matches = normalize(query), []
    for item, file, text, cues in editions:
        base = {"id": item["id"], "kind": item["kind"], "title": item["title"], "path": file["path"], "sha256": file["sha256"], "source_url": item["source_url"], "official_context_url": item["official_context_url"], "quality_label": item.get("quality_label", "not_recorded")}
        if cues:
            for cue in cues:
                if needle in normalize(cue["text"]):
                    matches.append({**base, "clock": item.get("clock", "edition_zero"), "cue": cue["cue"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"]})
        else:
            for paragraph in text.split("\n\n"):
                if needle in normalize(paragraph):
                    matches.append({**base, "text": paragraph[:1000]})
    return {"valid": True, "query": query, "match_count": len(matches), "matches": matches[:limit]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("--root", default=".")
    search_parser = commands.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--root", default=".")
    search_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        result = check(args.root) if args.command == "check" else search(args.root, args.query, args.limit)
    except (ValueError, OSError, UnicodeError) as exc:
        result = {"valid": False, "errors": [str(exc)]}
    except (TypeError, KeyError):
        result = {"valid": False, "errors": ["Invalid official publication data shape"]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
