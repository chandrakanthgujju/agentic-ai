"""Lambda entry point: MCP Streamable HTTP server behind a Function URL (AWS_IAM).

The MCP SDK ships an ASGI (Starlette) app. Mangum translates Function URL events
to ASGI. Because the server runs in *stateless* mode with JSON responses, every
invocation is a self-contained JSON-RPC exchange, which is what lets it scale
horizontally on Lambda with no sticky sessions.

The SDK's session manager must be started exactly once per process and stay
running, so we start it on a module-level event loop during the cold start, and
Mangum reuses that same loop for every invocation.
"""

from __future__ import annotations

import asyncio
import os

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from mangum import Mangum
from mcp.server.transport_security import TransportSecuritySettings

from server import build_server

logger = Logger()
tracer = Tracer()
metrics = Metrics()

mcp = build_server()

# DNS-rebinding protection guards browser-reachable localhost servers. This endpoint
# is only reachable with a SigV4 signature (Function URL AWS_IAM), and its hostname
# is not known until deploy time, so Host-header allowlisting adds nothing here.
app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
_started = asyncio.Event()


async def _keep_session_manager_running() -> None:
    async with mcp.session_manager.run():
        _started.set()
        await asyncio.Event().wait()


_runner = _loop.create_task(_keep_session_manager_running())
_loop.run_until_complete(_started.wait())

_asgi_handler = Mangum(app, lifespan="off", api_gateway_base_path=os.environ.get("MCP_BASE_PATH", "/"))


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event, context):
    metrics.add_metric(name="McpRequests", unit=MetricUnit.Count, value=1)
    return _asgi_handler(event, context)
