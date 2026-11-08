from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Status = Literal["pending", "processing", "completed", "failed"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddInput(Model):
    tag: Tag
    content: Annotated[str, StringConstraints(min_length=1, max_length=100000)]
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def meaningful_content(self):
        if not self.content.strip():
            raise ValueError("content must contain non-whitespace text")
        return self


class AddResult(Model):
    raw_id: str
    processing_status: Status


class SearchInput(Model):
    tag: Tag
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    top_k: int = Field(default=10, ge=1, le=100)
    rerank: bool = False


class SearchHit(Model):
    id: str
    content: str
    raw_ids: list[str]
    score: float


class SearchMeta(Model):
    score_type: Literal["rrf", "reranker"] = "rrf"
    rerank_applied: bool = False
    rerank_error: str | None = None


class SearchResult(Model):
    results: list[SearchHit]
    meta: SearchMeta = Field(default_factory=SearchMeta)


class GetMemoryInput(Model):
    tag: Tag
    id: Identifier


class MemoryView(Model):
    id: str
    tag: str
    type: Literal["raw", "extracted"]
    content: str
    raw_ids: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RawMemoryView(MemoryView):
    type: Literal["raw"] = "raw"
    processing_status: Status
    extracted_ids: list[str]


class ExtractedMemoryView(MemoryView):
    type: Literal["extracted"] = "extracted"


class ProfileInput(Model):
    tag: Tag


class EditProfileInput(ProfileInput):
    expected_version: int = Field(ge=0)
    content: str = Field(max_length=100000)


class ProfileView(Model):
    tag: str
    content: str = ""
    version: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ErrorResult(Model):
    error: dict[str, Any]


class ExtractedFact(Model):
    content: Text


class ExtractedEntity(Model):
    key: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    summary: str = Field(max_length=2000)
    memory_indices: list[int] = Field(min_length=1, max_length=64)


class ExtractedRelation(Model):
    source_key: str
    target_key: str
    type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    status: Literal["active", "inactive"]
    memory_indices: list[int] = Field(min_length=1, max_length=64)


class Extraction(Model):
    memories: list[ExtractedFact] = Field(max_length=64)
    entities: list[ExtractedEntity] = Field(max_length=64)
    relations: list[ExtractedRelation] = Field(max_length=128)
    profile_updates: list[Text] = Field(max_length=32)

    @model_validator(mode="after")
    def valid_references(self):
        keys = [entity.key for entity in self.entities]
        if len(keys) != len(set(keys)):
            raise ValueError("Entity keys must be unique")
        for item in [*self.entities, *self.relations]:
            if any(index < 0 or index >= len(self.memories) for index in item.memory_indices):
                raise ValueError("Every memory index must reference an extracted memory")
        for relation in self.relations:
            if relation.source_key not in keys or relation.target_key not in keys:
                raise ValueError("Every relation endpoint must reference an extracted entity")
        return self


class EntityResolution(Model):
    key: str
    existing_id: str | None


class RelationResolution(Model):
    index: int
    existing_id: str | None


class Resolutions(Model):
    entities: list[EntityResolution]
    relations: list[RelationResolution]


class TextReplacement(Model):
    before: Annotated[str, StringConstraints(min_length=1)]
    after: str


class ProfilePatch(Model):
    replacements: list[TextReplacement] = Field(max_length=32)
    append: str = Field(max_length=20000)
