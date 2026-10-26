#!/usr/bin/env python3
"""Append stable display accessions for public editions, separate from raw IDs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit

from catalog_transaction import StateLock
from official_publications import check as check_publications

NAMESPACE = "public-edition-accession:v1"
PREFIXES = {"document": "T", "caption": "S", "research": "R", "image": "I"}
LABELS = {"document": "文稿", "caption": "字幕", "research": "研究", "image": "图像"}
REGISTRY = "registry/public-accessions.json"
INDEX = "NUMBERED_INDEX.md"
PAGE_LIMIT = 500 * 1024  # Leave headroom below the 512KiB rendering target.
RANGE_SIZE = 100
PAGE_STEMS = {"document": "documents", "caption": "captions", "research": "research", "image": "images"}
GENERATED_PAGE = re.compile(r"(?:documents|captions|research|images)(?:-[0-9]{6}-[0-9]{6})?\.md\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(path):
    _require(path.is_file() and not path.is_symlink(), "Numbering input is missing or symlinked")
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite numbering data")))


def _relative(value, public=False):
    _require(isinstance(value, str) and value and not any(c in value for c in ("\\", ":", "\x00", "\n", "\r")), "Numbering metadata requires a safe relative path")
    parts = value.split("/")
    _require(all(p not in ("", ".", "..") for p in parts) and (not public or parts[0] == "published"), "Numbering metadata path is outside public editions")


def _https(value):
    _require(isinstance(value, str) and not any(c.isspace() for c in value), "Invalid public source URL")
    url = urlsplit(value)
    _require(url.scheme == "https" and url.hostname and not url.username and not url.password, "Numbering sources require HTTPS URLs without credentials")
    unused = url.port


def _public_metadata(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("archive_path", "archive_member_path"):
                _relative(item)
            if (key.endswith("url") or key.endswith("_url")) and isinstance(item, str):
                _https(item)
            _public_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _public_metadata(item)
    elif isinstance(value, str):
        _require(not value.startswith(("/Users/", "/home/", "file://", "\\\\")) and re.match(r"^[A-Za-z]:[\\/]", value) is None, "Local paths must not enter public numbering metadata")


def _file_key(file):
    _require(isinstance(file, dict) and isinstance(file.get("sha256"), str) and HEX.fullmatch(file["sha256"]) and type(file.get("bytes")) is int and file["bytes"] > 0, "Invalid numbered file SHA/bytes")
    return file["sha256"], file["bytes"]


def _location(file):
    location = {key: value for key, value in file.items() if key != "file_accession"}
    _file_key(location)
    _relative(location.get("path"), public=True)
    _require(location.get("storage", "git") in ("git", "release"), "Unknown public file storage")
    if location.get("storage") == "release":
        _https(location.get("download_url"))
        _relative(location.get("archive_path"))
    _public_metadata(location)
    return location


def _payload(item):
    files = item.get("files")
    _require(isinstance(files, list) and files, "Public edition needs numbered files")
    result = {"source_url": item.get("source_url"), "official_context_url": item.get("official_context_url"), "files": sorted((_location(f) for f in files), key=_encoded)}
    _https(result["source_url"]); _https(result["official_context_url"])
    _require(len({_encoded(f) for f in result["files"]}) == len(result["files"]), "Duplicate numbered storage location")
    return result


def _locations(row):
    return {(file["file_accession"], _sha(_encoded(location))): location for file in row["files"] for location in file["locations"]}


def _version_payload(row, version, locations=None):
    locations = _locations(row) if locations is None else locations
    references = version.get("files")
    _require(isinstance(references, list) and references, "Version needs file-location references")
    files = []
    for reference in references:
        _require(isinstance(reference, dict) and set(reference) == {"file_accession", "location_sha256"}, "Invalid version file-location reference")
        key = (reference["file_accession"], reference["location_sha256"])
        _require(key in locations, "Version references an unknown file accession/location")
        files.append(locations[key])
    return _payload({"source_url": version.get("source_url"), "official_context_url": version.get("official_context_url"), "files": files})


def _aliases(item):
    result = [{"namespace": "published:item-id", "value": item["id"]}]
    for field in ("accession", "entity_id", "image_id", "source_image_id", "official_image_id", "legacy_id", "legacy_accession"):
        if isinstance(item.get(field), str) and item[field]:
            result.append({"namespace": "published:" + field, "value": item[field]})
    for container in ("entity", "source_metadata"):
        if isinstance(item.get(container), dict):
            for field in ("id", "accession", "image_id"):
                if isinstance(item[container].get(field), str) and item[container][field]:
                    result.append({"namespace": container + ":" + field, "value": item[container][field]})
    declared = item.get("aliases", [])
    _require(isinstance(declared, list), "Declared public aliases must be a list")
    for alias in declared:
        if isinstance(alias, str):
            result.append({"namespace": "published:alias", "value": alias})
        elif isinstance(alias, dict):
            result.append({"namespace": alias.get("namespace"), "value": alias.get("value", alias.get("id"))})
        else:
            raise ValueError("Invalid existing public alias")
    unique = []
    for alias in result:
        _require(all(isinstance(alias.get(k), str) and alias[k] for k in ("namespace", "value")), "Invalid public alias identity")
        _public_metadata(alias)
        if alias not in unique:
            unique.append(alias)
    return unique


def _manifest(root):
    checked = check_publications(root)
    _require(checked.get("valid") is True, "Public editions must pass their publisher/source/version checks before numbering")
    path = root / "published/manifest.json"
    value = _json(path)
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1 and isinstance(value.get("items"), list), "Invalid numbered manifest")
    rows = {}
    for item in value["items"]:
        _require(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] and item["id"] not in rows, "Duplicate or missing public source identity")
        _require(item.get("kind") in PREFIXES and isinstance(item.get("title"), str) and item["title"].strip(), "Invalid public edition kind/title")
        _payload(item); _aliases(item)
        rows[item["id"]] = item
    return rows, _sha(path.read_bytes())


def _git(root, *args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        return None


def _baselines(root, explicit=None):
    baselines, mode = [], "initial_snapshot_only"
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    revisions, tracked, shallow = [], False, False
    if inside and inside.returncode == 0:
        history = _git(root, "log", "-2", "--format=%H", "--", REGISTRY)
        revisions = history.stdout.splitlines() if history and history.returncode == 0 else []
        tracked_result = _git(root, "ls-files", "--error-unmatch", REGISTRY)
        tracked = bool(tracked_result and tracked_result.returncode == 0)
        shallow_result = _git(root, "rev-parse", "--is-shallow-repository")
        shallow = bool(shallow_result and shallow_result.returncode == 0 and shallow_result.stdout.strip() == "true")
        _require(not shallow or not (tracked or revisions) or explicit is not None, "Numbering history is shallow; fetch complete history or provide a trusted --baseline")
        if not shallow:
            for revision in revisions:
                result = _git(root, "show", revision + ":" + REGISTRY)
                _require(result is not None and result.returncode == 0, "Cannot read committed numbering baseline")
                baselines.append(json.loads(result.stdout))
            if baselines:
                mode = "git_committed_history"
    if explicit is not None:
        path = Path(explicit).resolve()
        _require(path != (root / REGISTRY).resolve(), "Baseline must be a separate trusted registry snapshot")
        baselines.append(_json(path))
        mode = "explicit_and_git" if baselines[:-1] else "explicit_snapshot"
    return baselines, mode, bool(tracked or revisions)


def _validate_state(state):
    _require(isinstance(state, dict) and state.get("namespace") == NAMESPACE and type(state.get("schema_version")) is int and state["schema_version"] == 1, "Wrong public numbering namespace/schema")
    _require(isinstance(state.get("initial_manifest_sha256"), str) and HEX.fullmatch(state["initial_manifest_sha256"]) and isinstance(state.get("source_manifest_sha256"), str) and HEX.fullmatch(state["source_manifest_sha256"]), "Invalid numbering manifest fingerprints")
    counters = state.get("counters")
    _require(isinstance(counters, dict) and set(counters) == set(PREFIXES) and all(type(v) is int and 0 <= v <= 999999 for v in counters.values()), "Invalid public accession counters")
    _require(isinstance(state.get("items"), list), "Public accession items must be a list")
    rows, accessions, numbers = {}, set(), {k: set() for k in PREFIXES}
    for row in state["items"]:
        _require(isinstance(row, dict) and isinstance(row.get("source_id"), str) and row["source_id"] and row["source_id"] not in rows, "Duplicate or missing numbered source identity")
        kind, accession = row.get("kind"), row.get("accession")
        _require(kind in PREFIXES and isinstance(accession, str) and re.fullmatch(r"AR-" + PREFIXES[kind] + r"[0-9]{6}", accession) and accession not in accessions, "Duplicate accession or wrong kind/number segment")
        number = int(accession[-6:]); _require(number > 0, "Public accessions start at one")
        numbers[kind].add(number); accessions.add(accession)
        _require(isinstance(row.get("title"), str) and row["title"].strip() and type(row.get("in_current_manifest")) is bool, "Invalid numbered edition metadata")
        aliases = row.get("aliases")
        _require(isinstance(aliases, list) and all(isinstance(a, dict) and set(a) == {"namespace", "value"} and all(isinstance(a[k], str) and a[k] for k in ("namespace", "value")) for a in aliases), "Invalid numbered aliases")
        _require(len({_encoded(a) for a in aliases}) == len(aliases) and {"namespace": "published:item-id", "value": row["source_id"]} in aliases, "Missing original identity alias or duplicate alias")
        files, file_counter = row.get("files"), row.get("file_counter")
        _require(isinstance(files, list) and type(file_counter) is int and 0 < file_counter <= 99999, "Invalid per-edition file counter")
        by_content, by_accession, file_numbers = {}, {}, set()
        for file in files:
            key, child = _file_key(file), file.get("file_accession")
            _require(isinstance(child, str) and re.fullmatch(re.escape(accession) + r"-F[0-9]{5}", child) and child not in by_accession and key not in by_content, "Duplicate/invalid file accession or repeated byte identity")
            num = int(child[-5:]); _require(num > 0, "File accessions start at one")
            file_numbers.add(num)
            locations = file.get("locations")
            _require(isinstance(locations, list) and locations and len({_encoded(x) for x in locations}) == len(locations), "Missing/duplicate numbered file locations")
            for location in locations:
                _require(_file_key(_location(location)) == key, "Numbered file location changed its bytes")
            by_content[key] = file; by_accession[child] = file
        _require(file_numbers == set(range(1, file_counter + 1)), "File counter gap or removed file mapping")
        versions = row.get("versions")
        _require(isinstance(versions, list) and versions, "Numbered edition needs preserved version history")
        seen_versions = set()
        locations = _locations(row)
        for version in versions:
            _require(isinstance(version, dict), "Invalid numbered edition version")
            payload = _version_payload(row, version, locations)
            digest = _sha(_encoded(payload))
            _require(version.get("version_sha256") == digest and digest not in seen_versions, "Invalid/duplicate numbered version fingerprint")
            seen_versions.add(digest)
        _require(row.get("current_version_sha256") in seen_versions, "Current numbered version is missing")
        _public_metadata(row)
        rows[row["source_id"]] = row
    _require(all(numbers[k] == set(range(1, counters[k] + 1)) for k in PREFIXES), "Public counter gap or removed accession mapping")
    return rows


def _compare_baseline(state, baseline):
    old = _validate_state(baseline)
    current = {r["source_id"]: r for r in state["items"]}
    _require(state["initial_manifest_sha256"] == baseline["initial_manifest_sha256"], "Numbering initialization identity cannot change")
    _require(all(state["counters"][k] >= baseline["counters"][k] for k in PREFIXES), "Public counters cannot move backwards")
    for identity, previous in old.items():
        row = current.get(identity)
        _require(row is not None and row["accession"] == previous["accession"] and row["kind"] == previous["kind"], "Existing numbered mappings cannot be deleted, renumbered or retyped")
        _require(row["versions"][:len(previous["versions"])] == previous["versions"] and row["aliases"][:len(previous["aliases"])] == previous["aliases"], "Existing versions/aliases are append-only")
        _require(row["file_counter"] >= previous["file_counter"], "File counters cannot move backwards")
        children = {f["file_accession"]: f for f in row["files"]}
        for file in previous["files"]:
            child = children.get(file["file_accession"])
            _require(child is not None and _file_key(child) == _file_key(file) and child["locations"][:len(file["locations"])] == file["locations"], "Existing file numbers/locations are append-only")


def _guard_initial_or_history(state, baselines, manifest, fingerprint):
    for baseline in baselines:
        _compare_baseline(state, baseline)
    if not baselines:
        _require(state["initial_manifest_sha256"] == fingerprint and len(state["items"]) == len(manifest), "Existing numbering needs its trusted prior registry; restore history or provide --baseline")
        counters = {k: 0 for k in PREFIXES}
        rows = {r["source_id"]: r for r in state["items"]}
        for item in manifest.values():
            counters[item["kind"]] += 1
            row = rows.get(item["id"])
            _require(row is not None and row["accession"] == "AR-" + PREFIXES[item["kind"]] + f"{counters[item['kind']]:06d}" and len(row["versions"]) == 1, "Initial numbering differs from its explicit first snapshot")


def _bind_current(state, manifest, fingerprint, baselines):
    rows = _validate_state(state)
    _require(state["source_manifest_sha256"] == fingerprint and set(manifest).issubset(rows), "Current public source IDs or manifest fingerprint lack numbering")
    known = set().union(*(set(r["source_id"] for r in b["items"]) for b in baselines)) if baselines else set()
    for identity, row in rows.items():
        item = manifest.get(identity)
        _require(row["in_current_manifest"] is (item is not None), "Current/historical numbering flag differs from the manifest")
        if item is None:
            _require(identity in known, "Historical mapping lacks a trusted earlier registry")
            continue
        payload = _payload(item)
        _require(row["kind"] == item["kind"] and row["title"] == item["title"] and row["current_version_sha256"] == _sha(_encoded(payload)), "Numbered current kind/title/version differs from its source")
        _require(all(a in row["aliases"] for a in _aliases(item)), "Current original identity aliases are missing")
    return rows


def _append_version(row, item):
    payload = _payload(item)
    by_content = {_file_key(f): f for f in row["files"]}
    references = []
    for location in (_location(file) for file in item["files"]):
        key = _file_key(location)
        file = by_content.get(key)
        if file is None:
            row["file_counter"] += 1
            _require(row["file_counter"] <= 99999, "Per-edition file numbering segment is exhausted")
            file = {"file_accession": row["accession"] + f"-F{row['file_counter']:05d}", "sha256": key[0], "bytes": key[1], "locations": []}
            row["files"].append(file); by_content[key] = file
        if location not in file["locations"]:
            file["locations"].append(copy.deepcopy(location))
        references.append({"file_accession": file["file_accession"], "location_sha256": _sha(_encoded(location))})
    digest = _sha(_encoded(payload))
    if not any(v["version_sha256"] == digest for v in row["versions"]):
        row["versions"].append({**payload, "files": references, "version_sha256": digest})
    row["current_version_sha256"] = digest
    row["title"] = item["title"]
    for alias in _aliases(item):
        if alias not in row["aliases"]:
            row["aliases"].append(alias)


def _md(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")


def _numbered_rows(rows):
    lines = ["| 编号 | 成品与原文 | 原始身份 | 文件子号/版本 |", "|---|---|---|---|"]
    for row in rows:
        if not row["in_current_manifest"]:
            lines.append("| `" + row["accession"] + "` | 历史编号保留，当前清单未包含 | " + _md(row["source_id"]) + " | 不复用 |")
            continue
        version = next(v for v in row["versions"] if v["version_sha256"] == row["current_version_sha256"])
        files, locations = [], _locations(row)
        for reference in version["files"][:3]:
            file = locations[(reference["file_accession"], reference["location_sha256"])]
            if file.get("storage", "git") == "release":
                url = file["download_url"]
                _https(url)
            else:
                url = "../" + file["path"]
            files.append("[" + reference["file_accession"] + "](" + quote(url, safe=":/?#&=%._-~") + ")")
        if len(version["files"]) > 3:
            files.append("另" + str(len(version["files"]) - 3) + "个位置见登记")
        detail = " / ".join(files) + f"；累计{row['file_counter']}个文件号/{len(row['versions'])}版"
        lines.append("| `" + row["accession"] + "` | [" + _md(row["title"]) + "](" + quote(version["source_url"], safe=":/?#&=%._-~") + ") | " + _md(row["source_id"]) + " | " + detail + " |")
    return lines


def _category_page(kind, rows, segment=None):
    title = LABELS[kind] + "公开成品编号" + (" · " + segment if segment else "")
    lines = ["# " + title, "", "[编号入口](../NUMBERED_INDEX.md) · [完整文件号、位置与历史](../registry/public-accessions.json)", "",
             f"本页{len(rows)}条登记，当前{sum(r['in_current_manifest'] for r in rows)}条。主号与文件号不因分类或分页改变。", ""]
    lines += _numbered_rows(rows)
    lines += ["", "Release链接指实际归档包；包内路径、全部成员F号与SHA见登记，不将虚拟成员路径当作本地文件。", ""]
    return "\n".join(lines).encode("utf-8")


def _indexes(state):
    active = [r for r in state["items"] if r["in_current_manifest"]]
    lines = ["# 公开成品编号索引", "", f"当前{len(active)}条公开成品记录；编号登记累计{len(state['items'])}条。图像、文件字节版本与存储位置分别计算，tiles不算独立作品。", "",
             "编号空间：`" + NAMESPACE + "`。AR编号与P4原片/字幕编号独立，保留原ID、原编号与文件名。", "",
             "| 分类 | 当前记录 | 累计登记 | 浏览 |", "|---|---:|---:|---|"]
    output = {}
    for kind in PREFIXES:
        stem = PAGE_STEMS[kind]
        rows = sorted((r for r in state["items"] if r["kind"] == kind), key=lambda r: r["accession"])
        current = sum(r["in_current_manifest"] for r in rows)
        lines.append("| " + LABELS[kind] + " | " + str(current) + " | " + str(len(rows)) + " | [打开分类](numbered/" + stem + ".md) |")
        page = _category_page(kind, rows)
        if len(page) < PAGE_LIMIT:
            output["numbered/" + stem + ".md"] = page
            continue
        groups = {}
        for row in rows:
            start = ((int(row["accession"][-6:]) - 1) // RANGE_SIZE) * RANGE_SIZE + 1
            groups.setdefault(start, []).append(row)
        category = ["# " + LABELS[kind] + "公开成品编号", "", "[编号入口](../NUMBERED_INDEX.md) · [完整文件登记](../registry/public-accessions.json)", "",
                    f"当前{current}条，累计{len(rows)}条。按固定号段浏览，新增记录不重新分配旧号。", "", "| 号段 | 本段登记 |", "|---|---:|"]
        for start, selected in sorted(groups.items()):
            end = start + RANGE_SIZE - 1
            segment = f"{start:06d}–{end:06d}"
            name = f"{stem}-{start:06d}-{end:06d}.md"
            child = _category_page(kind, selected, segment)
            _require(len(child) < PAGE_LIMIT, "Numbered category segment exceeds 512KiB; use narrower stable ranges")
            output["numbered/" + name] = child
            category.append("| [" + segment + "](" + name + ") | " + str(len(selected)) + " |")
        output["numbered/" + stem + ".md"] = ("\n".join(category) + "\n").encode("utf-8")
    lines += ["", "[全部文件子号、存储位置与版本](registry/public-accessions.json) · [公开资料](published/README.md) · [打卡进度](PROGRESS.md)", "",
              "分类页较大时按稳定号段分页，每页保持小于512KiB。Release采用实际下载URL；编号不改变原件，也不表示作品总数。", ""]
    output[INDEX] = "\n".join(lines).encode("utf-8")
    _require(all(len(body) < PAGE_LIMIT for body in output.values()), "Numbered index page exceeds 512KiB")
    return output


def _index(state):
    return _indexes(state)[INDEX]


def _generated_pages(root):
    result = {INDEX} if (root / INDEX).exists() or (root / INDEX).is_symlink() else set()
    directory = root / "numbered"
    _require(not directory.is_symlink(), "Numbered index directory cannot be symlinked")
    if directory.is_dir():
        for path in directory.iterdir():
            if GENERATED_PAGE.fullmatch(path.name):
                result.add("numbered/" + path.name)
    return result


def _atomic(path, body):
    _require(not path.is_symlink() and (not path.exists() or path.is_file()), "Numbering output must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".accessions-", delete=False) as handle:
            temporary = Path(handle.name); handle.write(body)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _result(state, mode):
    return {"valid": True, "errors": [], "namespace": NAMESPACE, "active_items": sum(r["in_current_manifest"] for r in state["items"]), "registered_items": len(state["items"]), "file_accessions": sum(r["file_counter"] for r in state["items"]), "append_only_baseline": mode}


def assign(root, initialize=False, baseline=None):
    root = Path(root).resolve()
    _require(not (root / "registry").is_symlink(), "Registry directory cannot be symlinked")
    with StateLock(root.parent / ("." + root.name + "-public-accessions.lock"), root, allow_pending=True):
        manifest, fingerprint = _manifest(root)
        baselines, mode, prior = _baselines(root, baseline)
        path = root / REGISTRY
        if path.exists():
            _require(not initialize, "Existing numbering cannot be initialized again")
            state = _json(path); _validate_state(state)
            _guard_initial_or_history(state, baselines, manifest, fingerprint)
        else:
            _require(initialize and not prior and not baselines and not (root / INDEX).exists(), "Numbering state is missing: restore it; use --initialize only for this namespace's first allocation")
            state = {"schema_version": 1, "namespace": NAMESPACE, "initial_manifest_sha256": fingerprint, "source_manifest_sha256": fingerprint, "counters": {k: 0 for k in PREFIXES}, "items": []}
        rows = {r["source_id"]: r for r in state["items"]}
        for row in rows.values():
            row["in_current_manifest"] = False
        for identity, item in manifest.items():
            row = rows.get(identity)
            if row is None:
                kind = item["kind"]; state["counters"][kind] += 1
                _require(state["counters"][kind] <= 999999, "Public numbering segment is exhausted")
                row = {"source_id": identity, "kind": kind, "accession": "AR-" + PREFIXES[kind] + f"{state['counters'][kind]:06d}", "title": item["title"], "aliases": [], "file_counter": 0, "files": [], "versions": [], "in_current_manifest": True}
                state["items"].append(row); rows[identity] = row
            _require(row["kind"] == item["kind"], "Existing source identity cannot change its kind/number segment")
            row["in_current_manifest"] = True
            _append_version(row, item)
        state["source_manifest_sha256"] = fingerprint
        _validate_state(state)
        for previous in baselines:
            _compare_baseline(state, previous)
        _bind_current(state, manifest, fingerprint, baselines)
        indexes = _indexes(state)
        previous_pages = _generated_pages(root)
        _require(all(not (root / name).is_symlink() for name in set(indexes) | previous_pages), "Numbered pages cannot be symlinked")
        _atomic(path, (json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
        for name, body in indexes.items():
            _atomic(root / name, body)
        for name in previous_pages - set(indexes):
            (root / name).unlink()
        return {**_result(state, mode), "index_pages": len(indexes)}


def check(root, baseline=None):
    try:
        root = Path(root).resolve()
        _require(not (root / "registry").is_symlink(), "Registry directory cannot be symlinked")
        state = _json(root / REGISTRY)
        _validate_state(state)
        manifest, fingerprint = _manifest(root)
        baselines, mode, unused = _baselines(root, baseline)
        _guard_initial_or_history(state, baselines, manifest, fingerprint)
        _bind_current(state, manifest, fingerprint, baselines)
        indexes = _indexes(state)
        _require(_generated_pages(root) == set(indexes), "Missing or stale generated numbered page")
        _require(all((root / name).is_file() and not (root / name).is_symlink() and (root / name).read_bytes() == body for name, body in indexes.items()), "Numbered index pages differ from their registry")
        return {**_result(state, mode), "index_pages": len(indexes)}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {"valid": False, "errors": [str(exc)], "namespace": NAMESPACE}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("assign", "check"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--baseline")
    args = parser.parse_args()
    try:
        _require(args.command == "assign" or not args.initialize, "--initialize only applies to assign")
        result = assign(args.root, args.initialize, args.baseline) if args.command == "assign" else check(args.root, args.baseline)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {"valid": False, "errors": [str(exc)], "namespace": NAMESPACE}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
