"""Small, non-executable JSON cache helpers.

Embedding caches can be rebuilt, so they should never require a serializer
capable of executing constructors.  These helpers also make writes atomic to
avoid leaving half-written cache files after interruption.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class JsonCacheError(ValueError):
    """Raised when a JSON cache is missing the expected safe shape."""


def load_json_object(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    """Load a bounded JSON object without following a cache-file symlink."""

    path = Path(path)
    if path.is_symlink():
        raise JsonCacheError(f"refusing symlinked cache file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise JsonCacheError(f"cache path is not a regular file: {path}")
            if info.st_size > max_bytes:
                raise JsonCacheError(
                    f"cache file is too large ({info.st_size} bytes; limit {max_bytes})"
                )
            with os.fdopen(descriptor, "rb") as cache_file:
                descriptor = -1
                payload_bytes = cache_file.read(max_bytes + 1)
            if len(payload_bytes) > max_bytes:
                raise JsonCacheError(f"cache file exceeds the {max_bytes}-byte limit")
            payload = payload_bytes.decode("utf-8")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        value = json.loads(payload)
    except JsonCacheError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JsonCacheError(f"invalid JSON cache: {exc}") from exc
    if not isinstance(value, dict):
        raise JsonCacheError("JSON cache root must be an object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a private JSON object next to its final destination."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
