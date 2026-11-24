import boto3
import pytest
from moto import mock_aws

from shared.conversations import ConversationStore, display_messages, strip_tool_blocks, trim_history


def _u(t):
    return {"role": "user", "content": [{"text": t}]}


def _a(t):
    return {"role": "assistant", "content": [{"text": t}]}


def test_trim_starts_on_user_text_turn():
    tool_use = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "1", "name": "x", "input": {}}}]}
    tool_res = {"role": "user", "content": [{"toolResult": {"toolUseId": "1", "content": [{"text": "r"}], "status": "success"}}]}
    msgs = [_u("q1"), tool_use, tool_res, _a("a1"), _u("q2"), _a("a2")]
    out = trim_history(msgs, max_messages=4)
    assert out == [_u("q2"), _a("a2")]


def test_trim_shrinks_large_tool_results():
    big = {"role": "user", "content": [{"toolResult": {"toolUseId": "1", "content": [{"text": "x" * 5000}], "status": "success"}}]}
    out = trim_history(
        [_u("q"), {"role": "assistant", "content": [{"toolUse": {"toolUseId": "1", "name": "t", "input": {}}}]}, big, _a("a")]
    )
    assert len(out[2]["content"][0]["toolResult"]["content"][0]["text"]) < 2100


def test_strip_tool_blocks_keeps_alternation():
    msgs = [
        _u("q"),
        {"role": "assistant", "content": [{"text": "checking"}, {"toolUse": {"toolUseId": "1", "name": "t", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "1", "content": [{"text": "res"}], "status": "success"}}]},
        _a("final"),
    ]
    out = strip_tool_blocks(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert all("toolUse" not in b and "toolResult" not in b for m in out for b in m["content"])


def test_display_messages():
    msgs = [
        _u("hi"),
        {"role": "assistant", "content": [{"text": "x"}, {"toolUse": {"toolUseId": "1", "name": "get_order_status", "input": {}}}]},
    ]
    assert display_messages(msgs) == [
        {"role": "user", "text": "hi", "tools": []},
        {"role": "assistant", "text": "x", "tools": ["get_order_status"]},
    ]


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="conv",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def test_store_is_partitioned_by_user(table):
    store = ConversationStore(table=table, ttl_days=7)
    store.save("alice", "c1", [_u("hello"), _a("hi")], "m1", title="hello")
    store.save("alice", "c1", [_u("hello"), _a("hi"), _u("more"), _a("ok")], "m1", title="ignored")
    assert store.get("bob", "c1") is None
    item = store.get("alice", "c1")
    assert item["title"] == "hello" and len(item["messages"]) == 4 and item["ttl"] > item["updated_at"]
    assert [c["conversationId"] for c in store.list("alice")] == ["c1"]
    assert store.list("bob") == []
    store.delete("alice", "c1")
    assert store.get("alice", "c1") is None
