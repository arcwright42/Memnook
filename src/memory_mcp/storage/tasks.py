import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import aliased

from memory_mcp.config import Settings
from memory_mcp.domain.errors import LeaseLost, MemoryError
from memory_mcp.storage.db import Database
from memory_mcp.storage.models import Job


class TaskQueue:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    async def claim(self) -> Job | None:
        now = datetime.now(UTC)
        async with self.db.transaction() as session:
            previous = aliased(Job)
            earlier = (
                select(previous.raw_id)
                .where(
                    previous.tag == Job.tag,
                    previous.status.in_(["pending", "processing"]),
                    or_(
                        previous.created_at < Job.created_at,
                        and_(previous.created_at == Job.created_at, previous.raw_id < Job.raw_id),
                    ),
                )
                .exists()
            )
            busy = (
                select(previous.raw_id)
                .where(
                    previous.tag == Job.tag,
                    previous.raw_id != Job.raw_id,
                    previous.status == "processing",
                    previous.lease_until > now,
                )
                .exists()
            )
            job = await session.scalar(
                select(Job)
                .where(
                    or_(
                        and_(Job.status == "pending", Job.available_at <= now),
                        and_(Job.status == "processing", Job.lease_until <= now),
                    ),
                    ~earlier,
                    ~busy,
                )
                .order_by(Job.created_at, Job.raw_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            # Serialize claims within a tag. The lease protects its processing
            # after this short transaction releases the advisory lock.
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(func.hashtextextended(job.tag, 0)))
            )
            if not locked:
                return None
            if job.attempts >= self.settings.worker_max_attempts:
                job.status, job.last_error = "failed", "RETRY_LIMIT"
                job.lease_token, job.lease_until = None, None
                return None
            job.status, job.attempts = "processing", job.attempts + 1
            job.lease_token = uuid.uuid4().hex
            job.lease_until = now + timedelta(seconds=self.settings.worker_lease_seconds)
            job.updated_at = now
            return job

    async def owned(self, session, job: Job) -> Job:
        current = await session.scalar(
            select(Job)
            .where(
                Job.raw_id == job.raw_id,
                Job.status == "processing",
                Job.lease_token == job.lease_token,
                Job.lease_until > func.now(),
            )
            .with_for_update()
        )
        if current is None:
            raise LeaseLost()
        return current

    async def heartbeat(self, job: Job):
        async with self.db.transaction() as session:
            updated = await session.scalar(
                update(Job)
                .where(
                    Job.raw_id == job.raw_id,
                    Job.status == "processing",
                    Job.lease_token == job.lease_token,
                    Job.lease_until > func.now(),
                )
                .values(
                    lease_until=datetime.now(UTC)
                    + timedelta(seconds=self.settings.worker_lease_seconds)
                )
                .returning(Job.raw_id)
            )
            if updated is None:
                raise LeaseLost()

    async def checkpoint(self, job: Job, value: dict):
        async with self.db.transaction() as session:
            current = await self.owned(session, job)
            current.checkpoint = value
        job.checkpoint = value

    async def fail(self, job: Job, code: str):
        async with self.db.transaction() as session:
            try:
                current = await self.owned(session, job)
            except LeaseLost:
                return
            current.status = (
                "failed" if current.attempts >= self.settings.worker_max_attempts else "pending"
            )
            current.last_error = code[:120]
            current.lease_token, current.lease_until = None, None
            current.available_at = datetime.now(UTC) + timedelta(
                seconds=min(60, self.settings.worker_retry_seconds * 2 ** (current.attempts - 1))
            )
            current.updated_at = datetime.now(UTC)

    async def retry(self, tag: str, raw_id: str):
        async with self.db.transaction() as session:
            found = await session.scalar(
                update(Job)
                .where(Job.tag == tag, Job.raw_id == raw_id, Job.status == "failed")
                .values(
                    status="pending",
                    attempts=0,
                    last_error=None,
                    available_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(Job.raw_id)
            )
            if found is None:
                raise MemoryError(
                    "FAILED_JOB_NOT_FOUND", "No failed task matches this tag and raw ID."
                )
