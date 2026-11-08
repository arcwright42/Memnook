import json

import httpx
import pytest

from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.schemas import Extraction
from memory_mcp.providers.http import HTTPModels


async def test_embeddings_are_reordered_and_query_prefix_is_separate(settings):
    settings.embedding_query_prefix = "query: "

    def handle(request):
        assert json.loads(request.content)["input"] == ["query: one", "query: two"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 1, 0]},
                    {"index": 0, "embedding": [1, 0, 0]},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await HTTPModels(settings, client).embed(["one", "two"], kind="query")
    assert result == [[1, 0, 0], [0, 1, 0]]


@pytest.mark.parametrize(
    "records",
    [
        [{"index": 0, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [0, 0, 0]}],
        [{"index": 0, "embedding": ["NaN", 0, 0]}],
        [{"index": 1, "embedding": [1, 0, 0]}],
    ],
)
async def test_invalid_embeddings_are_rejected(settings, records):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": records}))
    ) as client:
        with pytest.raises(MemoryError) as error:
            await HTTPModels(settings, client).embed(["one"])
    assert error.value.code == "INVALID_EMBEDDING"


async def test_structured_output_retries_invalid_json_once(settings):
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["response_format"]["json_schema"]["strict"] is True
        content = (
            "invalid"
            if calls == 1
            else json.dumps(
                {"memories": [], "entities": [], "relations": [], "profile_updates": []}
            )
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await HTTPModels(settings, client).generate_structured([], Extraction)
    assert calls == 2
    assert not result.memories


async def test_http_errors_do_not_leak_response_content(settings):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="private API credential"))
    ) as client:
        with pytest.raises(MemoryError) as error:
            await HTTPModels(settings, client).embed(["one"])
    assert error.value.code == "PROVIDER_HTTP_401"
    assert "private" not in json.dumps(error.value.as_dict())


async def test_reranker_requires_a_complete_permutation(settings):
    settings.reranker_base_url, settings.reranker_model = "http://test.invalid", "rerank"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})
        )
    ) as client:
        with pytest.raises(MemoryError) as error:
            await HTTPModels(settings, client).rerank("query", ["one", "two"])
    assert error.value.code == "INVALID_RERANKING"
