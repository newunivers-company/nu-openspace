import pickle
from pathlib import Path

from openspace.grounding.core.search_tools import ToolRanker
from openspace.grounding.core.tool.local_tool import LocalTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.skill_engine.skill_ranker import SkillRanker


class _TestTool(LocalTool):
    _name = "cache_test"
    _description = "Embedding cache test"
    backend_type = BackendType.META

    async def _arun(self) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, content="ok")


class _PicklePayload:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self):
        return (Path.write_text, (self.marker, "pickle executed"))


def test_tool_embedding_cache_round_trips_as_json(tmp_path: Path) -> None:
    ranker = ToolRanker(
        model_name="test/model",
        cache_dir=tmp_path,
        enable_cache_persistence=True,
    )
    tool = _TestTool()
    ranker._set_embedding(tool, [0.25, 0.75])
    ranker._save_persistent_cache()

    cache_file = tmp_path / "embeddings_test_model_v2.json"
    assert cache_file.exists()
    assert not list(tmp_path.glob("*.pkl"))

    restored = ToolRanker(
        model_name="test/model",
        cache_dir=tmp_path,
        enable_cache_persistence=True,
    )
    assert restored._get_embedding(tool) == [0.25, 0.75]


def test_legacy_pickle_cache_is_never_loaded(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    legacy = tmp_path / "embeddings_test_v2.pkl"
    legacy.write_bytes(pickle.dumps(_PicklePayload(marker)))

    ToolRanker(
        model_name="test",
        cache_dir=tmp_path,
        enable_cache_persistence=True,
    )

    assert not marker.exists()


def test_skill_embedding_cache_round_trips_as_json(tmp_path: Path) -> None:
    ranker = SkillRanker(cache_dir=tmp_path, enable_cache=True)
    ranker._embedding_cache = {"skill-one": [0.1, 0.2]}
    ranker._save_cache()

    restored = SkillRanker(cache_dir=tmp_path, enable_cache=True)

    assert restored._embedding_cache == {"skill-one": [0.1, 0.2]}
    assert (tmp_path / "skill_embeddings_v2.json").exists()
