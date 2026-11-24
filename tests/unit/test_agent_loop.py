"""Agent loop with a scripted fake Bedrock stream and fake MCP tool session."""

import asyncio
import contextlib
import json

import pytest
from botocore.exceptions import ClientError

from tests.conftest import load_service

agent_loop = load_service("agent", "agent_loop")
settings_mod = __import__("settings")
tool_gateway = __import__("tool_gateway")

from shared.model_catalog import ModelSpec  # noqa: E402

SPEC = ModelSpec(id="us.amazon.nova-micro-v1:0", label="Nova Micro", provider="Amazon", maxTokens=256)


def text_turn(text, stop="end_turn", usage=(10, 5)):
    events = [{"messageStart": {"role": "assistant"}}]
    for chunk in [text[i : i + 4] for i in range(0, len(text), 4)]:
        events.append({"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": chunk}}})
    events += [{"contentBlockStop": {"contentBlockIndex": 0}}, {"messageStop": {"stopReason": stop}}]
    events.append({"metadata": {"usage": {"inputTokens": usage[0], "outputTokens": usage[1]}}})
    return events


def tool_turn(calls, preamble="Let me check."):
    events = [{"messageStart": {"role": "assistant"}}, {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": preamble}}}]
    for i, (tid, name, args) in enumerate(calls, start=1):
        raw = json.dumps(args)
        events.append({"contentBlockStart": {"contentBlockIndex": i, "start": {"toolUse": {"toolUseId": tid, "name": name}}}})
        events.append({"contentBlockDelta": {"contentBlockIndex": i, "delta": {"toolUse": {"input": raw[:5]}}}})
        events.append({"contentBlockDelta": {"contentBlockIndex": i, "delta": {"toolUse": {"input": raw[5:]}}}})
        events.append({"contentBlockStop": {"contentBlockIndex": i}})
    events += [{"messageStop": {"stopReason": "tool_use"}}, {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 8}}}]
    return events


class FakeBedrock:
    def __init__(self, turns, fail_first=0):
        self.turns = list(turns)
        self.requests = []
        self.fail_first = fail_first

    def converse_stream(self, **req):
        if self.fail_first:
            self.fail_first -= 1
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ConverseStream")
        self.requests.append(json.loads(json.dumps(req)))
        return {"stream": iter(self.turns.pop(0))}


class FakeSession:
    def __init__(self, results=None, delay=0.0, specs=None, fail_list=False):
        self.results = results or {}
        self.calls = []
        self.delay = delay
        self.specs = (
            specs
            if specs is not None
            else [
                {
                    "toolSpec": {
                        "name": "get_order_status",
                        "description": "d",
                        "inputSchema": {"json": {"type": "object", "properties": {}}},
                    }
                }
            ]
        )
        self.fail_list = fail_list

    async def list_tool_specs(self):
        if self.fail_list:
            raise ConnectionError("mcp down")
        return self.specs

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        ok, text, data = self.results.get(name, (True, '{"ok": true}', None))
        return tool_gateway.ToolResult(ok, text, data)


class FakeGateway:
    def __init__(self, session):
        self.session = session
        self.closed = False

    @contextlib.asynccontextmanager
    async def open(self):
        try:
            yield self.session
        finally:
            self.closed = True


def run(loop, history=None, text="hi"):
    async def go():
        return [e async for e in loop.run(SPEC, history or [], text)]

    return asyncio.run(go())


def make(bedrock, gateway=None, **overrides):
    s = settings_mod.AgentSettings(**{"max_iterations": 5, "tool_timeout_s": 1.0, **overrides})
    return agent_loop.AgentLoop(bedrock, gateway, s, sleep=lambda _: None)


def test_plain_answer_streams_text_and_usage():
    bedrock = FakeBedrock([text_turn("Hello there!")])
    loop = make(bedrock, FakeGateway(FakeSession()))
    events = run(loop)
    assert "".join(e.data["delta"] for e in events if e.type == "text") == "Hello there!"
    done = events[-1]
    assert done.type == "done" and done.data["usage"] == {"inputTokens": 10, "outputTokens": 5}
    assert loop.outcome.messages[-1] == {"role": "assistant", "content": [{"text": "Hello there!"}]}
    assert bedrock.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_order_status"


def test_tool_round_trip_then_answer():
    session = FakeSession(results={"get_order_status": (True, '{"status": "shipped"}', {"status": "shipped"})})
    gateway = FakeGateway(session)
    bedrock = FakeBedrock([tool_turn([("t1", "get_order_status", {"order_id": "ORD-5001"})]), text_turn("It shipped.")])
    loop = make(bedrock, gateway)
    events = run(loop, text="where is ORD-5001?")
    types = [e.type for e in events]
    assert types.index("tool_use") < types.index("tool_result") < len(types) - 1
    assert session.calls == [("get_order_status", {"order_id": "ORD-5001"})]
    second = bedrock.requests[1]["messages"]
    assert second[-2]["content"][1]["toolUse"]["input"] == {"order_id": "ORD-5001"}
    assert second[-1]["content"][0]["toolResult"] == {
        "toolUseId": "t1",
        "content": [{"text": '{"status": "shipped"}'}],
        "status": "success",
    }
    assert loop.outcome.tool_calls == 1 and loop.outcome.iterations == 2
    assert gateway.closed


def test_parallel_tool_calls_and_error_status():
    session = FakeSession(results={"b": (False, "not found", None)})
    bedrock = FakeBedrock([tool_turn([("t1", "a", {}), ("t2", "b", {})]), text_turn("done")])
    events = run(make(bedrock, FakeGateway(session)))
    results = {e.data["id"]: e.data["ok"] for e in events if e.type == "tool_result"}
    assert results == {"t1": True, "t2": False}
    blocks = bedrock.requests[1]["messages"][-1]["content"]
    assert [b["toolResult"]["status"] for b in blocks] == ["success", "error"]


def test_iteration_cap_stops_runaway_tool_use_and_closes_history():
    bedrock = FakeBedrock([tool_turn([(f"t{i}", "a", {})]) for i in range(3)])
    loop = make(bedrock, FakeGateway(FakeSession()), max_iterations=2)
    events = run(loop)
    assert any(e.type == "warning" and "iteration cap" in e.data["message"] for e in events)
    assert loop.outcome.stop_reason == "iteration_cap"
    assert loop.outcome.messages[-1]["role"] == "assistant"  # history stays valid for the next turn


def test_tool_timeout_becomes_error_result():
    session = FakeSession(delay=0.5)
    bedrock = FakeBedrock([tool_turn([("t1", "slow", {})]), text_turn("sorry")])
    events = run(make(bedrock, FakeGateway(session), tool_timeout_s=0.05))
    result = next(e for e in events if e.type == "tool_result")
    assert result.data["ok"] is False
    assert "timed out" in bedrock.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]


def test_tool_output_is_truncated():
    session = FakeSession(results={"big": (True, "x" * 500, None)})
    bedrock = FakeBedrock([tool_turn([("t1", "big", {})]), text_turn("ok")])
    run(make(bedrock, FakeGateway(session), max_tool_result_chars=100))
    sent = bedrock.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert len(sent) < 130 and sent.endswith("[truncated]")


def test_throttling_is_retried_with_backoff():
    bedrock = FakeBedrock([text_turn("after retry")], fail_first=2)
    events = run(make(bedrock, FakeGateway(FakeSession())))
    assert "".join(e.data["delta"] for e in events if e.type == "text") == "after retry"


def test_tools_down_degrades_and_strips_tool_history():
    history = [
        {"role": "user", "content": [{"text": "q"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "x", "name": "a", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "x", "content": [{"text": "r"}], "status": "success"}}]},
        {"role": "assistant", "content": [{"text": "answer"}]},
    ]
    bedrock = FakeBedrock([text_turn("fine")])
    events = run(make(bedrock, FakeGateway(FakeSession(fail_list=True))), history=history)
    assert events[0].type == "warning"
    req = bedrock.requests[0]
    assert "toolConfig" not in req
    assert all("toolUse" not in b and "toolResult" not in b for m in req["messages"] for b in m["content"])


def test_guardrail_config_is_attached_when_configured():
    bedrock = FakeBedrock([text_turn("ok")])
    run(make(bedrock, None, guardrail_id="gr-1", guardrail_version="1"))
    assert bedrock.requests[0]["guardrailConfig"] == {
        "guardrailIdentifier": "gr-1",
        "guardrailVersion": "1",
        "streamProcessingMode": "async",
    }


def test_non_retryable_error_propagates():
    class Broken:
        def converse_stream(self, **_):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no model access"}}, "ConverseStream")

    with pytest.raises(ClientError):
        run(make(Broken(), None))


def test_tool_config_rejection_falls_back_to_answer_without_tools():
    class RejectsTools(FakeBedrock):
        def converse_stream(self, **req):
            if "toolConfig" in req:
                self.requests.append(json.loads(json.dumps(req)))
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "The tool configuration is invalid."}}, "ConverseStream"
                )
            return super().converse_stream(**req)

    bedrock = RejectsTools([text_turn("Answer without tools.")])
    metrics = []
    loop = make(bedrock, FakeGateway(FakeSession()))
    loop.on_metric = lambda name, value, dims: metrics.append(name)
    events = run(loop)
    assert [e.type for e in events if e.type in ("warning", "done")] == ["warning", "done"]
    assert "".join(e.data["delta"] for e in events if e.type == "text") == "Answer without tools."
    assert "toolConfig" in bedrock.requests[0] and "toolConfig" not in bedrock.requests[1]
    assert "ToolConfigRejected" in metrics


def test_validation_error_without_tools_still_propagates():
    class Rejects:
        def converse_stream(self, **_):
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "ConverseStream")

    loop = make(Rejects(), None)
    with pytest.raises(ClientError):
        run(loop)


def test_empty_tool_output_is_never_sent_as_empty_text():
    bedrock = FakeBedrock([tool_turn([("t1", "get_order_status", {"order_id": "ORD-5001"})]), text_turn("ok")])
    loop = make(bedrock, FakeGateway(FakeSession(results={"get_order_status": (True, "", None)})))
    run(loop)
    tr = bedrock.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert tr["content"] == [{"text": "(empty result)"}]
