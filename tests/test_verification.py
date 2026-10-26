import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_library as verifier
from archive_catalog import write_json


class VerificationTests(unittest.TestCase):
    def make_job(self, work):
        public, private = work / "public", work / "private"
        source = work / "fixture.bin"
        source.write_bytes(b"original snapshot")
        stat = source.stat()
        write_json(public / "catalog/files.json", {"items": [{"id": "file:fixture", "accession": "P4-F000001", "bytes": stat.st_size, "sha256": "a" * 64, "in_current_inventory": True}]})
        write_json(private / "mapping.json", {"files": {"file:fixture": {"path": str(source), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}}})
        args = SimpleNamespace(root=str(public), local_state=str(private), job_dir=str(private / "job"), ffprobe=sys.executable, only_file=[], limit=0, probe_timeout=1)
        return verifier.VerificationJob(args), source

    def test_changed_snapshot_cannot_reuse_an_old_trusted_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, source = self.make_job(Path(tmp))
            source.write_bytes(b"changed after snapshot and larger")
            with mock.patch.object(verifier, "probe_file") as probe:
                with contextlib.redirect_stdout(io.StringIO()):
                    job.run()
            result = job.results["file:fixture"]
            self.assertEqual(result["status"], "source_changed_since_snapshot")
            self.assertIsNone(result["sha256"])
            self.assertEqual(job.hashes["files"], {})
            probe.assert_not_called()

    def test_source_change_during_hash_invalidates_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, source = self.make_job(Path(tmp))
            def changing_hash(*unused):
                source.write_bytes(b"replaced during hash")
                return "a" * 64, "fixture_hash"
            with mock.patch.object(job, "hash_file", changing_hash), mock.patch.object(verifier, "probe_file") as probe:
                with contextlib.redirect_stdout(io.StringIO()):
                    job.run()
            result = job.results["file:fixture"]
            self.assertEqual(result["status"], "source_changed_during_hash")
            self.assertIsNone(result["sha256"])
            probe.assert_not_called()

    def test_kernel_lock_prevents_second_verifier_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            code = "import sys;sys.path.insert(0," + repr(str(ROOT / "scripts")) + ");from verify_library import ProcessLock;lock=ProcessLock(" + repr(str(path)) + ",'fixture');lock.__enter__();lock.__exit__()"
            with verifier.ProcessLock(path, "fixture"):
                blocked = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8")
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("Another verification process", blocked.stderr)
            released = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(released.returncode, 0, released.stderr)


if __name__ == "__main__":
    unittest.main()
