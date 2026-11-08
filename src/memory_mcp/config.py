import hashlib
import json

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORY_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "postgresql+psycopg://memory:memory@localhost:5432/memory"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_json_schema: bool = True
    llm_timeout: float = Field(default=90, gt=0)

    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=1024, ge=1, le=16000)
    embedding_send_dimensions: bool = False
    embedding_document_prefix: str = ""
    embedding_query_prefix: str = ""
    embedding_timeout: float = Field(default=30, gt=0)

    reranker_base_url: str = ""
    reranker_api_key: SecretStr = SecretStr("")
    reranker_model: str = ""
    reranker_timeout: float = Field(default=3, gt=0)

    candidate_limit: int = Field(default=60, ge=1, le=1000)
    entity_top_k: int = Field(default=8, ge=1, le=100)
    entity_memory_limit: int = Field(default=20, ge=1, le=200)
    rerank_limit: int = Field(default=50, ge=1, le=500)
    worker_poll_seconds: float = Field(default=1, gt=0)
    worker_lease_seconds: int = Field(default=180, ge=3)
    worker_max_attempts: int = Field(default=4, ge=1, le=20)
    worker_retry_seconds: float = Field(default=2, ge=0)

    @property
    def embedding_fingerprint(self) -> str:
        # A model name alone is insufficient: endpoints and instruction prefixes
        # can produce different spaces even when the dimension count matches.
        identity = {
            "base_url": self.embedding_base_url.rstrip("/"),
            "model": self.embedding_model,
            "dimensions": self.embedding_dimensions,
            "document_prefix": self.embedding_document_prefix,
            "query_prefix": self.embedding_query_prefix,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
