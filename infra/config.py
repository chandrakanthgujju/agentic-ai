"""Typed deployment configuration loaded from CDK context.

Defaults live in ``cdk.json`` under ``agentic:config``. Any top-level key can be
overridden on the command line, e.g.::

    cdk deploy --all -c alarmEmail=me@example.com -c enableWaf=true
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from constructs import Node

INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "us-gov.", "global.")


@dataclass(frozen=True)
class ModelConfig:
    id: str
    label: str
    provider: str
    max_tokens: int = 1024
    temperature: float = 0.2

    @property
    def is_inference_profile(self) -> bool:
        return self.id.startswith(INFERENCE_PROFILE_PREFIXES)

    @property
    def foundation_model_id(self) -> str:
        if self.is_inference_profile:
            return self.id.split(".", 1)[1]
        return self.id

    def to_runtime(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "maxTokens": self.max_tokens,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class AppConfig:
    project_name: str
    models: tuple[ModelConfig, ...]
    default_model_id: str
    embedding_model_id: str
    embedding_dimensions: int
    enable_guardrail: bool
    enable_waf: bool
    alarm_email: str
    monthly_budget_usd: int
    api_rate_limit: int
    api_burst_limit: int
    chat_rate_limit: int
    chat_burst_limit: int
    agent_reserved_concurrency: int
    conversation_ttl_days: int
    retain_data: bool
    lambda_web_adapter_layer_version: int
    lambda_web_adapter_account: str
    cognito_domain_prefix: str
    extra_upload_origins: tuple[str, ...] = field(default_factory=tuple)

    def models_json(self) -> str:
        return json.dumps([m.to_runtime() for m in self.models], separators=(",", ":"))

    @staticmethod
    def from_node(node: Node) -> "AppConfig":
        raw: dict[str, Any] = dict(node.try_get_context("agentic:config") or {})
        for key in list(raw):
            override = node.try_get_context(key)
            if override is not None:
                if isinstance(override, str):
                    try:
                        override = json.loads(override)
                    except json.JSONDecodeError:
                        pass
                raw[key] = override

        models = tuple(
            ModelConfig(
                id=m["id"],
                label=m["label"],
                provider=m["provider"],
                max_tokens=int(m.get("maxTokens", 1024)),
                temperature=float(m.get("temperature", 0.2)),
            )
            for m in raw["models"]
        )
        cfg = AppConfig(
            project_name=raw["projectName"],
            models=models,
            default_model_id=raw.get("defaultModelId") or models[0].id,
            embedding_model_id=raw["embeddingModelId"],
            embedding_dimensions=int(raw["embeddingDimensions"]),
            enable_guardrail=bool(raw.get("enableGuardrail", True)),
            enable_waf=bool(raw.get("enableWaf", False)),
            alarm_email=str(raw.get("alarmEmail") or ""),
            monthly_budget_usd=int(raw.get("monthlyBudgetUsd") or 0),
            api_rate_limit=int(raw["apiRateLimit"]),
            api_burst_limit=int(raw["apiBurstLimit"]),
            chat_rate_limit=int(raw["chatRateLimit"]),
            chat_burst_limit=int(raw["chatBurstLimit"]),
            agent_reserved_concurrency=int(raw.get("agentReservedConcurrency") or 0),
            conversation_ttl_days=int(raw.get("conversationTtlDays", 30)),
            retain_data=bool(raw.get("retainData", False)),
            lambda_web_adapter_layer_version=int(raw["lambdaWebAdapterLayerVersion"]),
            lambda_web_adapter_account=str(raw.get("lambdaWebAdapterAccount", "753240598075")),
            cognito_domain_prefix=str(raw.get("cognitoDomainPrefix") or ""),
            extra_upload_origins=tuple(raw.get("extraUploadOrigins") or ()),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if len(self.models) < 1:
            raise ValueError("at least one model is required")
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate model ids")
        if self.default_model_id not in ids:
            raise ValueError("defaultModelId must be one of models[].id")
        if not re.match(r"^[a-z][a-z0-9-]{2,20}$", self.project_name):
            raise ValueError("projectName must be 3-21 chars: lowercase letters, digits, hyphens")
        if self.chat_rate_limit > self.api_rate_limit:
            raise ValueError("chatRateLimit cannot exceed apiRateLimit")
        if self.alarm_email and "@" not in self.alarm_email:
            raise ValueError("alarmEmail is not an email address")
