#!/usr/bin/env python3
"""Resume a local full-file SHA-256 queue. Never uploads source files."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from archive_catalog import digest, pathkey, read_json, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--limit", type=int, default=1, help="Maximum files this invocation; 0 means all")
    args = parser.parse_args()
    state = Path(args.local_state)
    queue = read_json(state / "hash_plan.json")["files"]
    result_path = state / "hash_results.json"
    results = read_json(result_path) if result_path.exists() else {"schema_version": 1, "files": {}}
    done = 0
    failures = []
    for item in queue:
        path = Path(item["path"])
        key = pathkey(path)
        stat = path.stat()
        cached = results["files"].get(key)
        if cached and cached["bytes"] == stat.st_size and cached["mtime_ns"] == stat.st_mtime_ns:
            continue
        if stat.st_size != item["bytes"] or stat.st_mtime_ns != item["mtime_ns"]:
            failures.append({"file_id": item["file_id"], "reason": "source_changed_since_inventory"})
            continue
        sha = digest(path)
        after = path.stat()
        if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
            failures.append({"file_id": item["file_id"], "reason": "source_changed_during_hash"})
            continue
        results["files"][key] = {"path": str(path), "bytes": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": sha, "verified_at": datetime.now(timezone.utc).isoformat()}
        write_json(result_path, results)
        done += 1
        print(json.dumps({"accession": item["accession"], "sha256": sha, "bytes": after.st_size}), flush=True)
        if args.limit and done >= args.limit:
            break
    print(json.dumps({"hashed_this_run": done, "failures": failures, "rerun_import_to_refresh_public_metadata": True}))


if __name__ == "__main__":
    main()
