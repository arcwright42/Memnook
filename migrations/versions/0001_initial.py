"""Initial PostgreSQL schema. Kept independent of mutable ORM models."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE memories (
    id varchar(64) PRIMARY KEY,
    tag varchar(128) NOT NULL,
    type varchar(16) NOT NULL CHECK (type IN ('raw', 'extracted')),
    content text NOT NULL,
    content_hash varchar(64) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    idempotency_key varchar(512),
    request_hash varchar(64),
    embedding vector,
    search_vector tsvector,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tag, id),
    UNIQUE (tag, idempotency_key),
    CHECK (type = 'raw' OR (embedding IS NOT NULL AND search_vector IS NOT NULL))
);
CREATE INDEX memories_tag_type ON memories (tag, type);
CREATE UNIQUE INDEX memories_extracted_hash ON memories (tag, content_hash) WHERE type = 'extracted';
CREATE INDEX memories_fts ON memories USING gin (search_vector);
CREATE TABLE memory_sources (
    tag varchar(128) NOT NULL,
    memory_id varchar(64) NOT NULL,
    raw_id varchar(64) NOT NULL,
    PRIMARY KEY (tag, memory_id, raw_id),
    FOREIGN KEY (tag, memory_id) REFERENCES memories (tag, id) ON DELETE CASCADE,
    FOREIGN KEY (tag, raw_id) REFERENCES memories (tag, id) ON DELETE CASCADE,
    CHECK (memory_id <> raw_id)
);
CREATE INDEX memory_sources_raw ON memory_sources (tag, raw_id);
CREATE TABLE user_profiles (
    tag varchar(128) PRIMARY KEY,
    content text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE entities (
    id varchar(64) PRIMARY KEY,
    tag varchar(128) NOT NULL,
    name varchar(300) NOT NULL,
    type varchar(80) NOT NULL,
    summary text NOT NULL,
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tag, id)
);
CREATE INDEX entities_tag ON entities (tag);
CREATE TABLE entity_memories (
    tag varchar(128) NOT NULL,
    entity_id varchar(64) NOT NULL,
    memory_id varchar(64) NOT NULL,
    PRIMARY KEY (tag, entity_id, memory_id),
    FOREIGN KEY (tag, entity_id) REFERENCES entities (tag, id) ON DELETE CASCADE,
    FOREIGN KEY (tag, memory_id) REFERENCES memories (tag, id) ON DELETE CASCADE
);
CREATE TABLE relations (
    id varchar(64) PRIMARY KEY,
    tag varchar(128) NOT NULL,
    source_id varchar(64) NOT NULL,
    target_id varchar(64) NOT NULL,
    type varchar(80) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('active', 'inactive')),
    source_created_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tag, id),
    UNIQUE (tag, source_id, target_id, type),
    FOREIGN KEY (tag, source_id) REFERENCES entities (tag, id),
    FOREIGN KEY (tag, target_id) REFERENCES entities (tag, id)
);
CREATE INDEX relations_target ON relations (tag, target_id);
CREATE TABLE relation_memories (
    tag varchar(128) NOT NULL,
    relation_id varchar(64) NOT NULL,
    memory_id varchar(64) NOT NULL,
    PRIMARY KEY (tag, relation_id, memory_id),
    FOREIGN KEY (tag, relation_id) REFERENCES relations (tag, id) ON DELETE CASCADE,
    FOREIGN KEY (tag, memory_id) REFERENCES memories (tag, id) ON DELETE CASCADE
);
CREATE TABLE jobs (
    raw_id varchar(64) PRIMARY KEY,
    tag varchar(128) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token varchar(64),
    last_error varchar(120),
    checkpoint jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tag, raw_id) REFERENCES memories (tag, id) ON DELETE CASCADE
);
CREATE INDEX jobs_ready ON jobs (status, available_at, created_at);
CREATE INDEX jobs_tag ON jobs (tag, status);
CREATE TABLE embedding_space (
    id integer PRIMARY KEY CHECK (id = 1),
    fingerprint varchar(64) NOT NULL,
    model text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0)
);
"""


def upgrade():
    for statement in SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for table in (
        "embedding_space",
        "jobs",
        "relation_memories",
        "relations",
        "entity_memories",
        "entities",
        "user_profiles",
        "memory_sources",
        "memories",
    ):
        op.drop_table(table)
