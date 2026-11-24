# ADR-0005: Stream tokens with API Gateway REST response streaming + Lambda Web Adapter

- Status: Accepted
- Date: 2026-11-24

## Context

Chat UX needs tokens to appear as they are generated. Constraints: Python Lambdas, Cognito-protected
API, served under the CloudFront origin.

Facts that shaped the decision (checked against AWS docs in Nov 2026):

- Lambda response streaming is natively supported only by the Node.js managed runtime (and custom
  runtimes); the Python managed runtime cannot stream by itself.
- API Gateway REST APIs support response streaming to Lambda (`responseTransferMode: STREAM`,
  invoked through the `response-streaming-invocations` API), with integration timeouts above 29 s.
- AWS Lambda Web Adapter (LWA) runs as a layer, starts a normal HTTP server inside the function and,
  with `AWS_LWA_INVOKE_MODE=response_stream`, streams its response back through Lambda.

## Options

1. Buffered REST/HTTP API response (no streaming). Simple; 29 s ceiling on HTTP APIs; poor UX.
2. WebSocket API. Real streaming, but connection-state table, more moving parts, separate auth model.
3. Function URL with `RESPONSE_STREAM`. Would need a non-Python runtime or LWA anyway, and Function
   URLs cannot use a Cognito authorizer (auth would be in code).
4. **REST API streaming + LWA + Starlette/uvicorn (chosen).**

## Decision

- Agent Lambda: Python 3.12 on arm64 with the public LWA layer
  (`arn:aws:lambda:<region>:753240598075:layer:LambdaAdapterLayerArm64:30`, configurable),
  `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`, handler `run.sh` launching uvicorn on 127.0.0.1:8080.
- `POST /api/chat` integration uses `ResponseTransferMode.STREAM` with a 120 s timeout. Other routes
  stay buffered.
- The app emits Server-Sent Events: `meta`, `text`, `tool_use`, `tool_result`, `warning`, `error`,
  `done`. The browser reads them with `fetch` + `ReadableStream` (EventSource cannot send headers).
- Cognito claims arrive from API Gateway in the `x-amzn-request-context` header that LWA forwards.
- CloudFront forwards `/api/*` uncached with compression off and a 60 s origin read timeout (which is
  measured between bytes, so long streams are fine as long as tokens keep flowing).

## Consequences

- No WebSocket state to manage; the same Cognito authorizer protects streaming and non-streaming routes.
- Streaming through CloudFront → REST API → LWA is the least-proven part of this design and was
  **not exercised against real AWS in this PR**. If streaming does not work in your account, the
  app still functions: the browser simply receives the whole SSE body at the end.
- Using a public layer means trusting the LWA publisher account; pin the version and review upgrades.
