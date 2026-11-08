import asyncio
import hashlib
import json
import uuid
from collections import defaultdict

from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert

from memory_mcp.config import Settings
from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.schemas import (
    AddInput,
    AddResult,
    EditProfileInput,
    ExtractedMemoryView,
    ProfileView,
    RawMemoryView,
)
from memory_mcp.storage.db import Database
from memory_mcp.storage.models import (
    EmbeddingSpace,
    Entity,
    EntityMemory,
    Job,
    Memory,
    MemorySource,
    Relation,
    UserProfile,
)
from memory_mcp.storage.text import query_text

SIMPLE = literal_column("'simple'::regconfig")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def profile_view(profile: UserProfile | None, tag: str) -> ProfileView:
    if profile is None:
        return ProfileView(tag=tag)
    return ProfileView(
        tag=tag,
        content=profile.content,
        version=profile.version,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


class Repository:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    async def check_ready(self):
        async with self.db.sessions() as session:
            await session.execute(text("SELECT version_num FROM alembic_version"))

    async def add(self, request: AddInput) -> AddResult:
        try:
            request_hash = digest(
                json.dumps(
                    {"content": request.content, "metadata": request.metadata},
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as error:
            raise MemoryError(
                "INVALID_METADATA", "Metadata must contain valid JSON values."
            ) from error
        raw_id = f"raw_{uuid.uuid4().hex}"
        async with self.db.transaction() as session:
            inserted = await session.scalar(
                insert(Memory.__table__)
                .values(
                    id=raw_id,
                    tag=request.tag,
                    type="raw",
                    content=request.content,
                    content_hash=digest(request.content),
                    metadata=request.metadata,
                    idempotency_key=request.idempotency_key,
                    request_hash=request_hash,
                )
                .on_conflict_do_nothing(index_elements=["tag", "idempotency_key"])
                .returning(Memory.id)
            )
            if inserted:
                session.add(Job(raw_id=raw_id, tag=request.tag, checkpoint={}))
                return AddResult(raw_id=raw_id, processing_status="pending")
            existing = (
                await session.execute(
                    select(Memory.id, Memory.request_hash, Job.status)
                    .join(Job, Job.raw_id == Memory.id)
                    .where(
                        Memory.tag == request.tag, Memory.idempotency_key == request.idempotency_key
                    )
                )
            ).one()
            if existing.request_hash != request_hash:
                raise MemoryError(
                    "IDEMPOTENCY_CONFLICT", "This key was already used for a different request."
                )
            return AddResult(raw_id=existing.id, processing_status=existing.status)

    async def get_memory(self, tag: str, identifier: str):
        async with self.db.sessions() as session:
            memory = await session.scalar(
                select(Memory).where(Memory.tag == tag, Memory.id == identifier)
            )
            if memory is None:
                raise MemoryError("MEMORY_NOT_FOUND", "Memory was not found in this tag.")
            common = dict(
                id=memory.id,
                tag=tag,
                content=memory.content,
                metadata=memory.metadata_,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
            )
            if memory.type == "raw":
                job = await session.get(Job, identifier)
                extracted = list(
                    await session.scalars(
                        select(MemorySource.memory_id)
                        .where(MemorySource.tag == tag, MemorySource.raw_id == identifier)
                        .order_by(MemorySource.memory_id)
                    )
                )
                return RawMemoryView(
                    **common, raw_ids=[], processing_status=job.status, extracted_ids=extracted
                )
            raw_ids = list(
                await session.scalars(
                    select(MemorySource.raw_id)
                    .where(MemorySource.tag == tag, MemorySource.memory_id == identifier)
                    .order_by(MemorySource.raw_id)
                )
            )
            return ExtractedMemoryView(**common, raw_ids=raw_ids)

    async def get_profile(self, tag: str) -> ProfileView:
        async with self.db.sessions() as session:
            return profile_view(await session.get(UserProfile, tag), tag)

    async def edit_profile(self, request: EditProfileInput) -> ProfileView:
        async with self.db.transaction() as session:
            return await self.put_profile(session, request)

    async def put_profile(self, session, request: EditProfileInput) -> ProfileView:
        if request.expected_version == 0:
            statement = insert(UserProfile).values(
                tag=request.tag, content=request.content, version=1
            )
            statement = statement.on_conflict_do_nothing(index_elements=["tag"]).returning(
                UserProfile
            )
        else:
            statement = (
                update(UserProfile)
                .where(
                    UserProfile.tag == request.tag, UserProfile.version == request.expected_version
                )
                .values(
                    content=request.content, version=UserProfile.version + 1, updated_at=func.now()
                )
                .returning(UserProfile)
            )
        profile = await session.scalar(statement)
        if profile is None:
            current = profile_view(
                await session.get(UserProfile, request.tag, populate_existing=True), request.tag
            )
            raise MemoryError(
                "PROFILE_VERSION_CONFLICT",
                "Reapply the intended edit to the current profile before retrying.",
                expected_version=request.expected_version,
                current_version=current.version,
                current_content=current.content,
            )
        return profile_view(profile, request.tag)

    async def ensure_space(self, session, *, create=False):
        cfg = self.settings
        if create:
            await session.execute(
                insert(EmbeddingSpace)
                .values(
                    id=1,
                    fingerprint=cfg.embedding_fingerprint,
                    model=cfg.embedding_model,
                    dimensions=cfg.embedding_dimensions,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
        existing = await session.get(EmbeddingSpace, 1)
        if existing and existing.fingerprint != cfg.embedding_fingerprint:
            raise MemoryError(
                "EMBEDDING_CONFIG_MISMATCH",
                "This database uses a different embedding configuration. Restore its configuration or rebuild its vector index before switching models.",
            )

    async def has_memories(self, tag: str) -> bool:
        async with self.db.sessions() as session:
            return (
                await session.scalar(
                    select(Memory.id).where(Memory.tag == tag, Memory.type == "extracted").limit(1)
                )
                is not None
            )

    async def candidates(self, tag: str, query: str, vector: list[float], *, limit: int):
        cfg = self.settings
        distance = Memory.embedding.cosine_distance(vector)
        conditions = (Memory.tag == tag, Memory.type == "extracted")
        keywords = await asyncio.to_thread(query_text, query)
        async with self.db.sessions() as session:
            await self.ensure_space(session)
            semantic = list(
                await session.scalars(
                    select(Memory.id).where(*conditions).order_by(distance, Memory.id).limit(limit)
                )
            )
            tsquery = func.websearch_to_tsquery(SIMPLE, keywords)
            keyword = list(
                await session.scalars(
                    select(Memory.id)
                    .where(*conditions, Memory.search_vector.op("@@")(tsquery))
                    .order_by(func.ts_rank_cd(Memory.search_vector, tsquery).desc(), Memory.id)
                    .limit(limit)
                )
            )
            entity_ids = list(
                await session.scalars(
                    select(Entity.id)
                    .where(Entity.tag == tag)
                    .order_by(Entity.embedding.cosine_distance(vector), Entity.id)
                    .limit(cfg.entity_top_k)
                )
            )
            entity_memories: list[str] = []
            if entity_ids:
                # Cap expansion per node before deduplication, then order candidates
                # by the relevance of their memory text, not by node similarity.
                linked = (
                    select(
                        Memory.id.label("id"),
                        distance.label("distance"),
                        func.row_number()
                        .over(partition_by=EntityMemory.entity_id, order_by=[distance, Memory.id])
                        .label("position"),
                    )
                    .join(
                        EntityMemory,
                        (EntityMemory.memory_id == Memory.id) & (EntityMemory.tag == Memory.tag),
                    )
                    .where(*conditions, EntityMemory.entity_id.in_(entity_ids))
                    .subquery()
                )
                ranked = (
                    await session.execute(
                        select(linked.c.id, func.min(linked.c.distance).label("distance"))
                        .where(linked.c.position <= cfg.entity_memory_limit)
                        .group_by(linked.c.id)
                        .order_by(func.min(linked.c.distance), linked.c.id)
                        .limit(limit)
                    )
                ).all()
                entity_memories = [row.id for row in ranked]
            return semantic, keyword, entity_memories

    async def fetch_hits(self, tag: str, identifiers: list[str]):
        async with self.db.sessions() as session:
            rows = (
                await session.execute(
                    select(Memory.id, Memory.content).where(
                        Memory.tag == tag, Memory.type == "extracted", Memory.id.in_(identifiers)
                    )
                )
            ).all()
            sources: dict[str, list[str]] = defaultdict(list)
            for memory_id, raw_id in (
                await session.execute(
                    select(MemorySource.memory_id, MemorySource.raw_id)
                    .where(MemorySource.tag == tag, MemorySource.memory_id.in_(identifiers))
                    .order_by(MemorySource.raw_id)
                )
            ).all():
                sources[memory_id].append(raw_id)
            return {
                row.id: {"id": row.id, "content": row.content, "raw_ids": sources[row.id]}
                for row in rows
            }

    async def entity_candidates(self, tag: str, entities, vectors):
        candidates: dict[str, list[dict]] = {}
        async with self.db.sessions() as session:
            await self.ensure_space(session)
            for entity, vector in zip(entities, vectors, strict=True):
                rows = list(
                    await session.scalars(
                        select(Entity)
                        .where(Entity.tag == tag)
                        .order_by(Entity.embedding.cosine_distance(vector), Entity.id)
                        .limit(5)
                    )
                )
                candidates[entity.key] = [
                    {"id": row.id, "name": row.name, "type": row.type, "summary": row.summary}
                    for row in rows
                ]
            ids = {item["id"] for items in candidates.values() for item in items}
            edges = (
                list(
                    await session.scalars(
                        select(Relation)
                        .where(
                            Relation.tag == tag,
                            Relation.source_id.in_(ids),
                            Relation.target_id.in_(ids),
                        )
                        .order_by(Relation.id)
                        .limit(200)
                    )
                )
                if ids
                else []
            )
            return candidates, [
                {
                    "id": edge.id,
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "type": edge.type,
                    "status": edge.status,
                }
                for edge in edges
            ]
