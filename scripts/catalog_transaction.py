"""Stage catalogue changes, validate them, and promote one recoverable batch.

The journal and backups live outside the public root. A failed promotion restores
both public output and private allocation files; an interrupted batch is rolled
back by the next invocation using the same local state before new work starts.
This is a local filesystem transaction, not a cross-machine coordination lock.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

STATE_FILES = ("registry.json", "mapping.json", "hash_plan.json", "hash_results.json", "probe_results.json", "public_scope_projection_report.json")
PENDING = ".catalog-batch-pending.json"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class StateLock:
    def __init__(self, path, job_dir, *, allow_pending=False):
        self.path, self.job_dir = Path(path), str(job_dir)
        self.allow_pending, self.handle = allow_pending, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        if not self.allow_pending and (self.path.parent / PENDING).exists():
            self.__exit__()
            raise RuntimeError("An incomplete catalogue batch must be recovered with its original local state before verification")
        return self

    def __exit__(self, *unused):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _hash(path):
    path = Path(path)
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _snapshot(root, names=None):
    root = Path(root)
    paths = [root / name for name in names] if names is not None else root.rglob("*")
    result = {}
    for path in paths:
        parts = path.relative_to(root).parts
        if any(part in (".git", "__pycache__", ".pytest_cache") for part in parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Catalogue staging does not follow symbolic links")
        if path.is_file() and ".git" not in path.relative_to(root).parts and "__pycache__" not in path.parts:
            result[path.relative_to(root).as_posix()] = _hash(path)
    return result


def _atomic_copy(source, target, private=False):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        if private:
            temporary.chmod(0o600)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _restore(journal):
    for change in reversed(journal["changes"]):
        target = Path(change["target"])
        if change["before"] is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_copy(change["backup"], target, change["private"])
    for parent in sorted(journal.get("created_parents", []), key=len, reverse=True):
        try:
            Path(parent).rmdir()
        except OSError:
            pass


def _finish(journal_path, journal):
    if journal.get("local_state"):
        (Path(journal["local_state"]) / PENDING).unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    shutil.rmtree(journal["workspace"], ignore_errors=True)


def staged_update(root, transform, validator, *, local_state=None):
    """Run transform(public_candidate, private_candidate), then validate/promote.

    Only files actually changed in the candidate are promoted. Other edits to the
    checkout are preserved, and concurrent changes to a selected target abort the
    batch before any output/allocation file is replaced.
    """
    root = Path(root).resolve()
    state = Path(local_state).resolve() if local_state is not None else None
    if state is not None and (state == root or root in state.parents):
        raise ValueError("Private local state must be outside public output")
    control = root.parent / ("." + root.name + "-catalog-transactions")
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal_path = control / "pending.json"
    with ExitStack() as locks:
        locks.enter_context(StateLock(control / "output.lock", root, allow_pending=True))
        if state is not None:
            locks.enter_context(StateLock(state / "library_verification.lock", root, allow_pending=True))
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("local_state") != (str(state) if state else None):
                raise RuntimeError("Recover the incomplete catalogue batch using its original local state")
            if journal["status"] != "committed":
                _restore(journal)
            _finish(journal_path, journal)
        elif state is not None and (state / PENDING).exists():
            raise RuntimeError("Local state belongs to an incomplete batch for another public root")
        workspace = Path(tempfile.mkdtemp(prefix="batch-", dir=control))
        try:
            before = _snapshot(root)
            public = workspace / "public"
            if root.exists():
                shutil.copytree(root, public, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"))
            else:
                public.mkdir()
            private = None
            state_before = {}
            if state is not None:
                state_before = _snapshot(state, STATE_FILES)
                private = workspace / "private"
                private.mkdir(mode=0o700)
                for name in state_before:
                    shutil.copy2(state / name, private / name)
            result = transform(public, private)
            checked = validator(public)
            if not checked["valid"]:
                raise ValueError("Candidate catalogue failed validation: " + json.dumps(checked["errors"], ensure_ascii=False))
            pairs = [(root, public, before, False)]
            if state is not None:
                pairs.append((state, private, state_before, True))
            changes, parents = [], set()
            for destination, candidate, original, is_private in pairs:
                after = _snapshot(candidate, STATE_FILES if is_private else None)
                for relative in sorted(set(original) | set(after)):
                    if original.get(relative) == after.get(relative):
                        continue
                    target = destination / relative
                    if _hash(target) != original.get(relative):
                        raise RuntimeError("A catalogue output changed concurrently; no batch files were replaced")
                    backup = workspace / "backups" / str(len(changes))
                    if target.exists():
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                    parent = target.parent
                    while not parent.exists() and parent != destination:
                        parents.add(str(parent))
                        parent = parent.parent
                    changes.append({"target": str(target), "source": str(candidate / relative) if relative in after else None, "backup": str(backup), "before": original.get(relative), "private": is_private})
            if not changes:
                return result
            journal = {"status": "committing", "workspace": str(workspace), "local_state": str(state) if state else None, "changes": changes, "created_parents": sorted(parents)}
            atomic_json(journal_path, journal)
            if state is not None:
                atomic_json(state / PENDING, {"journal": str(journal_path)})
            try:
                for change in changes:
                    if change["source"] is None:
                        Path(change["target"]).unlink(missing_ok=True)
                    else:
                        _atomic_copy(change["source"], change["target"], change["private"])
                journal["status"] = "committed"
                atomic_json(journal_path, journal)
            except BaseException:
                _restore(journal)
                _finish(journal_path, journal)
                raise
            _finish(journal_path, journal)
            return result
        finally:
            if not journal_path.exists():
                shutil.rmtree(workspace, ignore_errors=True)
