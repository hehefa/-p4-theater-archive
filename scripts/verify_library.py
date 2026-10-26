#!/usr/bin/env python3
"""Sequential, resumable local SHA-256 and ffprobe metadata verification.

No media decoding, GPU task, upload or original-file mutation is performed.
All paths, raw probe output, locks and running state stay outside the public root.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from archive_catalog import pathkey, read_json


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(31):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if attempt == 30 or (getattr(exc, "winerror", None) not in (5, 32, 33) and exc.errno not in (errno.EACCES, errno.EPERM)):
                raise
            time.sleep(0.1)


class ProcessLock:
    """Kernel-held file lock: crashes release it without stale-PID deletion."""
    def __init__(self, path, job_dir):
        self.path, self.job_dir = Path(path), str(job_dir)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b" ")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another verification process holds the local-state lock") from exc
        self._owner(True)
        return self

    def _owner(self, active):
        self.handle.seek(1)
        self.handle.truncate()
        self.handle.write(json.dumps({"pid": os.getpid(), "active": active, "updated_at": now(), "job_dir": self.job_dir}).encode("utf-8"))
        self.handle.flush()

    def __exit__(self, *unused):
        if self.handle is not None:
            self._owner(False)
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def same_fingerprint(stat, item):
    return stat.st_size == item["bytes"] and stat.st_mtime_ns == item["mtime_ns"]


def probe_file(ffprobe, source, timeout):
    started = time.monotonic()
    try:
        proc = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)], capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        try:
            raw = json.loads(output) if output else {}
        except json.JSONDecodeError:
            raw = {}
        streams = [{k: s.get(k) for k in ("index", "codec_type", "codec_name", "width", "height", "sample_rate", "channels", "duration", "r_frame_rate") if k in s} for s in raw.get("streams", [])]
        duration = raw.get("format", {}).get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        status = "metadata_readable" if proc.returncode == 0 and any(s.get("codec_type") == "video" for s in streams) else "metadata_probe_failed"
        return {"status": status, "exit_code": proc.returncode, "duration_seconds": duration, "streams": streams, "stderr": stderr[-12000:], "elapsed_seconds": round(time.monotonic() - started, 3), "raw": raw, "scope": "ffprobe_metadata_only_not_full_decode"}
    except subprocess.TimeoutExpired:
        return {"status": "metadata_probe_timeout", "exit_code": None, "duration_seconds": None, "streams": [], "elapsed_seconds": round(time.monotonic() - started, 3), "scope": "ffprobe_metadata_only_not_full_decode"}


class VerificationJob:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root).resolve()
        self.state = Path(args.local_state).resolve()
        self.job = Path(args.job_dir).resolve()
        for private in (self.state, self.job):
            if private == self.root or self.root in private.parents:
                raise ValueError("Private job/local-state must be outside the public repository")
        self.ffprobe = shutil.which(args.ffprobe) or (str(Path(args.ffprobe).resolve()) if Path(args.ffprobe).exists() else None)
        if not self.ffprobe:
            raise ValueError("ffprobe executable is unavailable")
        mapping = read_json(self.state / "mapping.json")["files"]
        catalog = read_json(self.root / "catalog" / "files.json")["items"]
        self.items = []
        for entry in catalog:
            if not entry.get("in_current_inventory", True):
                continue
            if args.only_file and entry["accession"] not in args.only_file and entry["id"] not in args.only_file:
                continue
            location = mapping[entry["id"]]
            self.items.append({**location, "file_id": entry["id"], "accession": entry["accession"], "catalog_bytes": entry["bytes"], "trusted_sha256": entry.get("sha256")})
        if args.only_file and len(self.items) != len(set(args.only_file)):
            raise ValueError("Some --only-file references were absent or duplicated by accession/ID")
        manifest = [{k: x[k] for k in ("file_id", "bytes", "mtime_ns")} for x in self.items]
        self.fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.job.mkdir(parents=True, exist_ok=True)
        config_path = self.job / "run_config.json"
        if config_path.exists() and read_json(config_path)["source_manifest_sha256"] != self.fingerprint:
            raise ValueError("Snapshot changed; create a new job directory, reusing the existing local hash cache")
        atomic_json(config_path, {"schema_version": 1, "source_manifest_sha256": self.fingerprint, "selected_source_count": len(self.items), "selected_source_bytes": sum(x["bytes"] for x in self.items), "scope": "full_sha256_and_ffprobe_metadata_only", "ffprobe": self.ffprobe})
        self.results = {}
        for item in self.items:
            saved = self.job / "results" / (item["accession"] + ".json")
            if saved.exists():
                previous = read_json(saved)
                source = Path(item["path"])
                unchanged = source.exists() and same_fingerprint(source.stat(), item)
                still_missing = not source.exists() and previous.get("status") == "missing_source"
                if previous.get("fingerprint") == {"bytes": item["bytes"], "mtime_ns": item["mtime_ns"]} and (unchanged or still_missing):
                    self.results[item["file_id"]] = previous
        self.hashes = read_json(self.state / "hash_results.json") if (self.state / "hash_results.json").exists() else {"schema_version": 1, "files": {}}
        self.probes = read_json(self.state / "probe_results.json") if (self.state / "probe_results.json").exists() else {"schema_version": 1, "files": {}}
        self.started_at = now()
        self.current = None
        self.newly_processed = 0

    def event(self, action, **details):
        record = {"at": now(), "event": action, **details}
        with (self.job / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def progress(self, status="running"):
        counts = dict(Counter(x["status"] for x in self.results.values()))
        value = {"schema_version": 1, "pid": os.getpid(), "status": status, "started_at": self.started_at, "updated_at": now(), "selected_source_count": len(self.items), "selected_source_bytes": sum(x["bytes"] for x in self.items), "processed_count": len(self.results), "processed_snapshot_bytes": sum(x["fingerprint"]["bytes"] for x in self.results.values()), "status_counts": counts, "hash_basis_counts": dict(Counter(x.get("hash_basis", "not_accepted") for x in self.results.values())), "newly_processed_this_run": self.newly_processed, "current": self.current, "verification_scope": "full_sha256_and_ffprobe_metadata_only_not_full_decode_or_human_review"}
        atomic_json(self.job / "progress.json", value)
        return value

    def hash_file(self, source, item):
        key = pathkey(source)
        cached = self.hashes["files"].get(key)
        if cached and cached["bytes"] == item["bytes"] and cached["mtime_ns"] == item["mtime_ns"]:
            return cached["sha256"], "reused_local_full_hash"
        if item["trusted_sha256"]:
            return item["trusted_sha256"], "reused_catalog_full_hash_with_unchanged_snapshot_fingerprint"
        h = hashlib.sha256()
        done = 0
        last = 0
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                h.update(chunk)
                done += len(chunk)
                if time.monotonic() - last >= 3:
                    self.current.update(stage="hashing", bytes_hashed=done)
                    self.progress()
                    last = time.monotonic()
        return h.hexdigest(), "full_file_sha256_this_run"

    def one_file(self, item):
        source = Path(item["path"])
        result = {"schema_version": 1, "file_id": item["file_id"], "accession": item["accession"], "path": str(source), "fingerprint": {"bytes": item["bytes"], "mtime_ns": item["mtime_ns"]}, "started_at": now(), "sha256": None, "hash_status": "not_computed", "probe": None}
        self.current = {"file_id": item["file_id"], "accession": item["accession"], "stage": "checking_snapshot", "bytes": item["bytes"], "bytes_hashed": 0}
        self.progress()
        if item["catalog_bytes"] != item["bytes"]:
            result["status"] = "catalog_and_private_snapshot_disagree"
        elif not source.exists():
            result["status"] = "missing_source"
        elif not same_fingerprint(source.stat(), item):
            result["status"] = "source_changed_since_snapshot"
        else:
            try:
                sha, basis = self.hash_file(source, item)
                if not same_fingerprint(source.stat(), item):
                    result["status"] = "source_changed_during_hash"
                else:
                    result.update(sha256=sha, hash_status="full_sha256_recorded", hash_basis=basis)
                    self.current.update(stage="probing_metadata", bytes_hashed=item["bytes"])
                    self.progress()
                    probe = probe_file(self.ffprobe, source, self.args.probe_timeout)
                    if not same_fingerprint(source.stat(), item):
                        result.update(status="source_changed_during_probe", sha256=None, hash_status="invalidated_source_changed")
                    else:
                        result["probe"] = probe
                        result["status"] = "hash_and_metadata_readable" if probe["status"] == "metadata_readable" else "hash_recorded_metadata_needs_review"
                        self.hashes["files"][pathkey(source)] = {"path": str(source), "bytes": item["bytes"], "mtime_ns": item["mtime_ns"], "sha256": sha, "verified_at": now(), "basis": basis}
                        self.probes["files"][pathkey(source)] = {"path": str(source), "bytes": item["bytes"], "mtime_ns": item["mtime_ns"], "verified_at": now(), "status": probe["status"], "duration_seconds": probe["duration_seconds"], "streams": probe["streams"], "scope": probe["scope"]}
                        atomic_json(self.state / "hash_results.json", self.hashes)
                        atomic_json(self.state / "probe_results.json", self.probes)
            except (OSError, ValueError) as exc:
                result.update(status="verification_error", error=str(exc), sha256=None, hash_status="not_accepted")
        result["finished_at"] = now()
        atomic_json(self.job / "results" / (item["accession"] + ".json"), result)
        self.results[item["file_id"]] = result
        self.newly_processed += 1
        self.event("file_finished", accession=item["accession"], status=result["status"], hash_basis=result.get("hash_basis"), probe_status=(result.get("probe") or {}).get("status"))
        self.current = None
        self.progress()

    def run(self):
        self.event("run_started", pid=os.getpid(), sources=len(self.items), resumed_results=len(self.results), scope="metadata_probe_and_sha256_only")
        status = "running"
        try:
            for item in self.items:
                if item["file_id"] in self.results:
                    continue
                if self.args.limit and self.newly_processed >= self.args.limit:
                    status = "paused_at_requested_limit"
                    break
                self.one_file(item)
            else:
                status = "completed_with_findings" if any(x["status"] != "hash_and_metadata_readable" for x in self.results.values()) else "completed"
        except BaseException as exc:
            status = "interrupted"
            self.event("run_interrupted", exception_type=type(exc).__name__, detail=str(exc))
            raise
        finally:
            report = self.progress(status)
            report["finished_at"] = now()
            report["all_selected_sources_processed"] = len(self.results) == len(self.items)
            report["full_decode_performed"] = False
            report["human_review_performed"] = False
            atomic_json(self.job / "completion.json", report)
            self.event("run_finished", status=status, processed=len(self.results), selected=len(self.items))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--probe-timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0, help="Maximum new files this invocation; 0 means all selected files")
    parser.add_argument("--only-file", action="append", default=[], help="Optional representative file ID or accession; use a separate sample job directory")
    args = parser.parse_args()
    if args.limit < 0 or args.probe_timeout < 1:
        parser.error("limit must be nonnegative and probe-timeout positive")
    with ProcessLock(Path(args.local_state) / "library_verification.lock", args.job_dir):
        job = VerificationJob(args)
        report = job.run()
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
