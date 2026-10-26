#!/usr/bin/env python3
"""Deterministic reading pages and on-demand search of approved public SRTs."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "https://github.com/hehefa/-p4-theater-archive/blob/main/"
TABLES = ("projects", "collections", "events", "assets", "files", "subtitles", "publications")
ROLES = {"original_asr": "原语", "machine_translation": "English", "bilingual_machine_translation": "中英"}
EVENT_TYPES = {"rehearsal": "排练", "performance": "演出", "discussion": "讨论", "unclassified": "资料组"}
ALLOWED_HOSTS = {"a.p4theater.cn", "b.p4theater.cn", "p4theater.cn", "p.p4theater.cn", "hefa.p4theater.cn"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
ACCESSION = re.compile(r"P4-[PCEVFSUH]\d{6}\Z")
TIME = re.compile(r"(\d{2,3}):([0-5]\d):([0-5]\d)[,.](\d{3})")


def _read(path):
    return json.loads(Path(path).read_bytes().decode("utf-8-sig"))


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _md(value):
    return html.escape(" ".join(str(value).split()), quote=False).replace("|", "\\|")


def _url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username
            or parsed.password or parsed.port is not None or any(c.isspace() for c in value)):
        raise ValueError("Unsupported public source URL")
    return value.replace("(", "%28").replace(")", "%29")


def _link(label, url):
    return "[" + _md(label) + "](" + _url(url) + ")"


def _scope_policy(root, tables, source_hashes):
    """Freeze approved identities and caption versions before reading any text."""
    path = root / ".p4-public-scope.json"
    exports = {s["id"] for s in tables["subtitles"] if s.get("public_export")}
    if not path.exists():
        if exports:
            raise ValueError("Public captions require the explicit public scope policy")
        return None
    if path.is_symlink():
        raise ValueError("Public scope policy must be a regular file")
    policy = _read(path)
    allowed = policy.get("allowed_caption_subtitle_ids", [])
    versions = policy.get("allowed_caption_versions", {})
    if (policy.get("projection_required") is not True or not isinstance(allowed, list)
            or len(allowed) != len(set(allowed)) or not isinstance(versions, dict)
            or set(versions) != set(allowed)
            or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in versions.values())):
        raise ValueError("Public scope policy must freeze the allowed caption versions")
    if exports != set(allowed):
        raise ValueError("Public exports differ from the explicit scope allow list")
    forbidden = set(policy.get("excluded_identity_sha256s", []))
    for rows in tables.values():
        for row in rows:
            if _sha(row["id"].encode("utf-8")) in forbidden:
                raise ValueError("Excluded private identity cannot return to public reading")
    source_hashes[".p4-public-scope.json"] = _sha(path.read_bytes())
    return policy


def _official_source(root, asset, source_file, sub, source_hashes):
    proof = asset.get("official_media_evidence", {})
    digest = source_file.get("sha256", "")
    identity = {"asset_id": asset["id"], "asset_accession": asset["accession"],
                "file_id": source_file["id"], "file_accession": source_file["accession"],
                "source_sha256": digest}
    if (not isinstance(digest, str) or not HEX.fullmatch(digest)
            or source_file["id"] != asset["file_id"]
            or any(proof.get(k) != v for k, v in identity.items())
            or proof.get("decision") != "confirmed_project_media"
            or proof.get("scope") != "project_media_provenance_only"
            or asset.get("official_media_affiliation_status") != "confirmed_official_source_and_project_context"
            or asset.get("public_caption_export_allowed") is not True
            or sub.get("official_media_affiliation_status") != "confirmed_official_source_and_project_context"
            or sub["public_export"].get("official_evidence_source_sha256") != digest):
        raise ValueError("Caption lacks its exact official source evidence: " + sub["accession"])
    relative = "evidence/" + asset["accession"] + "-official-media.json"
    path = root / relative
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("Official source evidence file is missing: " + asset["accession"])
    saved = _read(path)
    for key in (*identity, "decision", "scope", "evidence"):
        if saved.get(key) != proof.get(key):
            raise ValueError("Official source evidence disagrees with the catalogue: " + asset["accession"])
    urls = []
    for row in saved.get("evidence", []):
        if row.get("http_status") == 200 and isinstance(row.get("html_utf8_sha256"), str) and HEX.fullmatch(row["html_utf8_sha256"]):
            urls.append(_url(row["url"]))
    if not {"a.p4theater.cn", "b.p4theater.cn"}.issubset({urlsplit(u).hostname for u in urls}):
        raise ValueError("Official source requires archive and project-context evidence")
    source_hashes[relative] = _sha(path.read_bytes())
    return {"source_sha256": digest, "official_evidence_path": relative,
            "official_source_urls": sorted(set(urls))}


def _caption_path(root, sub):
    accession = sub["accession"]
    export = sub["public_export"]
    digest = sub.get("version_sha256", "")
    if (not ACCESSION.fullmatch(accession) or not HEX.fullmatch(digest)
            or export.get("sha256") != digest
            or export.get("scope_review_status") != "approved_official_source_and_text_scope"
            or export.get("quality_label") != "machine_draft"
            or sub.get("public_text_status") != "public_machine_draft"
            or sub.get("clock") != "source_zero"):
        raise ValueError("Invalid approved caption metadata: " + accession)
    raw = export.get("path", "")
    parts = PurePosixPath(raw).parts
    if ("\\" in raw or ":" in raw or len(parts) != 4
            or parts[:3] != ("captions", accession, digest)
            or parts[3] in {".", ".."} or not parts[3].lower().endswith(".srt")):
        raise ValueError("Invalid approved caption path: " + accession)
    path = root.joinpath(*parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("Caption path leaves archive: " + accession) from error
    if any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)):
        raise ValueError("Caption symlink is not accepted: " + accession)
    if not path.is_file():
        raise ValueError("Approved caption is missing: " + accession)
    return path, raw


def _data(root, verify_bytes=True):
    root = Path(root)
    tables = {}
    source_hashes = {}
    for name in TABLES:
        path = root / "catalog" / (name + ".json")
        if not path.exists() and name in {"collections", "publications"}:
            tables[name] = []
            continue
        source_hashes[path.relative_to(root).as_posix()] = _sha(path.read_bytes())
        tables[name] = _read(path)["items"]
    maps = {name: {row["id"]: row for row in rows} for name, rows in tables.items()}
    for rows in tables.values():
        if len({r["id"] for r in rows}) != len(rows) or len({r["accession"] for r in rows}) != len(rows):
            raise ValueError("Duplicate public identity or accession")
        for row in rows:
            if not ACCESSION.fullmatch(row["accession"]):
                raise ValueError("Invalid public accession")
    for asset in tables["assets"]:
        source_file = maps["files"].get(asset["file_id"])
        if not source_file or source_file.get("asset_id") != asset["id"]:
            raise ValueError("Asset file binding is inconsistent: " + asset["accession"])
    policy = _scope_policy(root, tables, source_hashes)
    notes_path = root / "content/reading-notes.json"
    notes = _read(notes_path) if notes_path.exists() else {"schema_version": 1, "projects": {}}
    if notes_path.exists():
        source_hashes["content/reading-notes.json"] = _sha(notes_path.read_bytes())
    official_path = root / "registry/official-projects.json"
    official = _read(official_path) if official_path.exists() else {"items": [], "snapshot_at": None}
    if official_path.exists():
        source_hashes["registry/official-projects.json"] = _sha(official_path.read_bytes())
    captions, sources, paths = [], {}, {}
    for sub in sorted(tables["subtitles"], key=lambda s: s["accession"]):
        # An equivalent_public_export reference is not an independent export.
        if not sub.get("public_export"):
            continue
        if policy["allowed_caption_versions"].get(sub["id"]) != sub.get("version_sha256"):
            raise ValueError("Caption version differs from the explicit scope approval: " + sub["accession"])
        path, relative = _caption_path(root, sub)
        if relative in paths:
            raise ValueError("Duplicate public caption path")
        if verify_bytes and _sha(path.read_bytes()) != sub["version_sha256"]:
            raise ValueError("Public caption hash mismatch: " + sub["accession"])
        source_asset_id = sub.get("source_asset_id")
        asset = maps["assets"].get(source_asset_id)
        if not asset or asset["id"] not in sub.get("asset_ids", []):
            raise ValueError("Caption source asset is not bound: " + sub["accession"])
        source_file_id = sub.get("source_file_id")
        source_file = maps["files"].get(source_file_id)
        if not source_file or source_file.get("asset_id") != asset["id"]:
            raise ValueError("Caption source file is not bound: " + sub["accession"])
        official_source = _official_source(root, asset, source_file, sub, source_hashes)
        project_rows = [maps["projects"][p] for p in asset["project_ids"] if p in maps["projects"]]
        if len(project_rows) != len(asset["project_ids"]):
            raise ValueError("Caption source project is missing")
        filename = source_file.get("camera_filename", source_file["accession"])
        if "/" in filename or "\\" in filename or ":" in filename or any(c in filename for c in "\r\n"):
            raise ValueError("Source filename must not contain a storage path")
        source = {"asset_id": asset["id"], "asset_accession": asset["accession"],
                  "file_id": source_file["id"], "file_accession": source_file["accession"],
                  "camera_filename": filename,
                  "projects": [{"id": p["id"], "accession": p["accession"], "title": p["title"]}
                               for p in sorted(project_rows, key=lambda p: p["accession"])],
                  "event_ids": sorted(asset.get("event_ids", []))}
        source.update(official_source)
        if source_file_id in sources and sources[source_file_id] != source:
            raise ValueError("Conflicting caption source binding")
        sources[source_file_id] = source
        captions.append({"id": sub["id"], "accession": sub["accession"],
                         "source_file_id": source_file_id, "language": sub["language"],
                         "role": sub["role"], "clock": "source_zero",
                         "sha256": sub["version_sha256"], "path": relative,
                         "quality_label": "machine_draft",
                         "human_listening_review": sub.get("human_listening_review", "not_recorded")})
        paths[relative] = sub
    actual_paths = {p.relative_to(root).as_posix() for p in (root / "captions").rglob("*")
                    if p.suffix.lower() == ".srt" and (p.is_file() or p.is_symlink())}
    if actual_paths != set(paths):
        raise ValueError("Caption tree contains text outside the explicit public scope")
    manifest = {"schema_version": 1, "source_sha256": dict(sorted(source_hashes.items())),
                "public_caption_count": len(captions), "source_file_count": len(sources),
                "sources": sorted(sources.values(), key=lambda s: s["file_accession"]), "captions": captions}
    return {"root": root, "tables": tables, "maps": maps, "notes": notes,
            "official": official, "manifest": manifest, "policy": policy}


def _parse_srt(body, accession):
    lines = body.decode("utf-8-sig").splitlines()
    cues, i = [], 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        first = i
        if not lines[i].strip().isdigit() or i + 1 >= len(lines):
            raise ValueError("Malformed public SRT cue: " + accession)
        cue = int(lines[i].strip())
        stamps = re.fullmatch(r"\s*(\d{2,3}:[0-5]\d:[0-5]\d[,.]\d{3})\s*-->\s*(\d{2,3}:[0-5]\d:[0-5]\d[,.]\d{3})(?:\s+.*)?", lines[i + 1])
        if not stamps:
            raise ValueError("Malformed public SRT timestamp: " + accession)
        def milliseconds(stamp):
            h, m, s, ms = map(int, TIME.fullmatch(stamp).groups())
            return ((h * 60 + m) * 60 + s) * 1000 + ms
        start, end = map(milliseconds, stamps.groups())
        if end < start:
            raise ValueError("Reversed public SRT timestamp: " + accession)
        i += 2
        text = []
        while i < len(lines) and lines[i].strip():
            text.append(lines[i]); i += 1
        cues.append({"cue": cue, "start_ms": start, "end_ms": end,
                     "text": "\n".join(text), "line": first + 1})
    return cues


def _caption_bytes(root, caption):
    body = (Path(root) / caption["path"]).read_bytes()
    if _sha(body) != caption["sha256"]:
        raise ValueError("Public caption hash mismatch: " + caption["accession"])
    return body


def _time(ms):
    seconds, milli = divmod(ms, 1000); minutes, second = divmod(seconds, 60); hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d},{milli:03d}"


def _date(event):
    date = event.get("date") or {}
    value = date.get("value")
    if not value:
        return "日期待补"
    basis = "〔目录日期线索〕" if date.get("basis") == "source_folder_label" else "〔" + _md(date.get("basis", "来源待补")) + "〕"
    return _md(value) + basis


def _official_rows(data, project):
    return [row for row in data["official"]["items"]
            if any(x.get("id") == project["id"] for x in row.get("current_video_catalogue_matches", []))]


def _title(data, project):
    matches = _official_rows(data, project)
    return matches[0]["title_zh"] if len(matches) == 1 else project["title"]


def _caption_links(captions, prefix=""):
    return " / ".join("[" + ROLES.get(c["role"], _md(c["language"])) + "](" + prefix + c["path"] + ")"
                      for c in sorted(captions, key=lambda c: (list(ROLES).index(c["role"]) if c["role"] in ROLES else 99, c["accession"])))


def _source_groups(data):
    grouped = defaultdict(list)
    for c in data["manifest"]["captions"]:
        grouped[c["source_file_id"]].append(c)
    return grouped


def _official_links(source):
    return " / ".join(_link("视频档案原文" if urlsplit(u).hostname == "a.p4theater.cn" else "研究依据原文", u)
                      for u in source["official_source_urls"])


def _publication_note(data, prefix="", project=None):
    asset_ids = {a["id"] for a in data["tables"]["assets"]
                 if project is None or project["id"] in a["project_ids"]}
    records = [r for r in data["tables"]["publications"] if r.get("asset_id") in asset_ids]
    if not records:
        return []
    return [f"另有[{len(records)}条既有 YouTube 元数据关联]({prefix}publications/README.md)。"
            "元数据、完整播放和在线字幕分别核验，关联记录不代替后两项检查。", ""]


def _asset_captions(data, asset, grouped):
    direct = grouped.get(asset["file_id"], [])
    if direct:
        return direct, ""
    source_id = asset.get("canonical_byte_source_asset_id")
    source = data["maps"]["assets"].get(source_id)
    if not source or asset.get("byte_identity_relation", {}).get("status") != "full_sha256_equal":
        return [], ""
    own_file = data["maps"]["files"].get(asset["file_id"], {})
    source_file = data["maps"]["files"].get(asset.get("canonical_byte_source_file_id"), {})
    digest = own_file.get("sha256")
    if not digest or source_file.get("sha256") != digest or source_file.get("id") != source.get("file_id"):
        return [], ""
    # Byte identity preserves a file relationship, not a second public export.
    return [], "已登记字节副本 → " + source["accession"] + " / " + source_file["accession"] + "；字幕仅在获批来源记录提供"


def _featured(data, project, note):
    lines = []
    for feature in note.get("featured", []):
        c = next((x for x in data["manifest"]["captions"] if x["accession"] == feature["subtitle_accession"]), None)
        sources = {s["file_id"]: s for s in data["manifest"]["sources"]}
        if not c or project["id"] not in [p["id"] for p in sources[c["source_file_id"]]["projects"]]:
            raise ValueError("Reading note does not reference this project's public caption")
        source = sources[c["source_file_id"]]
        cues = _parse_srt(_caption_bytes(data["root"], c), c["accession"])
        selected = [q for q in cues if feature["cue_start"] <= q["cue"] <= feature["cue_end"]]
        if not selected or selected[0]["cue"] != feature["cue_start"] or selected[-1]["cue"] != feature["cue_end"] or len(selected) > 12:
            raise ValueError("Reading note cue range is not available")
        related = [x for x in data["manifest"]["captions"] if x["source_file_id"] == source["file_id"]]
        lines += ["## " + _md(feature["title"]), "", _md(feature["summary"]), "",
                  f"**source_zero {_time(selected[0]['start_ms'])}–{_time(selected[-1]['end_ms'])} · cue {feature['cue_start']}–{feature['cue_end']} · 机器字幕依据**", "",
                  _caption_links(related, "../../"), "",
                  "来源：`" + source["asset_accession"] + "` / `" + source["file_accession"] + "` · " + _md(source["camera_filename"]), "",
                  _official_links(source), ""]
    return lines


def _project_page(data, project, grouped):
    tables, maps = data["tables"], data["maps"]
    assets = sorted((a for a in tables["assets"] if project["id"] in a["project_ids"]), key=lambda a: a["accession"])
    events = sorted((e for e in tables["events"] if project["id"] in e["project_ids"]), key=lambda e: e["accession"])
    sources = [s for s in data["manifest"]["sources"] if project["id"] in [p["id"] for p in s["projects"]]]
    notes = data["notes"].get("projects", {}).get(project["accession"], {})
    heading = _title(data, project)
    lead = notes.get("lead", f"这里从《{heading}》的材料记录进入，按{len(events)}个资料组查找来源、事件线索与文件版本。")
    lines = ["# " + _md(heading), "", _md(lead), ""]
    if heading != project["title"]:
        lines += ["本批资料名称：**" + _md(project["title"]) + "**。", ""]
    actions = []
    if project.get("related_public_project_page"):
        actions.append(_link("项目材料与研究（B站）", project["related_public_project_page"]))
    for row in _official_rows(data, project):
        actions.append(_link("官方项目原文 · " + row["official_project_code"], row["project_url_zh"]))
        if row.get("project_url_en"):
            actions.append(_link("English source", row["project_url_en"]))
    actions += ["[可读字幕](#可读字幕)", "[资料组与来源](#资料组与来源)", "[补充资料](../../CONTRIBUTING.md)"]
    lines += [" · ".join(actions), "", "项目编号：`" + project["accession"] + "`" +
              (" · 既有编号：" + " / ".join("`" + _md(x) + "`" for x in project["aliases"]) if project.get("aliases") else ""), ""]
    lines += _featured(data, project, notes)
    lines += _publication_note(data, "../../", project)
    lines += ["## 可读字幕", ""]
    if sources:
        lines += [f"{len(sources)}个来源文件已开放原语、英文或中英字幕；均保留机器稿状态。", "",
                  "| 来源记录 | 可打开的文字 | 官方来源 | 文件关系 |", "| --- | --- | --- | --- |"]
        for source in sources:
            asset = maps["assets"][source["asset_id"]]
            relation = ""
            if asset.get("alternate_representation_candidate_of_asset_id") in maps["assets"]:
                other = maps["assets"][asset["alternate_representation_candidate_of_asset_id"]]
                relation = "另一文件候选版本 → " + other["accession"] + "；各自保留文件与时间轴"
            lines.append("| [" + source["asset_accession"] + "](#" + source["asset_accession"].lower() + ") · " + _md(source["camera_filename"]) + " | " + _caption_links(grouped[source["file_id"]], "../../") + " | " + _official_links(source) + " | " + _md(relation or "独立来源记录") + " |")
        lines.append("")
    else:
        lines += ["本页当前提供项目、事件与文件索引；已开放的字幕可从[全库字幕入口](../../reading/captions.md)阅读。", ""]
    lines += ["## 资料组与来源", "", "日期依来源字段显示；资料组编号保持原有对应。", "",
              "| 日期线索 | 类型与资料组 | 文件记录 | 可读字幕来源 |", "| --- | --- | ---: | ---: |"]
    for event in events:
        members = [a for a in assets if event["id"] in a["event_ids"]]
        public_files = {a["file_id"] for a in members if grouped.get(a["file_id"])}
        event_type = event.get("event_type") or "unclassified"
        label = EVENT_TYPES.get(event_type, event_type)
        lines.append(f"| {_date(event)} | [{_md(label)} · {event['accession']}](#event-{event['accession'].lower()}) | {len(members)} | {len(public_files)} |")
    lines += ["", "<details>", "<summary>项目身份与全部文件记录</summary>", "",
              "Canonical ID：`" + project["id"] + "`", "", "既有档案对照：`" + project.get("legacy_reconciliation", "pending") + "`", ""]
    anchored = set()
    for event in events:
        lines += [f'<a id="event-{event["accession"].lower()}"></a>', "", "### " + _md(event["title"]) + " · " + event["accession"], "",
                  "日期：" + _date(event), "",
                  "| 视频 / 文件 | 格式 / MB | 可读字幕 | 解码记录 | 发布记录 |", "| --- | --- | --- | --- | --- |"]
        for asset in assets:
            if event["id"] not in asset["event_ids"]:
                continue
            f = maps["files"][asset["file_id"]]
            caption_rows, relationship = _asset_captions(data, asset, grouped)
            caption_text = _caption_links(caption_rows, "../../") or "暂无公开文本"
            if relationship:
                caption_text += "；" + _md(relationship)
            anchor = "" if asset["id"] in anchored else f'<a id="{asset["accession"].lower()}"></a>'
            anchored.add(asset["id"])
            lines.append(f"| {anchor}{asset['accession']} / {f['accession']} | {_md(f['extension'])} / {f['bytes'] / 1_000_000:.2f} | {caption_text} | `{f.get('full_decode_status', 'not_recorded')}` | `{asset.get('publication_status', 'not_recorded')}` |")
        lines.append("")
    lines += ["</details>", "", "[补充日期、人物或经过](../../templates/事件记录.md) · [修订字幕](../../templates/字幕修订.md) · [返回资料目录](../../INDEX.md)", ""]
    return "\n".join(lines)


def _artifacts(root):
    data = _data(root)
    manifest = data["manifest"]
    grouped = _source_groups(data)
    projects = sorted(data["tables"]["projects"], key=lambda p: p["accession"])
    artifacts = {}
    for p in projects:
        artifacts[f"projects/{p['accession']}/index.md"] = _project_page(data, p, grouped)
    lines = ["# P4项目与资料目录", "",
             "[认识项目](reading/projects.md) · [读公开字幕](reading/captions.md) · [查来源与文件](#本批资料目录) · [补充记录](CONTRIBUTING.md)", "",
             "从[阅读入口](reading/index.md)进入P4的作品、文字与资料关系。", "",
             "## 现在可以读什么", "", "| 项目 | 可读来源文件 | 入口 |", "| --- | ---: | --- |"]
    for p in projects:
        n = sum(p["id"] in [x["id"] for x in s["projects"]] for s in manifest["sources"])
        if n:
            lines.append(f"| [{_md(_title(data, p))}](projects/{p['accession']}/index.md) | {n} | [原语 / English / 中英](projects/{p['accession']}/index.md#可读字幕) |")
    if not manifest["captions"]:
        lines.append("| 本批暂无公开字幕 | 0 | [查项目与来源](#本批资料目录) |")
    lines += [""] + _publication_note(data)
    lines += ["", "## 本批资料目录", "",
              f"{len(projects)}个候选项目、{len(data['tables']['collections'])}个来源集合、{len(data['tables']['events'])}个暂定事件组、{len(data['tables']['files'])}个文件记录。", "",
              f"另见[官方{len(data['official']['items'])}项项目入口](reading/projects.md)；官方条目与本批资料按已登记关系互链。", "",
              "| 本批编号 | 项目 / 来源名称 | 文件 / 资料组 | 当前可读来源 | 项目说明 |", "| --- | --- | ---: | ---: | --- |"]
    for p in projects:
        n_assets = sum(p["id"] in a["project_ids"] for a in data["tables"]["assets"])
        n_events = sum(p["id"] in e["project_ids"] for e in data["tables"]["events"])
        n_public = sum(p["id"] in [x["id"] for x in s["projects"]] for s in manifest["sources"])
        links = [_link(row["official_project_code"], row["project_url_zh"]) for row in _official_rows(data, p)]
        if p.get("related_public_project_page"):
            links.append(_link("材料与研究", p["related_public_project_page"]))
        name = _md(_title(data, p))
        if _title(data, p) != p["title"]:
            name += " · 资料名：" + _md(p["title"])
        lines.append(f"| {p['accession']} | [{name}](projects/{p['accession']}/index.md) | {n_assets} / {n_events} | {n_public} | " + " / ".join(links or ["由本批材料继续补充"]) + " |")
    lines += ["", "[官方原始注册表](registry/README.md) · [编号与分类](docs/编号与分类.md) · [阅读与检索说明](docs/阅读与检索.md) · [机器可读目录](catalog/summary.json)", ""]
    artifacts["INDEX.md"] = "\n".join(lines)
    artifacts["reading/index.md"] = "\n".join(["# 从作品与文字进入P4", "",
        "P4始于北京，以表演、摄影、影像和写作展开现场实践。", "",
        "| 你想找什么 | 从这里进入 |", "| --- | --- |",
        "| 认识P4与已发表项目 | [P4简介](https://p4theater.cn/about/p4-theater/) · [官方项目入口](projects.md) |",
        "| 现在能读到的文字 | [公开字幕](captions.md) · [按项目查材料](../INDEX.md) |",
        "| 查已有视频的关联记录 | [既有 YouTube 元数据](../publications/README.md)；播放与在线字幕另行核验 |",
        "| 对照来源、事件与文件版本 | [本批资料目录](../INDEX.md) · [编号与分类](../docs/编号与分类.md) |",
        "| 补充日期、人物或字幕 | [增补指南](../CONTRIBUTING.md) · [事件记录](../templates/事件记录.md) |", "",
        "## 网站与档案各自做什么", "",
        "[P4官网](https://p4theater.cn/)看作品与当前信息；[经典版](https://p.p4theater.cn/)保留原有阅读方式；[A站](https://a.p4theater.cn/)读项目原文与公开材料；[B站](https://b.p4theater.cn/)读研究与材料关系。此仓保存来源编号、字幕与版本；[Toolkit](https://github.com/hehefa/p4-archive-toolkit)用于本地导入、检索与整理。", "",
        f"当前阅读清单仅含{manifest['public_caption_count']}份经白名单批准并核对字节哈希的SRT，关联{manifest['source_file_count']}个有官方逐项证据的来源文件；不另复制全文索引。", "",
        "[关键词检索用法](../docs/阅读与检索.md) · [返回档案README](../README.md)", ""])
    official_lines = ["# 从官方项目进入", "", "保留A站公开索引的项目名称、编号、原文与本批对应。", "",
                      "快照时间：" + _md(data["official"].get("snapshot_at") or "未提供") + "。", "",
                      "| 官方编号 | 项目原文 | 索引年代线索 | 本批资料入口 |", "| --- | --- | --- | --- |"]
    for row in sorted(data["official"]["items"], key=lambda row: row["official_project_code"]):
        local = []
        for relation in row.get("current_video_catalogue_matches", []):
            p = data["maps"]["projects"].get(relation["id"])
            if p:
                local.append(f"[{_md(p['title'])} · {p['accession']}](../projects/{p['accession']}/index.md)")
        for relation in row.get("candidate_current_video_catalogue_matches", []):
            p = data["maps"]["projects"].get(relation["id"])
            if p:
                local.append(f"[名称线索：{p['accession']}](../projects/{p['accession']}/index.md)")
        links = _link(row["title_zh"], row["project_url_zh"])
        if row.get("project_url_en"):
            links += " · " + _link("English", row["project_url_en"])
        official_lines.append("| " + row["official_project_code"] + " | " + links + " | " + _md(row.get("published_temporal_coverage") or "未列") + " | " + " / ".join(local or ["先读官方原文"]) + " |")
    official_lines += ["", "[本批16项资料目录](../INDEX.md) · [原始注册表与映射依据](../registry/README.md)", ""]
    # The label follows the actual inventory, including synthetic fixtures.
    official_lines[-2] = official_lines[-2].replace("16项", str(len(projects)) + "项")
    artifacts["reading/projects.md"] = "\n".join(official_lines)
    caption_lines = ["# 公开字幕阅读入口", "", f"{manifest['public_caption_count']}份SRT，{manifest['source_file_count']}个来源文件。每份字幕保留自己的ID、哈希、源文件与source_zero时间轴；机器稿状态随条目保留。", ""]
    for p in projects:
        sources = [s for s in manifest["sources"] if p["id"] in [x["id"] for x in s["projects"]]]
        if not sources:
            continue
        caption_lines += ["## " + _md(_title(data, p)), "", f"[项目材料与日期线索](../projects/{p['accession']}/index.md) · {len(sources)}个来源文件", "",
                          "| 来源文件 | 可读文本 | 来源编号 | 官方来源 |", "| --- | --- | --- | --- |"]
        for s in sources:
            caption_lines.append("| " + _md(s["camera_filename"]) + " | " + _caption_links(grouped[s["file_id"]], "../") + " | " + s["asset_accession"] + " / " + s["file_accession"] + " | " + _official_links(s) + " |")
        caption_lines.append("")
    caption_lines += ["[检索关键词并返回cue与版本](../docs/阅读与检索.md) · [完整公开导出登记](../captions/README.md) · [返回资料目录](../INDEX.md)", ""]
    artifacts["reading/captions.md"] = "\n".join(caption_lines)
    artifacts["reading/manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    return artifacts, {"projects": len(projects), "official_projects": len(data["official"]["items"]),
                       "public_captions": manifest["public_caption_count"], "source_files": manifest["source_file_count"]}


def build(root):
    """Validate every approved export, then write only deterministic owned files."""
    root = Path(root)
    artifacts, summary = _artifacts(root)
    changed = []
    for relative, text in sorted(artifacts.items()):
        target = root / relative
        body = text.replace("\r\n", "\n").encode("utf-8")
        if target.exists() and target.read_bytes() == body:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".reading-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        changed.append(relative)
    return {"ok": True, **summary, "generated_files": sorted(artifacts), "changed_files": changed}


def check(root):
    """Read-only generated-byte drift check; does not build or repair files."""
    root = Path(root)
    try:
        artifacts, summary = _artifacts(root)
    except (ValueError, KeyError, OSError, UnicodeError) as error:
        return {"ok": False, "errors": [str(error)]}
    errors = []
    for relative, text in sorted(artifacts.items()):
        expected = text.replace("\r\n", "\n").encode("utf-8")
        path = root / relative
        if not path.is_file():
            errors.append("Missing generated file: " + relative)
        elif path.read_bytes() != expected:
            errors.append("Generated file drift: " + relative)
    return {"ok": not errors, **summary, "checked_files": sorted(artifacts), "errors": errors}


def search(root, query, limit=20, language=None):
    """Search only approved, unchanged public SRTs; preserve each source binding."""
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("Search query must contain 1–200 characters")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Search limit must be between 1 and 100")
    data = _data(root, verify_bytes=False)
    expected = data["manifest"]
    path = Path(root) / "reading/manifest.json"
    if not path.exists() or _read(path) != expected:
        raise ValueError("Reading manifest is missing or stale; run build")
    def normalize(text):
        return " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    terms = normalize(query).split()
    sources = {s["file_id"]: s for s in expected["sources"]}
    matches, cache = [], {}
    available = {c["language"] for c in expected["captions"]} | {"original"}
    if language is not None and language not in available:
        raise ValueError("Unknown public caption language; use original, und, en or zh-en")
    for c in expected["captions"]:
        if language is not None and not (c["role"] == "original_asr" if language == "original" else c["language"] == language):
            continue
        body = _caption_bytes(root, c)
        if c["sha256"] not in cache:
            cache[c["sha256"]] = _parse_srt(body, c["accession"])
        source = sources[c["source_file_id"]]
        for cue in cache[c["sha256"]]:
            text = normalize(cue["text"])
            if not all(term in text for term in terms):
                continue
            score = sum(min(text.count(term), 10) for term in terms)
            matches.append({"projects": source["projects"],
                            "source_asset_id": source["asset_id"], "source_asset_accession": source["asset_accession"],
                            "source_file_id": source["file_id"], "source_file_accession": source["file_accession"],
                            "event_ids": source["event_ids"], "subtitle_id": c["id"],
                            "subtitle_accession": c["accession"], "subtitle_sha256": c["sha256"],
                            "language": c["language"], "role": c["role"], "cue": cue["cue"],
                            "clock": "source_zero", "start_ms": cue["start_ms"], "end_ms": cue["end_ms"],
                            "start_timecode": _time(cue["start_ms"]), "end_timecode": _time(cue["end_ms"]),
                            "text": cue["text"], "quality_label": c["quality_label"],
                            "human_listening_review": c["human_listening_review"],
                            "original_source_sha256": source["source_sha256"],
                            "official_source_urls": source["official_source_urls"],
                            "source_url": PUBLIC_REPO + quote(c["path"], safe="/") + "#L" + str(cue["line"]),
                            "source_path": c["path"], "_score": score})
    matches.sort(key=lambda r: (-r["_score"], r["source_asset_accession"], r["subtitle_accession"], r["cue"]))
    for result in matches:
        result.pop("_score")
    return matches[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "check", "search"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=ROOT)
        if name == "search":
            command.add_argument("query", nargs="?")
            command.add_argument("--query", dest="query_option")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--language")
    args = parser.parse_args()
    try:
        if args.command == "search":
            if args.query is not None and args.query_option is not None:
                raise ValueError("Provide either a positional query or --query")
            result = search(args.root, args.query_option if args.query_option is not None else args.query, args.limit, args.language)
        else:
            result = globals()[args.command](args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and not result.get("ok") else 0
    except (ValueError, KeyError, OSError, UnicodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
