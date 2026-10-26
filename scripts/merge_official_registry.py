#!/usr/bin/env python3
"""Add a source-attributed official public index snapshot as a separate registry."""
import argparse
import json
from pathlib import Path
from archive_catalog import TABLES, read_json, render_indexes, validate, write_json
from catalog_transaction import staged_update

ITEM_FIELDS = ("official_project_code", "official_code_namespace", "official_project_slug", "official_slug_namespace", "title_zh", "title_en", "source_type", "project_url_zh", "project_url_en", "published_temporal_coverage", "p4_relation_evidence", "catalogue_status", "current_video_catalogue_matches", "candidate_current_video_catalogue_matches", "current_video_inventory_attached", "video_absence_claim", "source_index_urls")


def _merge_registry(root, staging):
    root = Path(root)
    prepared = read_json(staging)
    registry = {"schema_version": 1, "snapshot_at": prepared["prepared_at"], "scope": prepared["scope"], "sources": prepared["sources"], "summary": prepared["summary"], "preservation_rules": prepared["preservation_rules"], "items": [{key: item[key] for key in ITEM_FIELDS} for item in prepared["items"]]}
    projects = read_json(root / "catalog/projects.json")
    by_id = {x["id"]: x for x in projects["items"]}
    matched_count = 0
    for item in registry["items"]:
        for match in item["current_video_catalogue_matches"]:
            project = by_id[match["id"]]
            slugs = {x["value"] for x in project.get("existing_registry_identifiers", [])}
            if project["accession"] != match["accession"] or item["official_project_slug"] not in slugs:
                raise ValueError("Official direct match lacks the existing scoped registry slug")
            code = item["official_project_code"]
            project["aliases"] = list(dict.fromkeys(project.get("aliases", []) + [code]))
            reference = {key: item[key] for key in ("official_project_code", "official_code_namespace", "official_project_slug", "project_url_zh", "project_url_en")}
            reference.update(snapshot_at=registry["snapshot_at"], mapping_basis=match["mapping_basis"], asset_membership_verified=False)
            old_refs = [x for x in project.get("official_registry_references", []) if x["official_project_code"] != code]
            project["official_registry_references"] = old_refs + [reference]
            matched_count += 1
    write_json(root / "registry/official-projects.json", registry)
    write_json(root / "catalog/projects.json", projects)
    summary = read_json(root / "catalog/summary.json")
    summary["official_public_registry_count"] = len(registry["items"])
    write_json(root / "catalog/summary.json", summary)
    lines = ["# 官方公开项目索引快照", "", f"本页保存 {registry['snapshot_at']} 抽取的官方中英文公开索引，共 {len(registry['items'])} 条。两种语言页面的项目编号集合一致。", "", "它是公开注册表的一个快照。本批本地视频目录另有 16 个候选项目、18 个来源集合；两种口径不能相加，也不能据此声称已经列尽 P4 全部历史项目。没有关联到本批视频，不代表该项目不存在录像。", "", "5 个条目按既有注册表的项目 slug 对应到本地候选项目；3P 小组仅列名称候选，不建立身份等同。任何项目级对照都不能代替逐视频的内容归属与公开范围核验。", "", "| 官方编号 | 项目 | English | 官方年代线索 | 本批视频目录关系 |", "|---|---|---|---|---|"]
    for item in sorted(registry["items"], key=lambda x: x["official_project_code"]):
        linked = item["current_video_catalogue_matches"]
        candidates = item["candidate_current_video_catalogue_matches"]
        if linked:
            relation = " / ".join("[" + x["accession"] + "](../projects/" + x["accession"] + "/index.md)（项目级对照）" for x in linked)
        elif candidates:
            relation = " / ".join("[" + x["accession"] + "](../projects/" + x["accession"] + "/index.md)（仅名称候选）" for x in candidates)
        else:
            relation = "注册表收录；未关联本批视频"
        lines.append("| " + item["official_project_code"] + " | [" + item["title_zh"] + "](" + item["project_url_zh"] + ") | [" + item["title_en"] + "](" + item["project_url_en"] + ") | " + (item["published_temporal_coverage"] or "未列") + " | " + relation + " |")
    lines += ["", "北风剧社 P4C-061 与漩涡 P4C-014 分开登记；决斗俱乐部 P4C-012 与紫色行动 P4C-040 分开登记。玻璃动物园沿用研究注册表已明确的 P4C-018；它未列在这个官方快照里，不能因此改成玻璃剧场 P4C-064。", "", "官方页面给出的年代仅作为原样来源字段，不据此填写本地某次排练或演出的发生日期。", "", "来源：[中文公开索引](https://a.p4theater.cn/wechat/projects)、[English index](https://a.p4theater.cn/en/projects)。原始响应的 UTF-8 SHA-256、读取时间与字段抽取范围保存在 [机器可读注册表](official-projects.json)。", "", "[返回本地项目与视频目录](../INDEX.md)。", ""]
    (root / "registry/README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    tables = {name: read_json(root / "catalog" / (name + ".json"))["items"] for name in TABLES}
    render_indexes(root, tables, summary)
    result = validate(root)
    if not result["valid"]:
        raise ValueError("Official registry failed validation: " + json.dumps(result["errors"], ensure_ascii=False))
    return {"official_public_registry_items": len(registry["items"]), "direct_project_level_matches": matched_count, "local_project_count_unchanged": len(projects["items"]), "valid": True}


def merge_registry(root, staging):
    return staged_update(root, lambda candidate, unused: _merge_registry(candidate, staging), validate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--staging", required=True)
    args = parser.parse_args()
    print(json.dumps(merge_registry(args.root, args.staging), ensure_ascii=False))


if __name__ == "__main__":
    main()
