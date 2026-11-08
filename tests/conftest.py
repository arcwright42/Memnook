import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from memory_mcp.config import Settings
from memory_mcp.storage.db import Database
from memory_mcp.storage.repository import Repository


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        llm_model="test-chat",
        embedding_model="test-embedding",
        embedding_dimensions=3,
        embedding_base_url="http://test.invalid/v1",
        worker_retry_seconds=0,
        worker_lease_seconds=6,
    )


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("MEMORY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MEMORY_TEST_DATABASE_URL to run PostgreSQL integration tests")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Integration tests require a disposable database whose name ends in _test")
    environment = dict(os.environ, MEMORY_DATABASE_URL=url)
    result = subprocess.run(
        [sys.executable, "-m", "memory_mcp.interfaces.cli", "migrate"],
        env=environment,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(
            "Test database migration failed. Check the disposable PostgreSQL server and pgvector; "
            "run memory migrate against that test database for diagnostics.",
            pytrace=False,
        )
    return url


@pytest.fixture
async def repository(database_url, settings):
    settings.database_url = database_url
    db = Database(database_url)
    async with db.transaction() as session:
        await session.execute(
            text(
                "TRUNCATE embedding_space, jobs, relation_memories, relations, entity_memories, "
                "entities, user_profiles, memory_sources, memories CASCADE"
            )
        )
    try:
        yield Repository(db, settings)
    finally:
        await db.close()
