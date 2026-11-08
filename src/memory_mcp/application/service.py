import asyncio
import math

from memory_mcp.config import Settings
from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.ports import Embedder, Reranker
from memory_mcp.domain.ranking import rrf
from memory_mcp.domain.schemas import SearchHit, SearchInput, SearchMeta, SearchResult
from memory_mcp.storage.repository import Repository


class MemoryService:
    def __init__(
        self,
        repository: Repository,
        embedder: Embedder,
        reranker: Reranker | None,
        settings: Settings,
    ):
        self.repository, self.embedder, self.reranker, self.settings = (
            repository,
            embedder,
            reranker,
            settings,
        )

    async def search(self, request: SearchInput) -> SearchResult:
        repo, cfg = self.repository, self.settings
        if not await repo.has_memories(request.tag):
            return SearchResult(results=[])
        async with repo.db.sessions() as session:
            await repo.ensure_space(session)
        vector = (await self.embedder.embed([request.query], kind="query"))[0]
        semantic, keyword, entity = await repo.candidates(
            request.tag, request.query, vector, limit=max(cfg.candidate_limit, request.top_k)
        )
        memory_route = [identifier for identifier, _ in rrf(semantic, keyword)]
        fused = rrf(memory_route, entity)
        budget = max(request.top_k, cfg.rerank_limit) if request.rerank else request.top_k
        fused = fused[:budget]
        hits = await repo.fetch_hits(request.tag, [identifier for identifier, _ in fused])
        results = [
            SearchHit(**hits[identifier], score=score)
            for identifier, score in fused
            if identifier in hits
        ]
        meta = SearchMeta()
        if request.rerank and results:
            try:
                if self.reranker is None:
                    raise MemoryError("RERANKER_NOT_CONFIGURED", "No reranker is configured.")
                async with asyncio.timeout(cfg.reranker_timeout):
                    scores = await self.reranker.rerank(
                        request.query, [item.content for item in results]
                    )
                if len(scores) != len(results) or not all(math.isfinite(score) for score in scores):
                    raise MemoryError("INVALID_RERANKING", "Reranker returned invalid scores.")
                for item, score in zip(results, scores, strict=True):
                    item.score = score
                results.sort(key=lambda item: (-item.score, item.id))
                meta = SearchMeta(score_type="reranker", rerank_applied=True)
            except Exception as error:
                meta.rerank_error = (
                    error.code
                    if isinstance(error, MemoryError)
                    else "RERANKER_TIMEOUT"
                    if isinstance(error, TimeoutError)
                    else "RERANKER_ERROR"
                )
        return SearchResult(results=results[: request.top_k], meta=meta)
