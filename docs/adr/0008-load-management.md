# ADR-0008: Load management

- Status: Accepted
- Date: 2026-11-24

## Context

The scarce resources are Bedrock tokens-per-minute quotas (low by default on new accounts), money, and
the Knowledge Base's one-ingestion-job-at-a-time rule. Lambda and DynamoDB scale far beyond them.

## Decision — layered protection, outermost first

1. **WAF (optional, `enableWaf`)**: per-IP rate rule + AWS managed rule groups, on CloudFront and API.
2. **API Gateway stage throttling**: 20 rps / 40 burst for the API; `POST /api/chat` 5 rps / 10 burst
   (configurable). Excess gets HTTP 429 before any Lambda runs.
3. **Request validation**: body schema at API Gateway, plus a 16 KB body cap and `extra=forbid`
   model in the app.
4. **Optional reserved concurrency for the agent** (`agentReservedConcurrency`) to cap Bedrock spend
   and protect the account's shared Lambda pool.
5. **Bounded agent loop**: ≤5 model/tool iterations, 90 s wall-clock budget, per-tool 15 s timeout,
   capped `maxTokens`, history trimmed to the last 24 messages with tool results shrunk.
6. **Retries with full-jitter backoff** on Bedrock throttling, only *before* a stream starts (retrying
   mid-stream would duplicate output), on top of botocore's adaptive retry mode.
7. **Graceful degradation**: if MCP is unavailable the agent answers without tools and tells the UI.
8. **Queue-buffered ingestion**: S3 events → EventBridge → SQS (60 s batching window, max 2
   concurrent pollers) → one `StartIngestionJob` per batch with an idempotent client token;
   "job already running" leaves messages on the queue for retry; poison messages go to a DLQ (alarmed).
9. **On-demand DynamoDB** with TTL on conversations so storage does not grow unbounded.

## Consequences

- Under a burst, users see 429s from API Gateway rather than slow, expensive failures.
- To raise capacity: request higher Bedrock quotas first, then raise the throttles in `cdk.json`.
- A load test plan is in `docs/operations.md`; no load test was run for this PR.
