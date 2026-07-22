import os
from pathlib import Path

import pytest

from openspace.utils.safe_json_cache import (
    JsonCacheError,
    atomic_write_json,
    load_json_object,
)


def test_atomic_json_round_trip_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"

    atomic_write_json(path, {"version": 1, "values": [1.0, 2.0]})

    assert load_json_object(path) == {"version": 1, "values": [1.0, 2.0]}
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_json_cache_rejects_non_object_and_symlink(tmp_path: Path) -> None:
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(JsonCacheError, match="root must be an object"):
        load_json_object(array_path)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(JsonCacheError, match="symlinked"):
        load_json_object(link)


def test_json_cache_enforces_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_text('{"payload":"too large"}', encoding="utf-8")

    with pytest.raises(JsonCacheError, match="too large|exceeds"):
        load_json_object(path, max_bytes=8)
