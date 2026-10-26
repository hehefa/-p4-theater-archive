#!/usr/bin/env python3
"""Export existing project identifiers; never allocate or reconcile identities."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


TARGET = Path("registry/identity-crosswalk.json")
BASES = {"directory_title_match_only", "existing_scoped_registry_slug_and_official_index_slug_match"}


def public_text(value, label):
    if not isinstance(value, str) or not value or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid public " + label)
    return value


def public_url(value, hosts):
    public_text(value, "project URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc not in hosts or parsed.query or parsed.fragment or not re.fullmatch(r"/(?:wechat/projects|en/projects|projects|works)/[a-z0-9-]+/?", parsed.path):
        raise ValueError("Project URL is outside the known public project routes")
    return value


def identifier_projection(source):
    row = {key: source[key] for key in ("namespace", "value", "mapping_basis", "asset_membership_verified") if key in source}
    for field in ("namespace", "value"):
        value = public_text(row.get(field), "identifier")
        pattern = r"[A-Za-z0-9_.:\[\]/-]+" if field == "namespace" else r"[A-Za-z0-9_.:-]+"
        if value.startswith(("/", "\\")) or not re.fullmatch(pattern, value):
            raise ValueError("Invalid external identifier")
    if "mapping_basis" in row and (not isinstance(row["mapping_basis"], str) or row["mapping_basis"] not in BASES):
        raise ValueError("Unknown project matching basis")
    if "asset_membership_verified" in row and type(row["asset_membership_verified"]) is not bool:
        raise ValueError("Asset membership must remain a boolean")
    return row


def projection(root):
    projects = json.loads((Path(root) / "catalog/projects.json").read_text(encoding="utf-8"))["items"]
    rows = []
    owners = {}
    for project in sorted(projects, key=lambda item: item["accession"]):
        identifiers = []
        for source in project.get("existing_registry_identifiers", []):
            identifiers.append(identifier_projection(source))
        for source in project.get("official_registry_references", []):
            identifiers.append(identifier_projection({
                "namespace": source["official_code_namespace"],
                "value": source["official_project_code"],
                "mapping_basis": source["mapping_basis"],
                "asset_membership_verified": source.get("asset_membership_verified", False),
            }))
        unique = {}
        for identifier in identifiers:
            key = (identifier.get("namespace"), identifier.get("value"))
            if not all(isinstance(value, str) and value for value in key):
                raise ValueError("An external identifier lacks its namespace or value")
            previous = owners.setdefault(key, project["id"])
            if previous != project["id"]:
                raise ValueError("An external identifier has conflicting catalogue owners")
            unique.setdefault(key, identifier)
        related = project.get("related_public_project_page")
        if related is not None:
            related = public_url(related, {"b.p4theater.cn"})
        pages = []
        for reference in project.get("official_registry_references", []):
            page = {}
            for key in ("official_project_code", "official_project_slug", "snapshot_at"):
                if key in reference:
                    page[key] = public_text(reference[key], "official project reference")
            for key in ("project_url_zh", "project_url_en"):
                if key in reference:
                    page[key] = public_url(reference[key], {"a.p4theater.cn"})
            pages.append(page)
        aliases = project.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", alias) for alias in aliases):
            raise ValueError("Invalid legacy alias")
        rows.append({
            "catalogue_id": project["id"],
            "accession": project["accession"],
            "title": project["title"],
            "reconciliation_status": project.get("legacy_reconciliation", "pending"),
            "legacy_aliases": aliases,
            "external_identifiers": [unique[key] for key in sorted(unique)],
            "related_public_project_page": related,
            "official_project_pages": pages,
            "scope": "existing_project_level_references_only_not_file_membership_or_identity_merger",
        })
    return {
        "schema_version": 1,
        "generated_from": "catalog/projects.json",
        "purpose": "Portable existing-ID crosswalk for Mac, PC and source adapters; no new identities or matching claims.",
        "project_count": len(rows),
        "items": rows,
    }


def encoded(root):
    return (json.dumps(projection(root), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build(root):
    data = encoded(root)
    target = Path(root) / TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": TARGET.as_posix(), "projects": len(projection(root)["items"])}


def check(root):
    target = Path(root) / TARGET
    return target.is_file() and target.read_bytes() == encoded(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        valid = check(args.root)
        print(json.dumps({"valid": valid, "path": TARGET.as_posix()}))
        return 0 if valid else 1
    print(json.dumps(build(args.root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
