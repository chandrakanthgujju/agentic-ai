"""Server-side allowlist of Bedrock models the UI may select.

The list is defined once in ``cdk.json`` (context ``agentic.models``) and passed
to Lambdas as the ``MODELS_JSON`` environment variable. Any model id the client
sends that is not in this list is rejected before Bedrock is called, so a user
cannot point the agent at an expensive model by editing a request.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import BaseModel, Field


class ModelSpec(BaseModel):
    id: str = Field(description="Bedrock model id or inference profile id used in Converse")
    label: str
    provider: str
    max_tokens: int = Field(default=1024, alias="maxTokens", ge=64, le=8192)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    supports_tools: bool = Field(default=True, alias="supportsTools")

    model_config = {"populate_by_name": True}


class ModelCatalog:
    def __init__(self, models: list[ModelSpec], default_id: str | None = None):
        if not models:
            raise ValueError("model catalog is empty")
        self._by_id = {m.id: m for m in models}
        self.default_id = default_id if default_id in self._by_id else models[0].id

    @classmethod
    def from_json(cls, raw: str, default_id: str | None = None) -> "ModelCatalog":
        return cls([ModelSpec.model_validate(m) for m in json.loads(raw)], default_id)

    def resolve(self, model_id: str | None) -> ModelSpec:
        if model_id is None:
            return self._by_id[self.default_id]
        spec = self._by_id.get(model_id)
        if spec is None:
            raise ValueError(f"model '{model_id}' is not allowed")
        return spec

    def public_list(self) -> list[dict]:
        return [{"id": m.id, "label": m.label, "provider": m.provider, "default": m.id == self.default_id} for m in self._by_id.values()]


@lru_cache(maxsize=1)
def catalog_from_env() -> ModelCatalog:
    return ModelCatalog.from_json(os.environ["MODELS_JSON"], os.environ.get("DEFAULT_MODEL_ID"))
