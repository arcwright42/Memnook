import asyncio
import logging

from memory_mcp.application.extraction import Extractor, stable_id
from memory_mcp.config import Settings
from memory_mcp.domain.errors import LeaseLost, MemoryError
from memory_mcp.domain.profile import apply_profile_patch
from memory_mcp.domain.schemas import EditProfileInput, Extraction
from memory_mcp.storage.ingest import IngestWriter
from memory_mcp.storage.repository import Repository
from memory_mcp.storage.tasks import TaskQueue
from memory_mcp.storage.text import tokenize

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, repository: Repository, extractor: Extractor, settings: Settings):
        self.repository, self.extractor, self.settings = repository, extractor, settings
        self.tasks = TaskQueue(repository.db, settings)
        self.writer = IngestWriter(repository, self.tasks)

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.worker_lease_seconds / 3)
            await self.tasks.heartbeat(job)

    async def run_once(self) -> bool:
        job = await self.tasks.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await self.process(job)
        except LeaseLost:
            logger.info("Task ownership expired: %s", job.raw_id)
        except asyncio.CancelledError:
            await self.tasks.fail(job, "WORKER_STOPPED")
            raise
        except Exception as error:
            code = (
                error.code if isinstance(error, MemoryError) else "INTERNAL_" + type(error).__name__
            )
            logger.warning("Task %s attempt %s: %s", job.raw_id, job.attempts, code)
            await self.tasks.fail(job, code)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def process(self, job):
        repo = self.repository
        raw = await repo.get_memory(job.tag, job.raw_id)
        if "extraction" in job.checkpoint:
            extraction = Extraction.model_validate(job.checkpoint["extraction"])
        else:
            extraction = await self.extractor.extract(raw)
            await self.tasks.checkpoint(job, {"extraction": extraction.model_dump()})
        prepared = await self.extractor.prepare(raw, extraction)
        base_profile = await repo.get_profile(raw.tag)
        patch = await self.extractor.profile_patch(raw, extraction, base_profile)
        # Reject malformed patches before entering a transaction.
        apply_profile_patch(base_profile.content, patch)
        tokenized = await asyncio.to_thread(
            lambda: [" ".join(tokenize(m.content)) for m in extraction.memories]
        )
        current = base_profile
        for attempt in range(4):
            content = apply_profile_patch(
                base_profile.content,
                patch,
                current=current.content if current.version != base_profile.version else None,
            )
            edit = (
                EditProfileInput(tag=raw.tag, expected_version=current.version, content=content)
                if content != current.content
                else None
            )
            try:
                await self.writer.commit(
                    job,
                    raw,
                    extraction=extraction,
                    memory_vectors=prepared.memory_vectors,
                    entity_vectors=prepared.entity_vectors,
                    entity_ids=prepared.entity_ids,
                    new_entity_keys=prepared.new_entity_keys,
                    relation_types=prepared.relation_types,
                    memory_ids=[
                        stable_id("mem", raw.id, str(i)) for i in range(len(extraction.memories))
                    ],
                    relation_ids=[
                        stable_id("rel", raw.id, str(i)) for i in range(len(extraction.relations))
                    ],
                    tokenized=tokenized,
                    profile_edit=edit,
                )
                return
            except MemoryError as error:
                if error.code != "PROFILE_VERSION_CONFLICT":
                    raise
                if attempt == 3:
                    raise MemoryError(
                        "PROFILE_CONTENTION", "Profile changed repeatedly; retry the task later."
                    ) from error
                current = await repo.get_profile(raw.tag)

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
                except TimeoutError:
                    pass
