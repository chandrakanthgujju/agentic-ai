"""Knowledge-base administration API (Cognito group ``admin`` only).

GET    /api/kb/documents              list documents in the source bucket
POST   /api/kb/documents              get a presigned POST to upload one document
DELETE /api/kb/documents/{name}       delete a document (re-sync removes its vectors)
POST   /api/kb/sync                   start an ingestion job now
GET    /api/kb/sync                   recent ingestion jobs and their statistics

Uploads go straight from the browser to S3 with a presigned POST whose policy
pins the object key, the content type and a maximum size, so the Lambda never
handles file bytes and a client cannot write anywhere else in the bucket.
"""

from __future__ import annotations

import os
import re
import urllib.parse

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, ForbiddenError, NotFoundError, UnauthorizedError
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError

from shared.auth import UnauthorizedError as NoIdentity
from shared.auth import caller_from_rest_event

logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayRestResolver(strip_prefixes=["/api"])

DOCS_PREFIX = "docs/"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Bedrock KB per-file limit for S3 data sources
ALLOWED_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,120}$")

_clients: dict = {}


def s3():
    if "s3" not in _clients:
        region = os.environ.get("AWS_REGION", "us-east-1")
        _clients["s3"] = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _clients["s3"]


def bedrock_agent():
    if "agent" not in _clients:
        _clients["agent"] = boto3.client("bedrock-agent", config=Config(retries={"mode": "adaptive", "max_attempts": 4}))
    return _clients["agent"]


def _bucket() -> str:
    return os.environ["DOCS_BUCKET"]


def _require_admin() -> None:
    try:
        caller = caller_from_rest_event(app.current_event.raw_event)
    except NoIdentity as exc:
        raise UnauthorizedError("Unauthorized") from exc
    if not caller.is_admin:
        raise ForbiddenError("admin group membership required")


def validate_name(name: str) -> tuple[str, str]:
    name = urllib.parse.unquote(name or "").strip()
    ext = os.path.splitext(name)[1].lower()
    if not NAME_RE.match(name) or ".." in name or ext not in ALLOWED_TYPES:
        raise BadRequestError(f"invalid file name; allowed types: {', '.join(sorted(ALLOWED_TYPES))}")
    return name, ALLOWED_TYPES[ext]


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    size: int = Field(gt=0, le=MAX_UPLOAD_BYTES)


@app.get("/kb/documents")
@tracer.capture_method
def list_documents():
    _require_admin()
    paginator = s3().get_paginator("list_objects_v2")
    docs = []
    for page in paginator.paginate(Bucket=_bucket(), Prefix=DOCS_PREFIX, PaginationConfig={"MaxItems": 1000}):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/"):
                continue
            docs.append({"name": obj["Key"][len(DOCS_PREFIX) :], "size": obj["Size"], "lastModified": obj["LastModified"].isoformat()})
    return {"documents": sorted(docs, key=lambda d: d["name"].lower())}


@app.post("/kb/documents")
@tracer.capture_method
def create_upload():
    _require_admin()
    try:
        req = UploadRequest.model_validate(app.current_event.json_body or {})
    except ValidationError as exc:
        raise BadRequestError(f"invalid upload request: {exc.errors(include_url=False)}") from exc
    name, content_type = validate_name(req.filename)
    key = DOCS_PREFIX + name
    post = s3().generate_presigned_post(
        Bucket=_bucket(),
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_UPLOAD_BYTES]],
        ExpiresIn=300,
    )
    metrics.add_metric(name="UploadUrlsIssued", unit=MetricUnit.Count, value=1)
    logger.info("issued upload url", extra={"key": key, "size": req.size})
    return Response(status_code=201, content_type="application/json", body={"upload": post, "key": key})


@app.delete("/kb/documents/<name>")
@tracer.capture_method
def delete_document(name: str):
    _require_admin()
    name, _ = validate_name(name)
    key = DOCS_PREFIX + name
    try:
        s3().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            raise NotFoundError("document not found") from exc
        raise
    s3().delete_object(Bucket=_bucket(), Key=key)
    logger.info("deleted document", extra={"key": key})
    return {"deleted": name, "note": "Vectors are removed by the next ingestion job (started automatically)."}


@app.post("/kb/sync")
@tracer.capture_method
def start_sync():
    _require_admin()
    try:
        job = bedrock_agent().start_ingestion_job(
            knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
            dataSourceId=os.environ["DATA_SOURCE_ID"],
            description="manual sync from admin UI",
        )["ingestionJob"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            return Response(status_code=409, content_type="application/json", body={"message": "An ingestion job is already running."})
        raise
    return Response(status_code=202, content_type="application/json", body={"jobId": job["ingestionJobId"], "status": job["status"]})


@app.get("/kb/sync")
@tracer.capture_method
def sync_status():
    _require_admin()
    resp = bedrock_agent().list_ingestion_jobs(
        knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
        dataSourceId=os.environ["DATA_SOURCE_ID"],
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=5,
    )
    jobs = [
        {
            "jobId": j["ingestionJobId"],
            "status": j["status"],
            "startedAt": j["startedAt"].isoformat() if hasattr(j.get("startedAt"), "isoformat") else j.get("startedAt"),
            "updatedAt": j["updatedAt"].isoformat() if hasattr(j.get("updatedAt"), "isoformat") else j.get("updatedAt"),
            "statistics": j.get("statistics", {}),
        }
        for j in resp.get("ingestionJobSummaries", [])
    ]
    return {"jobs": jobs}


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST, clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):
    return app.resolve(event, context)
