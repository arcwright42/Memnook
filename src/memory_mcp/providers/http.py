import copy
import json
import math
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from memory_mcp.config import Settings
from memory_mcp.domain.errors import MemoryError

T = TypeVar("T", bound=BaseModel)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(schema)

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            value.pop("default", None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


class HTTPModels:
    """Three small provider contracts, using a shared connection pool."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _post(self, base_url, path, key, payload, request_timeout):
        if not base_url:
            raise MemoryError("PROVIDER_NOT_CONFIGURED", "A model endpoint is not configured.")
        headers = (
            {"Authorization": f"Bearer {key.get_secret_value()}"} if key.get_secret_value() else {}
        )
        try:
            response = await self.client.post(
                f"{base_url.rstrip('/')}/{path}",
                json=payload,
                headers=headers,
                timeout=request_timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as error:
            raise MemoryError("PROVIDER_TIMEOUT", "The model request timed out.") from error
        except httpx.HTTPStatusError as error:
            # Do not persist response bodies, URLs, or headers in job error fields.
            raise MemoryError(
                f"PROVIDER_HTTP_{error.response.status_code}",
                "The model endpoint rejected the request.",
            ) from error
        except httpx.RequestError as error:
            raise MemoryError(
                "PROVIDER_UNAVAILABLE", "The model endpoint is unavailable."
            ) from error
        except ValueError as error:
            raise MemoryError(
                "PROVIDER_INVALID_RESPONSE", "The model returned invalid JSON."
            ) from error

    async def generate_structured(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        cfg = self.settings
        if not cfg.llm_model:
            raise MemoryError(
                "LLM_NOT_CONFIGURED", "Set MEMORY_LLM_MODEL before running extraction."
            )
        payload = {"model": cfg.llm_model, "messages": list(messages), "stream": False}
        if cfg.llm_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_schema(schema.model_json_schema()),
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}
            payload["messages"] = [
                *messages,
                {
                    "role": "system",
                    "content": "Return JSON matching this schema: "
                    + json.dumps(schema.model_json_schema()),
                },
            ]
        for attempt in range(2):
            data = await self._post(
                cfg.llm_base_url, "chat/completions", cfg.llm_api_key, payload, cfg.llm_timeout
            )
            try:
                return schema.model_validate_json(data["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValidationError):
                if attempt == 1:
                    raise MemoryError(
                        "INVALID_MODEL_OUTPUT", "Model output failed schema validation."
                    ) from None
                payload["messages"].append(
                    {
                        "role": "system",
                        "content": "Your previous response failed validation. Return a complete JSON object matching the required schema; check all references and indices.",
                    }
                )
        raise AssertionError("unreachable")

    async def embed(
        self, texts: list[str], *, kind: Literal["document", "query"] = "document"
    ) -> list[list[float]]:
        cfg = self.settings
        if not cfg.embedding_model:
            raise MemoryError("EMBEDDING_NOT_CONFIGURED", "Set MEMORY_EMBEDDING_MODEL.")
        prefix = cfg.embedding_query_prefix if kind == "query" else cfg.embedding_document_prefix
        result: list[list[float]] = []
        for offset in range(0, len(texts), 32):
            batch = texts[offset : offset + 32]
            payload = {"model": cfg.embedding_model, "input": [prefix + item for item in batch]}
            if cfg.embedding_send_dimensions:
                payload["dimensions"] = cfg.embedding_dimensions
            data = await self._post(
                cfg.embedding_base_url,
                "embeddings",
                cfg.embedding_api_key,
                payload,
                cfg.embedding_timeout,
            )
            try:
                records = sorted(data["data"], key=lambda item: item["index"])
                if [record["index"] for record in records] != list(range(len(batch))):
                    raise ValueError("Missing or duplicate embedding indices")
                for record in records:
                    vector = [float(value) for value in record["embedding"]]
                    if len(vector) != cfg.embedding_dimensions or not all(
                        math.isfinite(v) for v in vector
                    ):
                        raise ValueError("Invalid dimension or non-finite embedding")
                    if not any(vector):
                        raise ValueError("A zero vector cannot be used for cosine retrieval")
                    result.append(vector)
            except (KeyError, TypeError, ValueError) as error:
                raise MemoryError(
                    "INVALID_EMBEDDING",
                    "Embedding output has invalid indices, dimensions, or values.",
                ) from error
        return result

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        cfg = self.settings
        if not cfg.reranker_base_url or not cfg.reranker_model:
            raise MemoryError("RERANKER_NOT_CONFIGURED", "No reranker is configured.")
        data = await self._post(
            cfg.reranker_base_url,
            "rerank",
            cfg.reranker_api_key,
            {
                "model": cfg.reranker_model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            },
            cfg.reranker_timeout,
        )
        try:
            records = sorted(data["results"], key=lambda item: item["index"])
            if [record["index"] for record in records] != list(range(len(documents))):
                raise ValueError("Incomplete reranking")
            scores = [float(record["relevance_score"]) for record in records]
            if not all(math.isfinite(score) for score in scores):
                raise ValueError("Invalid reranking scores")
            return scores
        except (KeyError, TypeError, ValueError) as error:
            raise MemoryError(
                "INVALID_RERANKING", "Reranker returned incomplete or invalid results."
            ) from error
