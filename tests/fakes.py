import json

from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.schemas import Extraction, ProfilePatch, Resolutions


class FakeModels:
    def __init__(self, extraction=None):
        self.extraction = extraction or {
            "memories": [{"content": "Atlas 使用 PostgreSQL 保存业务数据。"}],
            "entities": [
                {
                    "key": "atlas",
                    "name": "Atlas",
                    "type": "project",
                    "summary": "A software project",
                    "memory_indices": [0],
                },
                {
                    "key": "postgres",
                    "name": "PostgreSQL",
                    "type": "technology",
                    "summary": "A relational database",
                    "memory_indices": [0],
                },
            ],
            "relations": [
                {
                    "source_key": "atlas",
                    "target_key": "postgres",
                    "type": "uses",
                    "status": "active",
                    "memory_indices": [0],
                }
            ],
            "profile_updates": [],
        }
        self.extraction_calls = 0
        self.embedding_failures = 0
        self.patch = ProfilePatch(replacements=[], append="- 默认使用中文。")
        self.profile_hook = None
        self.rerank_error = None

    async def generate_structured(self, messages, schema):
        data = json.loads(messages[-1]["content"])
        if schema is Extraction:
            self.extraction_calls += 1
            return Extraction.model_validate(self.extraction)
        if schema is ProfilePatch:
            if self.profile_hook is not None:
                await self.profile_hook()
                self.profile_hook = None
            return self.patch
        if schema is Resolutions:
            selected = {}
            for entity in data["extraction"]["entities"]:
                match = next(
                    (
                        c
                        for c in data["entity_candidates"][entity["key"]]
                        if c["name"] == entity["name"] and c["type"] == entity["type"]
                    ),
                    None,
                )
                selected[entity["key"]] = match["id"] if match else None
            relations = []
            for index, relation in enumerate(data["extraction"]["relations"]):
                match = next(
                    (
                        edge
                        for edge in data["existing_relationships"]
                        if edge["source_id"] == selected[relation["source_key"]]
                        and edge["target_id"] == selected[relation["target_key"]]
                        and edge["type"] == relation["type"]
                    ),
                    None,
                )
                relations.append({"index": index, "existing_id": match["id"] if match else None})
            return Resolutions.model_validate(
                {
                    "entities": [
                        {"key": key, "existing_id": value} for key, value in selected.items()
                    ],
                    "relations": relations,
                }
            )
        raise AssertionError(schema)

    async def embed(self, texts, *, kind="document"):
        if self.embedding_failures:
            self.embedding_failures -= 1
            raise MemoryError("PROVIDER_TIMEOUT", "Injected embedding timeout")
        return [[1.0, 0.1, 0.01] for _ in texts]

    async def rerank(self, query, documents):
        if self.rerank_error:
            raise self.rerank_error
        return [float(index) for index in range(len(documents))]
