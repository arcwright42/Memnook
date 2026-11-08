from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Memory(Timestamps, Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(512))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[Any | None] = mapped_column(Vector())
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)


class MemorySource(Base):
    __tablename__ = "memory_sources"
    tag: Mapped[str] = mapped_column(String(128), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class UserProfile(Timestamps, Base):
    __tablename__ = "user_profiles"
    tag: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)


class Entity(Timestamps, Base):
    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector())


class EntityMemory(Base):
    __tablename__ = "entity_memories"
    tag: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class Relation(Timestamps, Base):
    __tablename__ = "relations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RelationMemory(Base):
    __tablename__ = "relation_memories"
    tag: Mapped[str] = mapped_column(String(128), primary_key=True)
    relation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class Job(Timestamps, Base):
    __tablename__ = "jobs"
    raw_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(120))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EmbeddingSpace(Base):
    __tablename__ = "embedding_space"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
