"""Resumable metadata verification failures use synthetic bytes only."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_verification as verification_tests
import verify_library as verifier


def probe(status="metadata_readable"):
    return {"status": status, "duration_seconds": 1, "streams": [{"codec_type": "video"}], "scope": "ffprobe_metadata_only_not_full_decode"}


def run(job, status="metadata_readable"):
    with mock.patch.object(verifier, "probe_file", return_value=probe(status)) as called:
        with contextlib.redirect_stdout(io.StringIO()):
            result = job.run()
    return result, called.call_count


class RetryTests(unittest.TestCase):
    def test_restored_missing_source_is_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, source = verification_tests.VerificationTests().make_job(Path(tmp))
            contents, stat = source.read_bytes(), source.stat()
            source.unlink()
            run(first)
            self.assertEqual(first.results["file:fixture"]["status"], "missing_source")
            still_missing = verifier.VerificationJob(first.args)
            self.assertIn("file:fixture", still_missing.results)
            source.write_bytes(contents)
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            restored = verifier.VerificationJob(first.args)
            self.assertNotIn("file:fixture", restored.results)
            result, calls = run(restored)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, 1)

    def test_explicit_retry_reprobes_timeout_and_reuses_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, source = verification_tests.VerificationTests().make_job(Path(tmp))
            run(first, "metadata_probe_timeout")
            self.assertEqual(first.results["file:fixture"]["status"], "hash_recorded_metadata_needs_review")
            resumed = verifier.VerificationJob(first.args)
            self.assertIn("file:fixture", resumed.results)
            first.args.retry_failed = True
            retried = verifier.VerificationJob(first.args)
            self.assertNotIn("file:fixture", retried.results)
            result, calls = run(retried)
            self.assertEqual(calls, 1)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(retried.results["file:fixture"]["hash_basis"], "reused_local_full_hash")
            successful = verifier.VerificationJob(first.args)
            result, calls = run(successful)
            self.assertEqual(calls, 0)
            self.assertEqual(result["newly_processed_this_run"], 0)


if __name__ == "__main__":
    unittest.main()
