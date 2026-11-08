from datetime import UTC, datetime

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert

from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.schemas import EditProfileInput
from memory_mcp.storage.models import (
    Entity,
    EntityMemory,
    Memory,
    MemorySource,
    Relation,
    RelationMemory,
)
from memory_mcp.storage.repository import SIMPLE, Repository, digest
from memory_mcp.storage.tasks import TaskQueue


class IngestWriter:
    def __init__(self, repository: Repository, tasks: TaskQueue):
        self.repository, self.tasks = repository, tasks

    async def commit(
        self,
        job,
        raw,
        *,
        extraction,
        memory_vectors,
        entity_vectors,
        entity_ids,
        new_entity_keys,
        relation_types,
        memory_ids,
        relation_ids,
        tokenized,
        profile_edit: EditProfileInput | None,
    ):
        async with self.repository.db.transaction() as session:
            current_job = await self.tasks.owned(session, job)
            if memory_vectors or entity_vectors:
                await self.repository.ensure_space(session, create=True)
            persisted_ids: list[str] = []
            for index, memory in enumerate(extraction.memories):
                statement = insert(Memory.__table__).values(
                    id=memory_ids[index],
                    tag=raw.tag,
                    type="extracted",
                    content=memory.content,
                    content_hash=digest(memory.content),
                    metadata={},
                    embedding=memory_vectors[index],
                    search_vector=func.to_tsvector(SIMPLE, tokenized[index]),
                )
                statement = statement.on_conflict_do_update(
                    index_elements=["tag", "content_hash"],
                    index_where=text("type = 'extracted'"),
                    set_={"updated_at": func.now()},
                ).returning(Memory.id)
                identifier = await session.scalar(statement)
                persisted_ids.append(identifier)
                await session.execute(
                    insert(MemorySource)
                    .values(tag=raw.tag, memory_id=identifier, raw_id=raw.id)
                    .on_conflict_do_nothing()
                )

            for index, entity in enumerate(extraction.entities):
                identifier = entity_ids[entity.key]
                if entity.key in new_entity_keys:
                    await session.execute(
                        insert(Entity)
                        .values(
                            id=identifier,
                            tag=raw.tag,
                            name=entity.name,
                            type=entity.type,
                            summary=entity.summary,
                            embedding=entity_vectors[index],
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                elif (
                    await session.scalar(
                        select(Entity.id).where(Entity.id == identifier, Entity.tag == raw.tag)
                    )
                    is None
                ):
                    raise MemoryError("INVALID_RESOLUTION", "Existing entity is not in this tag.")
                indices = set(entity.memory_indices)
                for relation in extraction.relations:
                    if entity.key in (relation.source_key, relation.target_key):
                        indices.update(relation.memory_indices)
                for memory_index in indices:
                    await session.execute(
                        insert(EntityMemory)
                        .values(
                            tag=raw.tag, entity_id=identifier, memory_id=persisted_ids[memory_index]
                        )
                        .on_conflict_do_nothing()
                    )

            for index, relation in enumerate(extraction.relations):
                statement = insert(Relation).values(
                    id=relation_ids[index],
                    tag=raw.tag,
                    source_id=entity_ids[relation.source_key],
                    target_id=entity_ids[relation.target_key],
                    type=relation_types[index],
                    status=relation.status,
                    source_created_at=raw.created_at,
                )
                incoming = statement.excluded
                statement = statement.on_conflict_do_update(
                    index_elements=["tag", "source_id", "target_id", "type"],
                    set_={
                        "status": case(
                            (
                                incoming.source_created_at >= Relation.source_created_at,
                                incoming.status,
                            ),
                            else_=Relation.status,
                        ),
                        "source_created_at": func.greatest(
                            incoming.source_created_at, Relation.source_created_at
                        ),
                        "updated_at": func.now(),
                    },
                ).returning(Relation.id)
                identifier = await session.scalar(statement)
                for memory_index in set(relation.memory_indices):
                    await session.execute(
                        insert(RelationMemory)
                        .values(
                            tag=raw.tag,
                            relation_id=identifier,
                            memory_id=persisted_ids[memory_index],
                        )
                        .on_conflict_do_nothing()
                    )

            if profile_edit is not None:
                await self.repository.put_profile(session, profile_edit)
            current_job.status = "completed"
            current_job.lease_token, current_job.lease_until, current_job.last_error = (
                None,
                None,
                None,
            )
            current_job.checkpoint = {}
            current_job.updated_at = datetime.now(UTC)
