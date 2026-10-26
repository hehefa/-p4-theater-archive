#!/usr/bin/env python3
"""Build/check batch progress from explicit evidence, without an archive-wide ratio."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlsplit

STAGES = {"prepared": "已整理", "github_published": "GitHub已发布", "platform_published": "平台已发布"}
METRICS = {"full_sha256_recorded": "完整SHA登记", "full_decode_passed_existing_reports": "既有完整解码记录", "official_media_affiliation_confirmed_assets": "官方逐项关联"}
KINDS = {"all", "caption", "document", "research", "image"}
MEASURES = {"items", "files", "video_versions"}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _read(path):
    def nonfinite(unused):
        raise ValueError("Non-finite progress numbers are not accepted")
    _require(path.is_file() and not path.is_symlink(), "Required progress input is missing or symlinked: " + path.name)
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=nonfinite)


def _text(value, field):
    _require(isinstance(value, str) and value.strip(), "Missing progress text: " + field)


def _number(value, field):
    _require(value is None or (type(value) is int and value >= 0), "Progress number must be a nonnegative integer or null: " + field)


def _stamp(value, date_allowed=False):
    _text(value, "timestamp")
    if date_allowed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        date.fromisoformat(value)
        return
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(stamp.tzinfo is not None and stamp.utcoffset() is not None, "Progress observation time requires a timezone")


def _urls(values, field, nonempty=True):
    _require(isinstance(values, list) and (bool(values) or not nonempty), "Evidence URL list must not be empty: " + field)
    for value in values:
        _require(isinstance(value, str) and not any(c.isspace() for c in value), "Invalid evidence URL: " + field)
        parsed = urlsplit(value)
        _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password, "Evidence requires a public HTTPS URL: " + field)
        unused = parsed.port  # Also reject malformed port syntax.
    _require(len(set(values)) == len(values), "Duplicate evidence URL: " + field)


def _percent(count, total):
    return round(count * 100 / total, 1) if count is not None and total is not None and total > 0 else None


def _manifest(root):
    value = _read(root / "published/manifest.json")
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1 and isinstance(value.get("items"), list), "Invalid published manifest for snapshot progress")
    rows, paths = {}, set()
    for item in value["items"]:
        _require(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] and item["id"] not in rows, "Duplicate or missing published item identity")
        _require(item.get("kind") in KINDS - {"all"}, "Invalid published kind")
        _require(isinstance(item.get("files"), list) and item["files"], "Published snapshot item has no files")
        for file in item["files"]:
            _require(isinstance(file, dict) and isinstance(file.get("path"), str), "Invalid published snapshot file")
            path = file["path"]
            _require(path.casefold() not in paths, "Published snapshot files must be globally unique")
            paths.add(path.casefold())
        rows[item["id"]] = item
    return rows


def _snapshot(descriptor, rows):
    _require(isinstance(descriptor, dict) and set(descriptor) <= {"kind", "measure", "stage", "ids", "completed_where"}, "Invalid batch snapshot descriptor")
    kind, measure, stage = descriptor.get("kind"), descriptor.get("measure"), descriptor.get("stage")
    _require(kind in KINDS and measure in MEASURES and stage in STAGES, "Snapshot needs an explicit kind, measure and stage")
    if "ids" in descriptor:
        ids = descriptor["ids"]
        _require(isinstance(ids, list) and all(isinstance(x, str) and x for x in ids) and len(set(ids)) == len(ids), "Snapshot IDs must be explicit and unique")
        _require(all(identity in rows for identity in ids), "Snapshot refers to a missing published identity")
        selected = [rows[identity] for identity in ids]
        _require(kind == "all" or all(x["kind"] == kind for x in selected), "Snapshot IDs differ from its declared kind")
    else:
        selected = [x for x in rows.values() if kind == "all" or x["kind"] == kind]
    if measure == "items":
        count = len(selected)
    elif measure == "files":
        count = sum(len(x["files"]) for x in selected)
    else:
        _require(kind == "caption", "Video-version progress requires caption kind")
        versions = set()
        for item in selected:
            video = item.get("youtube_id")
            _require(isinstance(video, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video), "Video-version snapshot lacks an exact YouTube ID")
            version = item.get("published_edition_sha256") or item.get("source_media_sha256")
            _require(version is None or (isinstance(version, str) and re.fullmatch(r"[a-f0-9]{64}", version)), "Invalid published video-version hash")
            versions.add((video, version))
        count = len(versions)
    stage_count = count
    if "completed_where" in descriptor:
        condition = descriptor["completed_where"]
        _require(measure == "items" and isinstance(condition, dict) and set(condition) == {"content_status"}, "Completed filter supports only content_status with items measure")
        values = condition["content_status"]
        _require(isinstance(values, list) and values and all(isinstance(x, str) and x.strip() for x in values) and len(set(values)) == len(values), "Completed content_status values must be nonempty unique strings")
        _require(all(x.get("content_status") is None or isinstance(x.get("content_status"), str) for x in selected), "Invalid manifest content_status")
        stage_count = sum(x.get("content_status") in values for x in selected)
    return count, stage_count


def _coverage(root, relative, manifest):
    _require(isinstance(relative, str) and "\\" not in relative and ":" not in relative and "\x00" not in relative, "Invalid progress coverage path")
    parts = relative.split("/")
    _require(len(parts) >= 2 and parts[0] == "progress" and all(p not in ("", ".", "..") for p in parts) and relative.endswith(".json"), "Coverage source must be a safe relative JSON path inside progress")
    path = root
    for part in parts:
        path = path / part
        _require(not path.is_symlink(), "Coverage source cannot traverse a symlink")
    value = _read(path)
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1 and isinstance(value.get("items"), list), "Invalid caption coverage schema")
    seen, recorded, unresolved = set(), 0, 0
    for slot in value["items"]:
        _require(isinstance(slot, dict), "Invalid caption coverage slot")
        video, language = slot.get("youtube_id"), slot.get("language")
        _require(isinstance(video, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video) and isinstance(language, str) and language.strip() and not any(c.isspace() for c in language), "Coverage slot needs an exact YouTube ID and language")
        key = (video, language)
        _require(key not in seen, "Duplicate caption coverage video/language slot")
        seen.add(key)
        state = slot.get("state")
        _require(state in ("github_version_recorded", "source_version_unresolved"), "Unknown caption coverage state")
        if state == "github_version_recorded":
            identity = slot.get("caption_item_id")
            _require(isinstance(identity, str) and identity in manifest, "Recorded caption coverage lacks its manifest entity")
            caption = manifest[identity]
            _require(caption["kind"] == "caption" and caption.get("youtube_id") == video and caption.get("language") == language, "Caption coverage manifest entity differs from its video/language slot")
            recorded += 1
        else:
            _require(slot.get("caption_item_id") is None, "Unresolved caption slot cannot claim a recorded manifest entity")
            unresolved += 1
    expected = {"target_language_slots": len(seen), "github_versions_recorded": recorded, "source_version_unresolved": unresolved}
    _require(all(type(value.get(k)) is int and value[k] == count for k, count in expected.items()), "Coverage header counts differ from actual unique slots")
    return {"actual_total": len(seen), "actual_stage_count": recorded, "source_version_unresolved": unresolved}


def _catalogue_metrics(root):
    path = root / "catalog/summary.json"
    value = _read(path) if path.exists() else {}
    _require(isinstance(value, dict), "Invalid catalogue summary")
    selected, excluded = value.get("public_selected_video_files"), value.get("public_excluded_video_files")
    _number(selected, "catalogue selected"); _number(excluded, "catalogue excluded")
    metrics = {}
    for key, label in METRICS.items():
        count = value.get(key)
        _number(count, key)
        _require(count is None or selected is None or count <= selected, "Catalogue metric exceeds the selected-candidate denominator: " + key)
        metrics[key] = {"label": label, "count": count, "total": selected, "percent": _percent(count, selected)}
    return {"selected": selected, "excluded": excluded, "denominator_scope": "current_public_candidates_not_all_p4_or_publication_clearance", "unit": "个候选视频", "metrics": metrics}


def _summary(root):
    path = root / "progress/work-items.json"
    _require(not (root / "progress").is_symlink(), "Progress directory cannot be symlinked")
    value = _read(path)
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1, "Invalid work-items schema version")
    _require(set(value) == {"schema_version", "as_of", "scope_note", "batches", "leads"}, "Work-items uses only explicit batch scope; no overall totals/percentages")
    _stamp(value.get("as_of"), date_allowed=True)
    _text(value.get("scope_note"), "scope_note")
    _require(isinstance(value.get("batches"), list) and isinstance(value.get("leads"), list), "Progress batches and leads must be lists")
    seen, batches, leads, manifest, coverage_sources = set(), [], [], None, set()
    for batch in value["batches"]:
        _require(isinstance(batch, dict) and set(batch) <= {"id", "title", "scope", "total", "unit", "stages", "status", "next_action", "links", "snapshot", "coverage_source"}, "Invalid batch fields")
        for field in ("id", "title", "scope", "unit", "status", "next_action"):
            _text(batch.get(field), field)
        identity = batch["id"]
        _require(identity not in seen, "Duplicate batch/lead identity")
        seen.add(identity)
        _require(batch["status"] not in {"excluded", "overall_complete", "all_archive_complete"} and batch["scope"] not in {"excluded", "family_excluded"}, "Excluded scope belongs in leads/notes, not a pending-publication batch")
        _require("total" in batch, "Batch must explicitly state a denominator or null")
        total = batch["total"]
        _number(total, identity + ":total")
        _urls(batch.get("links", []), identity + ":links", nonempty=False)
        stages = batch.get("stages")
        _require(isinstance(stages, dict) and set(stages) == set(STAGES), "Each batch must record all three independent stages")
        result = {key: batch[key] for key in ("id", "title", "scope", "unit", "status", "next_action")}
        result.update(total=total, links=batch.get("links", []), stages={})
        for stage, entry in stages.items():
            _require(isinstance(entry, dict) and set(entry) <= {"count", "evidence_urls", "note", "observed_at"} and "count" in entry, "Invalid stage fields")
            count = entry["count"]
            _number(count, identity + ":" + stage)
            _require(count is None or total is None or count <= total, "Stage count exceeds its batch denominator")
            _urls(entry.get("evidence_urls"), identity + ":" + stage)
            if "note" in entry:
                _text(entry["note"], "stage note")
            if entry.get("observed_at") is not None:
                _stamp(entry["observed_at"])
            result["stages"][stage] = {**entry, "percent": _percent(count, total), "complete": count is not None and total is not None and total > 0 and count == total}
        _require(not ("snapshot" in batch and "coverage_source" in batch), "Batch snapshot and coverage_source are mutually exclusive")
        if "snapshot" in batch:
            if manifest is None:
                manifest = _manifest(root)
            actual_total, actual_stage_count = _snapshot(batch["snapshot"], manifest)
            stage = batch["snapshot"]["stage"]
            _require(total == actual_total and stages[stage]["count"] == actual_stage_count, "Snapshot denominator and selected stage must equal actual published-manifest counts")
            result["snapshot"] = {**batch["snapshot"], "actual_count": actual_total, "actual_total": actual_total, "actual_stage_count": actual_stage_count}
        if "coverage_source" in batch:
            if manifest is None:
                manifest = _manifest(root)
            coverage = _coverage(root, batch["coverage_source"], manifest)
            _require(total == coverage["actual_total"] and stages["github_published"]["count"] == coverage["actual_stage_count"], "Coverage batch denominator and GitHub count differ from verified slots")
            coverage_sources.add(batch["coverage_source"])
            result.update(coverage_source=batch["coverage_source"], coverage=coverage)
        batches.append(result)
    for lead in value["leads"]:
        _require(isinstance(lead, dict) and set(lead) == {"id", "title", "state", "evidence_urls", "next_action"}, "Invalid lead fields")
        for field in ("id", "title", "state", "next_action"):
            _text(lead[field], "lead " + field)
        _require(lead["id"] not in seen, "Duplicate batch/lead identity")
        seen.add(lead["id"])
        _urls(lead["evidence_urls"], "lead " + lead["id"])
        leads.append(dict(lead))
    source_paths = ["progress/work-items.json"] + (["published/manifest.json"] if manifest is not None else []) + (["catalog/summary.json"] if (root / "catalog/summary.json").is_file() else []) + sorted(coverage_sources)
    return {"schema_version": 1, "as_of": value["as_of"], "scope_note": value["scope_note"], "overall_progress": {"status": "not_calculated", "reason": "Batches have separate scope and units; unknown is not zero; excluded family scope is not pending publication."}, "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in source_paths}, "catalogue_metrics": _catalogue_metrics(root), "batches": batches, "leads": leads}


def _md(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\r", " ").replace("\n", " ")


def _evidence(urls):
    return " · ".join("[证据" + str(i + 1) + "](" + quote(url, safe=":/?#&=%._-~") + ")" for i, url in enumerate(urls))


def _ratio(count, total, percent):
    return (str(count) if count is not None else "未核定") + "/" + (str(total) if total is not None else "未核定") + "（" + (f"{percent:.1f}%" if percent is not None else "未核定") + "）"


def _markdown(summary):
    lines = ["# 档案进度", "", "截至：" + _md(summary["as_of"]), "", _md(summary["scope_note"]), "", "这里记录明确批次的打卡。各批次和单位分别计算；未核定不按0处理，不计算全量档案完成率。整理、GitHub发布、平台发布相互独立。", "", "## 当前原片工序", ""]
    # Batch-scale results come first; raw processing metrics have another scope.
    marker = lines.index("## 当前原片工序")
    quick = ["## 批次速览", "", "各行使用自己的范围与单位，不相加；平台回执的历史/当前范围见下方备注。", "", "| 批次 | 已整理 | GitHub归档 | 平台回执 |", "|---|---|---|---|"]
    for batch in summary["batches"]:
        ratios = [_ratio(batch["stages"][stage]["count"], batch["total"], batch["stages"][stage]["percent"]) for stage in STAGES]
        quick.append("| " + _md(batch["title"]) + "（" + _md(batch["unit"]) + "） | " + " | ".join(ratios) + " |")
    lines[marker:marker] = quick + [""]
    catalogue = summary["catalogue_metrics"]
    selected = catalogue["selected"]
    lines += ["本栏分母：" + (str(selected) if selected is not None else "未核定") + "个当前公开候选视频；这些是工序记录，不是原片已发布比例或全部P4作品数量。", "", "| 工序 | 已记录/本批分母 | 依据 |", "|---|---|---|"]
    for metric in catalogue["metrics"].values():
        lines.append("| " + metric["label"] + " | " + _ratio(metric["count"], metric["total"], metric["percent"]) + " | [当前目录摘要](catalog/summary.json) |")
    excluded = catalogue["excluded"]
    lines += ["", "范围排除：" + (str(excluded) + "项" if excluded is not None else "未核定") + "；单独保留，不计入待发布分母。", "", "## 明确批次", ""]
    for batch in summary["batches"]:
        lines += ["### " + _md(batch["title"]), "", "范围：" + _md(batch["scope"]) + "；单位：" + _md(batch["unit"]) + "。", "", "状态：" + _md(batch["status"]), ""]
        for stage, label in STAGES.items():
            entry = batch["stages"][stage]
            lines.append("- [" + ("x" if entry["complete"] else " ") + "] " + label + "：" + _ratio(entry["count"], batch["total"], entry["percent"]) + " " + _md(batch["unit"]) + " · " + _evidence(entry["evidence_urls"]))
            if entry.get("note"):
                lines.append("  " + _md(entry["note"]))
            if "observed_at" in entry:
                lines.append("  该阶段观察时间：" + (_md(entry["observed_at"]) if entry["observed_at"] else "未记载") + "；不自动视为当前平台复核。")
        lines += ["", "下一步：" + _md(batch["next_action"])]
        if batch["links"]:
            lines += ["", _evidence(batch["links"])]
        lines.append("")
    lines += ["## 后续线索", "", "线索不是已完成发布，也不加入各批分母。", ""]
    for lead in summary["leads"]:
        done = lead["state"] in ("done", "completed")
        lines += ["- [" + ("x" if done else " ") + "] **" + _md(lead["title"]) + "** · " + _md(lead["state"]) + " · " + _evidence(lead["evidence_urls"]), "  下一步：" + _md(lead["next_action"])]
    lines += ["", "口径：[进度口径](docs/进度口径.md) · [维护输入](progress/work-items.json) · [机器可读摘要](progress/summary.json)", ""]
    return "\n".join(lines)


def _artifacts(root):
    root = Path(root).resolve()
    summary = _summary(root)
    return summary, {"PROGRESS.md": _markdown(summary).encode("utf-8"), "progress/summary.json": (json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")}


def _result(summary):
    return {"valid": True, "errors": [], "batch_count": len(summary["batches"]), "lead_count": len(summary["leads"]), "overall_percent": None}


def build(root):
    root = Path(root).resolve()
    summary, artifacts = _artifacts(root)  # Finish validation before output writes.
    for relative in artifacts:
        target = root / relative
        _require(not target.is_symlink() and (not target.exists() or target.is_file()), "Progress output must be a regular file")
    for relative, body in artifacts.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".progress-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return _result(summary)


def check(root):
    try:
        summary, artifacts = _artifacts(root)
        errors = ["Progress output differs from its inputs: " + relative for relative, body in artifacts.items() if (Path(root) / relative).is_symlink() or not (Path(root) / relative).is_file() or (Path(root) / relative).read_bytes() != body]
        return {**_result(summary), "valid": not errors, "errors": errors}
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return {"valid": False, "errors": [str(exc)], "overall_percent": None}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check"))
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        result = build(args.root) if args.command == "build" else check(args.root)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        result = {"valid": False, "errors": [str(exc)], "overall_percent": None}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
