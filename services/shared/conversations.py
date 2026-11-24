"""Conversation persistence in DynamoDB.

Single-table layout, partitioned by user so one user can never read another's
history (every key includes the verified Cognito ``sub``):

    pk = "USER#<sub>"   sk = "CONV#<conversation_id>"
    title, model_id, created_at, updated_at, messages (JSON), ttl

Messages are stored in Bedrock Converse format so a conversation can be replayed
to any allowlisted model. History is trimmed before it is written, which keeps
items far below DynamoDB's 400 KB item limit and bounds prompt cost.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

MAX_STORED_MESSAGES = 24
MAX_TOOL_RESULT_CHARS_IN_HISTORY = 2000


def _is_user_text_turn(message: dict) -> bool:
    return message.get("role") == "user" and any("text" in block for block in message.get("content", []))


def trim_history(messages: list[dict], max_messages: int = MAX_STORED_MESSAGES) -> list[dict]:
    """Keep the newest ``max_messages`` messages, starting on a user *text* turn.

    Converse requires the first message to be from the user, and a toolResult
    block must follow the assistant toolUse that produced it, so we never cut
    between the two.
    """
    if len(messages) <= max_messages:
        trimmed = list(messages)
    else:
        trimmed = list(messages[-max_messages:])
    while trimmed and not _is_user_text_turn(trimmed[0]):
        trimmed.pop(0)
    return [_shrink_tool_results(m) for m in trimmed]


def _shrink_tool_results(message: dict) -> dict:
    content = []
    for block in message.get("content", []):
        if "toolResult" in block:
            tr = dict(block["toolResult"])
            new_parts = []
            for part in tr.get("content", []):
                if "text" in part and len(part["text"]) > MAX_TOOL_RESULT_CHARS_IN_HISTORY:
                    part = {"text": part["text"][:MAX_TOOL_RESULT_CHARS_IN_HISTORY] + " …[truncated]"}
                new_parts.append(part)
            tr["content"] = new_parts
            block = {"toolResult": tr}
        content.append(block)
    return {"role": message["role"], "content": content}


def strip_tool_blocks(messages: list[dict]) -> list[dict]:
    """Rewrite toolUse/toolResult blocks as plain text.

    Converse rejects history containing tool blocks when the request has no
    ``toolConfig``; this lets a conversation continue if the tool server is down.
    """
    out: list[dict] = []
    for m in messages:
        parts = []
        for block in m.get("content", []):
            if "text" in block:
                parts.append(block)
            elif "toolUse" in block:
                parts.append({"text": f"[called tool {block['toolUse'].get('name')}]"})
            elif "toolResult" in block:
                texts = [p.get("text", "") for p in block["toolResult"].get("content", []) if "text" in p]
                parts.append({"text": "[tool result] " + " ".join(texts)[:500]})
        if not parts:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(parts)
        else:
            out.append({"role": m["role"], "content": parts})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def display_messages(messages: list[dict]) -> list[dict]:
    """Project stored Converse messages to what the UI renders."""
    view = []
    for m in messages:
        text = "".join(b["text"] for b in m.get("content", []) if "text" in b)
        tools = [b["toolUse"]["name"] for b in m.get("content", []) if "toolUse" in b]
        if text or tools:
            view.append({"role": m["role"], "text": text, "tools": tools})
    return view


class ConversationStore:
    def __init__(self, table_name: str | None = None, ttl_days: int | None = None, table: Any | None = None):
        self.ttl_days = int(ttl_days if ttl_days is not None else os.environ.get("CONVERSATION_TTL_DAYS", "30"))
        self._table = table or boto3.resource("dynamodb").Table(table_name or os.environ["CONVERSATIONS_TABLE"])

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _key(user_id: str, conversation_id: str) -> dict:
        return {"pk": f"USER#{user_id}", "sk": f"CONV#{conversation_id}"}

    def get(self, user_id: str, conversation_id: str) -> dict | None:
        item = self._table.get_item(Key=self._key(user_id, conversation_id)).get("Item")
        if not item:
            return None
        item["messages"] = json.loads(item.get("messages") or "[]")
        return item

    def save(self, user_id: str, conversation_id: str, messages: list[dict], model_id: str, title: str | None = None) -> None:
        now = int(time.time())
        self._table.update_item(
            Key=self._key(user_id, conversation_id),
            UpdateExpression=(
                "SET messages = :m, model_id = :model, updated_at = :now, #ttl = :ttl, "
                "created_at = if_not_exists(created_at, :now), title = if_not_exists(title, :title)"
            ),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":m": json.dumps(trim_history(messages), separators=(",", ":")),
                ":model": model_id,
                ":now": now,
                ":ttl": now + self.ttl_days * 86400,
                ":title": (title or "New conversation")[:80],
            },
        )

    def list(self, user_id: str, limit: int = 50) -> list[dict]:
        resp = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("CONV#"),
            ProjectionExpression="sk, title, model_id, updated_at, created_at",
            Limit=limit,
        )
        items = [
            {
                "conversationId": i["sk"].split("#", 1)[1],
                "title": i.get("title", ""),
                "modelId": i.get("model_id"),
                "updatedAt": int(i.get("updated_at", 0)),
            }
            for i in resp.get("Items", [])
        ]
        return sorted(items, key=lambda x: x["updatedAt"], reverse=True)

    def delete(self, user_id: str, conversation_id: str) -> None:
        self._table.delete_item(Key=self._key(user_id, conversation_id))
