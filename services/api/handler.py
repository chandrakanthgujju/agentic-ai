"""Conversation and model-catalogue API (non-streaming routes).

GET    /api/models
GET    /api/conversations
GET    /api/conversations/{conversationId}
DELETE /api/conversations/{conversationId}
"""

from __future__ import annotations

import uuid

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError, UnauthorizedError
from aws_lambda_powertools.logging import correlation_paths

from shared.auth import UnauthorizedError as NoIdentity
from shared.auth import caller_from_rest_event
from shared.conversations import ConversationStore, display_messages
from shared.model_catalog import catalog_from_env

logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayRestResolver(strip_prefixes=["/api"])

_store: ConversationStore | None = None


def store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store


def _user_id() -> str:
    try:
        return caller_from_rest_event(app.current_event.raw_event).user_id
    except NoIdentity as exc:
        raise UnauthorizedError("Unauthorized") from exc


def _conversation_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise BadRequestError("invalid conversation id") from exc


@app.get("/models")
@tracer.capture_method
def list_models():
    _user_id()
    return {"models": catalog_from_env().public_list()}


@app.get("/conversations")
@tracer.capture_method
def list_conversations():
    return {"conversations": store().list(_user_id())}


@app.get("/conversations/<conversation_id>")
@tracer.capture_method
def get_conversation(conversation_id: str):
    item = store().get(_user_id(), _conversation_id(conversation_id))
    if item is None:
        raise NotFoundError("conversation not found")
    return {
        "conversationId": conversation_id,
        "title": item.get("title"),
        "modelId": item.get("model_id"),
        "messages": display_messages(item["messages"]),
    }


@app.delete("/conversations/<conversation_id>")
@tracer.capture_method
def delete_conversation(conversation_id: str):
    store().delete(_user_id(), _conversation_id(conversation_id))
    return {"deleted": True}


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST, clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):
    return app.resolve(event, context)
