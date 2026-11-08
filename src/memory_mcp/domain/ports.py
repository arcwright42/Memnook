from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLM(Protocol):
    async def generate_structured(self, messages: list[dict[str, str]], schema: type[T]) -> T: ...


class Embedder(Protocol):
    async def embed(
        self, texts: list[str], *, kind: Literal["document", "query"] = "document"
    ) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...
