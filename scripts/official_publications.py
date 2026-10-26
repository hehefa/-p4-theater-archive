#!/usr/bin/env python3
"""Check separately approved official editions without rebinding raw inventory IDs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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
INDEXES = frozenset(("published/README.md", "published/captions.md", "published/materials.md", "published/wechat.md"))
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


def _label(value):
    return str(value or "").replace("\\n", " ").replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _edition_indexes(root):
    unused, verified = _load_verified(root)
    items = {}
    for item, record, unused_text, unused_cues in verified:
        items[item["id"]] = item
    counts = Counter(item["kind"] for item in items.values())
    captions = defaultdict(list)
    for item in items.values():
        if item["kind"] == "caption":
            captions[item.get("youtube_id", item["id"])].append(item)
    wechat = [item for item in items.values() if item.get("subtype") in ("wechat_article", "wechat_backup_material")]
    other = [item for item in items.values() if item["kind"] != "caption" and item not in wechat]

    def files(item):
        links = []
        for record in item["files"]:
            language = record.get("language", Path(record["path"]).suffix[1:].upper())
            links.append("[" + _label(language) + "](" + record["path"].removeprefix("published/") + ")")
        return " · ".join(links)

    lines = ["# 已完成的作品字幕", "", f"{counts['caption']}份SRT，{len(captions)}个准确视频版本。文件数量包括独立语言和另存的字节版本，不是作品总数。", "",
             "每份保留来源、版本SHA和原有质量说明。历史平台回执、本次官网回读与YouTube字幕当前状态分别保存在逐项清单；本页不把本地成品写成已在YouTube上线。", "",
             "| 作品与准确版本 | 字幕下载 | 出处 |", "| --- | --- | --- |"]
    for identity, group in sorted(captions.items(), key=lambda pair: (pair[1][0]["title"].casefold(), pair[0])):
        first = group[0]
        title = first["title"].split(" · ")[0]
        target = first.get("youtube_url") or first["official_context_url"]
        links = []
        versions = Counter(item.get("language", "und") for item in group)
        for item in sorted(group, key=lambda item: (item.get("language", "und"), item["id"])):
            language = item.get("language", "und")
            label = language if versions[language] == 1 else language + " " + item["files"][0]["sha256"][:8]
            for record in item["files"]:
                links.append("[" + _label(label) + "](" + record["path"].removeprefix("published/") + ")")
        lines.append("| [" + _label(title) + "](" + target + ") | " + " · ".join(links) + " | [原文](" + first["official_context_url"] + ") |")
    lines += ["", "[文稿与研究](materials.md) · [公众号原文](wechat.md) · [打卡进度](../PROGRESS.md) · [资料说明](README.md)", ""]
    output = {"published/captions.md": "\n".join(lines)}

    lines = ["# 已公开文稿与研究", "", f"{len(other)}项文稿、影音材料说明与研究，按实际公开版本保留署名、时间依据和原文链接。", "",
             "公众号原文与备份文本另见[公众号目录](wechat.md)。影音原文件从原站观看或下载。", "",
             "| 材料 | 阅读或下载 | 原文 |", "| --- | --- | --- |"]
    for item in sorted(other, key=lambda item: (item["kind"], item["id"])):
        lines.append("| " + _label(item["title"]) + " | " + files(item) + " | [原文](" + item["source_url"] + ") |")
    lines += ["", "[字幕](captions.md) · [公众号原文](wechat.md) · [打卡进度](../PROGRESS.md)", ""]
    output["published/materials.md"] = "\n".join(lines)

    articles = [item for item in wechat if item.get("subtype") == "wechat_article"]
    captured = sum(item.get("content_status") in ("full_text", "captured_text", "edited_public_excerpt") for item in articles)
    lines = ["# 公众号原文与公开备份文本", "", f"{len(articles)}条公众号记录，其中{captured}条有保存文字；仅有目录信息的记录明确标为待补正文。保存文字不自动等于原始发表文章已经完整恢复。", "",
             "备份材料数量与文章篇数分别计算。保留原发布账号、原文署名、文章发布日期和原图链接；文章日期不自动当作演出或创作日期。", ""]
    for subtype, title in (("wechat_article", "公众号文章"), ("wechat_backup_material", "备份中的公开文本")):
        selected = [item for item in wechat if item.get("subtype") == subtype]
        lines += ["## " + title, "", "| 原文 | 状态 | 阅读 | 出处 |", "| --- | --- | --- | --- |"]
        for item in sorted(selected, key=lambda item: (str(item.get("publication_date", item.get("source_date", "")) or ""), item["id"])):
            status = {"full_text": "正文", "captured_text": "保存文字", "metadata_only": "待补正文", "edited_public_excerpt": "公开节选"}.get(item.get("content_status"), "依条目说明")
            lines.append("| " + _label(item["title"]) + " | " + status + " | " + files(item) + " | [原文](" + item["source_url"] + ") |")
        lines += [""]
    lines += ["[字幕](captions.md) · [文稿与研究](materials.md) · [打卡进度](../PROGRESS.md)", ""]
    output["published/wechat.md"] = "\n".join(lines)
    lines = ["# P4与何发：公开文字资料", "", "[字幕下载](captions.md) · [文稿与研究](materials.md) · [公众号原文](wechat.md) · [打卡进度](../PROGRESS.md)", "",
             f"当前收录{counts['caption']}份字幕、{counts['document']}项文稿／资料记录、{counts['research']}项研究。不同语言、字节版本、文章、素材和作品分别登记，数量不混作作品总数。", "",
             "文字保留原字节或有来源的公开访问版、准确署名与原文链接。何发个人作品保持个人归属。大型影音保留官方观看／下载入口，内部底稿、原片与私有运维信息不在公开包。", "",
             "[逐项来源与版本](manifest.json) · [版本批准记录](approvals.json) · [公开范围规则](../docs/公开范围投影.md)", "",
             "## 检查与检索", "", "```sh", "python -X utf8 scripts/official_publications.py check --root .", "python -X utf8 scripts/official_publications.py build-indexes --root .", "python -X utf8 scripts/official_publications.py search \"剧本\" --root .", "```", "",
             "检索带原文链接、字幕cue、版本SHA与相应时间轴。公开访问不授予统一开放许可；权利和核验范围沿用原资料。", ""]
    output["published/README.md"] = "\n".join(lines)
    return output


def build_indexes(root, check_only=False):
    root = Path(root).resolve()
    expected = _edition_indexes(root)
    changed = []
    for relative, text in expected.items():
        path = root / relative
        body = text.encode("utf-8")
        if not path.exists() or path.read_bytes() != body:
            changed.append(relative)
            if not check_only:
                path.write_bytes(body)
    return {"valid": not changed if check_only else True, "errors": ["Official edition indexes are stale"] if changed and check_only else [], "changed_files": changed}


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
    commands.add_parser("build-indexes").add_argument("--root", default=".")
    commands.add_parser("check-indexes").add_argument("--root", default=".")
    search_parser = commands.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--root", default=".")
    search_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        if args.command == "check":
            result = check(args.root)
        elif args.command in ("build-indexes", "check-indexes"):
            result = build_indexes(args.root, check_only=args.command == "check-indexes")
        else:
            result = search(args.root, args.query, args.limit)
    except (ValueError, OSError, UnicodeError) as exc:
        result = {"valid": False, "errors": [str(exc)]}
    except (TypeError, KeyError):
        result = {"valid": False, "errors": ["Invalid official publication data shape"]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
