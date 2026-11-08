import pytest

from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.profile import apply_profile_patch
from memory_mcp.domain.ranking import rrf
from memory_mcp.domain.schemas import Extraction, ProfilePatch
from memory_mcp.storage.text import query_text, tokenize


def test_rrf_deduplicates_within_route_and_promotes_overlap():
    scores = dict(rrf(["a", "a", "b"], ["b", "c"]))
    assert list(scores) == ["b", "a", "c"]
    assert scores["a"] == 1 / 61
    assert scores["b"] == 1 / 62 + 1 / 61


def test_profile_patch_preserves_concurrent_edit_and_adds_experience():
    base = "## 偏好\n- 喜欢详细回答。"
    current = "## 偏好\n- 默认简洁回答。"
    patch = ProfilePatch(
        replacements=[{"before": "- 喜欢详细回答。", "after": "- 喜欢较长的回答。"}],
        append="## 经历\n- 使用过 PostgreSQL。",
    )
    result = apply_profile_patch(base, patch, current=current)
    assert "默认简洁回答" in result
    assert "使用过 PostgreSQL" in result
    assert "较长的回答" not in result
    assert apply_profile_patch(base, patch, current=result) == result


def test_profile_patch_rejects_ambiguous_and_overlapping_edits():
    with pytest.raises(MemoryError, match="exactly once"):
        apply_profile_patch(
            "x x", ProfilePatch(replacements=[{"before": "x", "after": "y"}], append="")
        )
    with pytest.raises(MemoryError, match="overlap"):
        apply_profile_patch(
            "abcd",
            ProfilePatch(
                replacements=[{"before": "ab", "after": "z"}, {"before": "bc", "after": "z"}],
                append="",
            ),
        )


def test_concurrent_initial_profile_is_preserved():
    assert (
        apply_profile_patch(
            "", ProfilePatch(replacements=[], append="Old preference"), current="New preference"
        )
        == "New preference"
    )


def test_extraction_rejects_unsupported_references():
    with pytest.raises(ValueError, match="memory index"):
        Extraction.model_validate(
            {
                "memories": [],
                "entities": [
                    {
                        "key": "a",
                        "name": "A",
                        "type": "project",
                        "summary": "",
                        "memory_indices": [0],
                    }
                ],
                "relations": [],
                "profile_updates": [],
            }
        )


def test_mixed_language_tokenization_preserves_useful_terms():
    tokens = tokenize("数据库使用 PostgreSQL 和 userProfile")
    assert "数据库" in tokens
    assert "postgresql" in tokens
    assert "userprofile" in tokens
    assert "profile" in tokens
    assert '"postgresql"' in query_text("PostgreSQL 数据库")
