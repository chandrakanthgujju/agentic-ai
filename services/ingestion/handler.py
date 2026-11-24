"""SQS consumer that turns document changes into Knowledge Base ingestion jobs.

S3 (docs/ prefix) --EventBridge--> SQS (batching window, DLQ) --> this Lambda

* Batching: the event source mapping waits up to 60 s and takes up to 100
  messages, so a bulk upload of 50 files triggers one ingestion job, not 50.
* Concurrency: Bedrock allows one running ingestion job per data source. When a
  job is already running we report the batch as failed; SQS redelivers it after
  the visibility timeout. After ``maxReceiveCount`` attempts it lands in the DLQ
  and an alarm fires.
* Idempotency: ``clientToken`` is derived from the message ids, so a redelivered
  batch that Bedrock already accepted does not start a duplicate job.
"""

from __future__ import annotations

import hashlib
import json
import os

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from botocore.exceptions import ClientError

logger = Logger()
tracer = Tracer()
metrics = Metrics()

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agent", config=Config(retries={"mode": "adaptive", "max_attempts": 4}))
    return _client


def _changed_keys(records: list[dict]) -> list[str]:
    keys = []
    for r in records:
        try:
            detail = json.loads(r["body"]).get("detail", {})
            keys.append(detail.get("object", {}).get("key", "?"))
        except (json.JSONDecodeError, AttributeError):
            keys.append("?")
    return keys


def client_token(records: list[dict]) -> str:
    digest = hashlib.sha256("|".join(sorted(r["messageId"] for r in records)).encode()).hexdigest()
    return digest[:64]


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):  # noqa: ARG001 - Lambda signature
    records = event.get("Records", [])
    if not records:
        return {"batchItemFailures": []}
    keys = _changed_keys(records)
    logger.info("documents changed", extra={"count": len(records), "keys": keys[:20]})
    try:
        job = client().start_ingestion_job(
            knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
            dataSourceId=os.environ["DATA_SOURCE_ID"],
            clientToken=client_token(records),
            description=f"auto sync: {len(records)} change(s)",
        )["ingestionJob"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("ConflictException", "ThrottlingException", "ServiceQuotaExceededException"):
            logger.warning("ingestion job not started; batch will be retried", extra={"error_code": code})
            metrics.add_metric(name="IngestionDeferred", unit=MetricUnit.Count, value=1)
            return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records]}
        raise
    metrics.add_metric(name="IngestionJobsStarted", unit=MetricUnit.Count, value=1)
    logger.info("ingestion job started", extra={"job_id": job["ingestionJobId"], "status": job["status"]})
    return {"batchItemFailures": []}
