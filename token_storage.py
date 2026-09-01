"""Small, dependency-free helpers for safely replacing token JSON files."""

import json
import os
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path


@contextmanager
def locked_path(path: Path):
    """Serialize bundled writers for one destination across processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as lock_handle:
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"\0")
            lock_handle.flush()
        lock_handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(
    path: Path, payload: dict, *, acquire_lock: bool = True
) -> None:
    """Write JSON with a same-directory atomic replace and restrictive mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_context = locked_path(path) if acquire_lock else nullcontext()
    with lock_context:
        _atomic_write_json_unlocked(path, payload)


def _atomic_write_json_unlocked(path: Path, payload: dict) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
