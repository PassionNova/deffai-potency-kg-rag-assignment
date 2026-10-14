from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://potency_app:potency_app_dev@localhost:55433/potency"
    owner_database_url: str = "postgresql://potency_owner:potency_owner_dev@localhost:55433/potency"

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None

    potency_embedder: Literal["fastembed", "fake"] = "fastembed"
    potency_llm: Literal["anthropic", "fake"] = "anthropic"
    potency_embedding_model: str = "BAAI/bge-base-en-v1.5"
    fastembed_cache_path: str = ".cache/fastembed"
    potency_embed_threads: int = 4

    potency_max_usd: float = 40.0
    potency_price_input_per_mtok: float = 2.0
    potency_price_output_per_mtok: float = 10.0
    potency_extraction_concurrency: int = 4

    potency_dev_tokens: str = Field(default="dev-public=public-fda/casgevy-potency")

    @property
    def generation_model(self) -> str:
        if self.potency_llm == "fake":
            return "fake-llm-v1"
        if not self.anthropic_model:
            raise RuntimeError("ANTHROPIC_MODEL is not set; add it to .env")
        return self.anthropic_model

    @property
    def embedding_model_id(self) -> str:
        if self.potency_embedder == "fake":
            return "fake-hash-embedder-v1"
        return self.potency_embedding_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
