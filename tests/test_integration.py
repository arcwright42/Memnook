import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from memory_mcp.application.extraction import Extractor
from memory_mcp.application.service import MemoryService
from memory_mcp.application.worker import Worker
from memory_mcp.domain.errors import LeaseLost, MemoryError
from memory_mcp.domain.schemas import AddInput, EditProfileInput, ProfilePatch, SearchInput
from memory_mcp.storage.models import (
    Entity,
    EntityMemory,
    Job,
    Memory,
    MemorySource,
    Relation,
    RelationMemory,
)
from memory_mcp.storage.tasks import TaskQueue
from tests.fakes import FakeModels

pytestmark = pytest.mark.integration


def worker(repository, settings, models):
    return Worker(repository, Extractor(repository, models, models), settings)


async def test_add_is_durable_idempotent_and_tag_isolated(repository, settings):
    request = AddInput(tag="a", content="Atlas uses PostgreSQL", idempotency_key="session:1")
    first, second = await asyncio.gather(repository.add(request), repository.add(request))
    assert first.raw_id == second.raw_id
    assert (await repository.get_memory("a", first.raw_id)).processing_status == "pending"
    with pytest.raises(MemoryError) as error:
        await repository.add(request.model_copy(update={"content": "different"}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    other = await repository.add(request.model_copy(update={"tag": "b"}))
    assert other.raw_id != first.raw_id
    with pytest.raises(MemoryError) as error:
        await repository.get_memory("b", first.raw_id)
    assert error.value.code == "MEMORY_NOT_FOUND"
    async with repository.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 2


async def test_profile_create_replace_clear_and_concurrent_cas(repository):
    empty = await repository.get_profile("a")
    assert empty.version == 0 and empty.created_at is None
    results = await asyncio.gather(
        *[
            repository.edit_profile(EditProfileInput(tag="a", expected_version=0, content=value))
            for value in ("first", "second")
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    conflict = next(item for item in results if isinstance(item, MemoryError))
    assert conflict.code == "PROFILE_VERSION_CONFLICT"
    assert conflict.details["current_version"] == 1
    current = await repository.get_profile("a")
    cleared = await repository.edit_profile(
        EditProfileInput(tag="a", expected_version=current.version, content="")
    )
    assert cleared.content == "" and cleared.version == 2
    assert (await repository.get_profile("b")).version == 0


async def test_worker_builds_memory_graph_and_reuses_exact_facts(repository, settings):
    models = FakeModels()
    first = await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    assert await worker(repository, settings, models).run_once()
    raw = await repository.get_memory("a", first.raw_id)
    assert raw.processing_status == "completed" and len(raw.extracted_ids) == 1
    memory = await repository.get_memory("a", raw.extracted_ids[0])
    assert memory.raw_ids == [first.raw_id]
    second = await repository.add(
        AddInput(tag="a", content="再次确认 Atlas 的业务数据库是 PostgreSQL。")
    )
    assert await worker(repository, settings, models).run_once()
    memory = await repository.get_memory("a", memory.id)
    assert set(memory.raw_ids) == {first.raw_id, second.raw_id}
    async with repository.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Entity)) == 2
        assert await session.scalar(select(func.count()).select_from(Relation)) == 1
        assert await session.scalar(select(func.count()).select_from(RelationMemory)) == 1
    result = await MemoryService(repository, models, models, settings).search(
        SearchInput(tag="a", query="Atlas 数据库")
    )
    assert result.results[0].id == memory.id
    assert (
        await MemoryService(repository, models, models, settings).search(
            SearchInput(tag="b", query="Atlas")
        )
    ).results == []


async def test_explicit_relation_end_keeps_change_evidence(repository, settings):
    models = FakeModels()
    await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    models.extraction = copy.deepcopy(models.extraction)
    models.extraction["memories"] = [{"content": "Atlas 已停止使用 PostgreSQL。"}]
    models.extraction["relations"][0]["status"] = "inactive"
    await repository.add(AddInput(tag="a", content="Atlas 已停止使用 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    async with repository.db.sessions() as session:
        edge = await session.scalar(select(Relation))
        assert edge.status == "inactive"
        assert await session.scalar(select(func.count()).select_from(RelationMemory)) == 2


async def test_failed_embedding_reuses_persisted_extraction_after_worker_restart(
    repository, settings
):
    models = FakeModels()
    models.embedding_failures = 1
    added = await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    assert (await repository.get_memory("a", added.raw_id)).processing_status == "pending"
    assert models.extraction_calls == 1
    restarted_models = FakeModels()
    await worker(repository, settings, restarted_models).run_once()
    assert restarted_models.extraction_calls == 0
    assert (await repository.get_memory("a", added.raw_id)).processing_status == "completed"


async def test_empty_extraction_completes_without_creating_profile(repository, settings):
    models = FakeModels({"memories": [], "entities": [], "relations": [], "profile_updates": []})
    added = await repository.add(AddInput(tag="a", content="你好"))
    await worker(repository, settings, models).run_once()
    raw = await repository.get_memory("a", added.raw_id)
    assert raw.processing_status == "completed" and raw.extracted_ids == []
    assert (await repository.get_profile("a")).version == 0


async def test_exhausted_task_can_be_retried_without_losing_raw(repository, settings):
    settings.worker_max_attempts = 1
    models = FakeModels()
    models.embedding_failures = 1
    added = await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    raw = await repository.get_memory("a", added.raw_id)
    assert raw.processing_status == "failed" and raw.content == "Atlas 使用 PostgreSQL。"
    await TaskQueue(repository.db, settings).retry("a", added.raw_id)
    await worker(repository, settings, models).run_once()
    assert (await repository.get_memory("a", added.raw_id)).processing_status == "completed"
    assert models.extraction_calls == 1


async def test_profile_worker_rebases_over_manual_edit(repository, settings):
    base = await repository.edit_profile(
        EditProfileInput(tag="a", expected_version=0, content="- 喜欢详细回答。")
    )
    models = FakeModels()
    models.extraction["profile_updates"] = ["用户使用过 PostgreSQL。"]
    models.patch = ProfilePatch(
        replacements=[{"before": "- 喜欢详细回答。", "after": "- 喜欢长回答。"}],
        append="- 使用过 PostgreSQL。",
    )

    async def manual_edit():
        await repository.edit_profile(
            EditProfileInput(tag="a", expected_version=base.version, content="- 默认简洁回答。")
        )

    models.profile_hook = manual_edit
    added = await repository.add(AddInput(tag="a", content="用户使用过 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    profile = await repository.get_profile("a")
    assert profile.version == 3
    assert "默认简洁回答" in profile.content and "使用过 PostgreSQL" in profile.content
    assert "长回答" not in profile.content
    assert (await repository.get_memory("a", added.raw_id)).processing_status == "completed"


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_commit(repository, settings):
    await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    queue = TaskQueue(repository.db, settings)
    old = await queue.claim()
    async with repository.db.transaction() as session:
        await session.execute(
            update(Job)
            .where(Job.raw_id == old.raw_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    new = await queue.claim()
    assert new.lease_token != old.lease_token
    with pytest.raises(LeaseLost):
        await queue.checkpoint(old, {"extraction": {}})
    await worker(repository, settings, FakeModels()).process(new)
    assert (await repository.get_memory("a", new.raw_id)).processing_status == "completed"


async def test_claims_serialize_tags_but_allow_other_tags(repository, settings):
    for tag in ("a", "a", "b"):
        await repository.add(AddInput(tag=tag, content="raw"))
    queue = TaskQueue(repository.db, settings)
    claims = await asyncio.gather(queue.claim(), queue.claim(), queue.claim())
    assert sorted(item.tag for item in claims if item) == ["a", "b"]


async def test_retrying_older_job_waits_for_current_tag_owner(repository, settings):
    old = await repository.add(AddInput(tag="a", content="older record"))
    async with repository.db.transaction() as session:
        await session.execute(update(Job).where(Job.raw_id == old.raw_id).values(status="failed"))
    new = await repository.add(AddInput(tag="a", content="newer record"))
    queue = TaskQueue(repository.db, settings)
    current = await queue.claim()
    assert current.raw_id == new.raw_id
    await queue.retry("a", old.raw_id)
    assert await queue.claim() is None
    await queue.fail(current, "TEST_FAILURE")
    assert (await queue.claim()).raw_id == old.raw_id


async def test_exact_fact_upsert_works_with_generic_prepared_plans(repository, settings):
    async with repository.db.engine.connect() as connection:
        raw_connection = await connection.get_raw_connection()
        raw_connection.driver_connection.prepare_threshold = 0
        await connection.execute(text("SET plan_cache_mode = force_generic_plan"))
        await connection.commit()
    for content in ("first confirmation", "second confirmation"):
        added = await repository.add(AddInput(tag="a", content=content))
        await worker(repository, settings, FakeModels()).run_once()
        assert (await repository.get_memory("a", added.raw_id)).processing_status == "completed"
    async with repository.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MemorySource)) == 2


async def test_database_rejects_cross_tag_graph_links(repository, settings):
    for tag in ("a", "b"):
        await repository.add(AddInput(tag=tag, content="Atlas 使用 PostgreSQL。"))
        await worker(repository, settings, FakeModels()).run_once()
    async with repository.db.sessions() as session:
        entity = await session.scalar(select(Entity).where(Entity.tag == "a"))
        memory = await session.scalar(
            select(Memory).where(Memory.tag == "b", Memory.type == "extracted")
        )
    with pytest.raises(IntegrityError):
        async with repository.db.transaction() as session:
            session.add(EntityMemory(tag="a", entity_id=entity.id, memory_id=memory.id))


async def test_embedding_space_change_is_detected(repository, settings):
    models = FakeModels()
    await repository.add(AddInput(tag="a", content="Atlas 使用 PostgreSQL。"))
    await worker(repository, settings, models).run_once()
    settings.embedding_model = "different-model"
    with pytest.raises(MemoryError) as error:
        await MemoryService(repository, models, models, settings).search(
            SearchInput(tag="a", query="Atlas")
        )
    assert error.value.code == "EMBEDDING_CONFIG_MISMATCH"


async def test_entity_route_adds_candidates_missing_from_memory_route(repository, settings):
    settings.candidate_limit = 2
    settings.entity_top_k = 1
    raw = await repository.add(AddInput(tag="a", content="source"))
    async with repository.db.transaction() as session:
        await repository.ensure_space(session, create=True)
        for index, vector in enumerate(([1, 0, 0], [1, 0.1, 0], [0, 1, 0])):
            identifier = f"mem_{index}"
            session.add(
                Memory(
                    id=identifier,
                    tag="a",
                    type="extracted",
                    content=f"fact {index}",
                    content_hash=str(index),
                    metadata_={},
                    embedding=vector,
                    search_vector="",
                )
            )
            await session.flush()
            session.add(MemorySource(tag="a", memory_id=identifier, raw_id=raw.raw_id))
        session.add(
            Entity(
                id="ent_1", tag="a", name="Atlas", type="project", summary="", embedding=[1, 0, 0]
            )
        )
        await session.flush()
        session.add(EntityMemory(tag="a", entity_id="ent_1", memory_id="mem_2"))

    class Models(FakeModels):
        async def embed(self, texts, *, kind="document"):
            return [[1, 0, 0] for _ in texts]

    models = Models()
    service = MemoryService(repository, models, models, settings)
    result = await service.search(SearchInput(tag="a", query="Atlas", top_k=2))
    assert {hit.id for hit in result.results} == {"mem_0", "mem_2"}
    models.rerank_error = TimeoutError()
    fallback = await service.search(SearchInput(tag="a", query="Atlas", top_k=2, rerank=True))
    assert fallback.meta.rerank_applied is False
    assert fallback.meta.score_type == "rrf" and fallback.meta.rerank_error == "RERANKER_TIMEOUT"
    assert [(hit.id, hit.score) for hit in result.results] == [
        (hit.id, hit.score) for hit in fallback.results
    ]
    models.rerank_error = None
    reranked = await service.search(SearchInput(tag="a", query="Atlas", top_k=2, rerank=True))
    assert reranked.meta.rerank_applied and reranked.meta.score_type == "reranker"
    assert [hit.score for hit in reranked.results] == sorted(
        [hit.score for hit in reranked.results], reverse=True
    )


async def test_keyword_route_can_recall_chinese_outside_vector_candidates(repository, settings):
    settings.candidate_limit = 1
    raw = await repository.add(AddInput(tag="a", content="source"))
    async with repository.db.transaction() as session:
        session.add_all(
            [
                Memory(
                    id="mem_vec",
                    tag="a",
                    type="extracted",
                    content="unrelated",
                    content_hash="a",
                    metadata_={},
                    embedding=[1, 0, 0],
                    search_vector="",
                ),
                Memory(
                    id="mem_kw",
                    tag="a",
                    type="extracted",
                    content="数据库",
                    content_hash="b",
                    metadata_={},
                    embedding=[0, 1, 0],
                    search_vector="'数据库':1",
                ),
            ]
        )
    semantic, keyword, _ = await repository.candidates("a", "数据库", [1, 0, 0], limit=1)
    assert semantic == ["mem_vec"] and keyword == ["mem_kw"]
    assert raw.raw_id
