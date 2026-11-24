"""Retrieval from the Bedrock Knowledge Base (S3 Vectors store)."""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.config import Config

_BOTO_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 4}, connect_timeout=3, read_timeout=10)


class KnowledgeBaseRetriever:
    def __init__(self, knowledge_base_id: str | None = None, client: Any | None = None):
        self.knowledge_base_id = knowledge_base_id or os.environ.get("KNOWLEDGE_BASE_ID", "")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-agent-runtime", config=_BOTO_CONFIG)
        return self._client

    def retrieve(self, query: str, top_k: int) -> list[dict]:
        if not self.knowledge_base_id:
            raise RuntimeError("knowledge base is not configured")
        resp = self.client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        results = []
        for i, r in enumerate(resp.get("retrievalResults", []), start=1):
            uri = (((r.get("location") or {}).get("s3Location")) or {}).get("uri", "")
            results.append(
                {
                    "citation_id": i,
                    "source": uri.rsplit("/", 1)[-1] if uri else "unknown",
                    "uri": uri,
                    "score": round(float(r.get("score", 0.0)), 4),
                    "text": (r.get("content") or {}).get("text", ""),
                }
            )
        return results
