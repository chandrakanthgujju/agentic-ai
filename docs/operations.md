# Operations runbook

## Day-1 checklist

1. Set `alarmEmail` in `cdk.json` (or `-c alarmEmail=you@example.com`) and confirm the SNS email.
2. Optionally set `monthlyBudgetUsd` to get budget notifications (80% actual, 100% forecast; requires `alarmEmail`).
3. Create yourself a user and add it to `admin` (README, step 6).
4. Upload the files in `sample-docs/` from the **Knowledge base** tab and wait for the job to finish.
5. Open the `agentic-ai-ops` CloudWatch dashboard.

## What to watch

| Signal | Where | Normal | Action when abnormal |
| --- | --- | --- | --- |
| API 4xx | Dashboard, `ApiThrottled` alarm | Low | Spikes of 429 = stage throttling; raise limits in `cdk.json` *after* raising Bedrock quotas |
| API 5xx | `Api5xx` alarm | ~0 | Check the agent / API Lambda logs by `requestId` |
| `AgentErrors` | `AgentModelErrors` alarm | ~0 | Usually model access not enabled, Bedrock throttling, or guardrail misconfiguration |
| `BedrockRetries` | Dashboard | ~0 | Sustained retries = you are at the Bedrock TPM/RPM quota; request an increase |
| `ToolErrors`, `ToolLatencyMs` | Dashboard | Low / < 1 s | MCP Lambda logs; mock latency is set by `MOCK_DB_*_LATENCY_MS` |
| Ingestion DLQ depth | `IngestionDLQ` alarm | 0 | Inspect messages; re-run a sync from the KB tab; then purge/redrive the DLQ |
| Lambda throttles | Per-function alarms | 0 | Account concurrency exhausted or `agentReservedConcurrency` too low |

Logs are JSON (Powertools). Useful CloudWatch Logs Insights query for the agent log group:

```
fields @timestamp, level, message, conversation_id, model, error_code
| filter level = "ERROR" or message = "chat complete"
| sort @timestamp desc
| limit 50
```

Traces: X-Ray shows API Gateway → agent → Bedrock / DynamoDB / MCP Function URL.

## Diagnosing chat errors

The UI shows the error code and a sanitized copy of Bedrock's message (ARNs, account ids and
access-key ids removed, max 300 characters). The agent logs the full error. Each function has its
own CDK-named log group; find the agent's:

```bash
aws logs describe-log-groups --query "logGroups[?contains(logGroupName,'AgenticAi-Api-Agent')].logGroupName" --output text
```

Then run this in CloudWatch Logs Insights against that group:

```
fields @timestamp, model, error_code, error_message, @message
| filter message in ["agent failed", "model rejected request with tools; retrying without tools", "skipping MCP tool that Bedrock would reject"]
| parse @message /operation: (?<bedrock_reason>[^"\\]+)/
| display @timestamp, message, model, error_code, bedrock_reason, error_message
| sort @timestamp desc
| limit 50
```

`bedrock_reason` is Bedrock's own explanation, e.g. which `toolConfig...` or `messages...` field it
rejected. `AccessDeniedException` means model access or IAM; `ValidationException` means the request
shape; `ThrottlingException` means quota.

## Knowledge-base maintenance

- **Add/replace a document**: upload from the KB tab. The S3 event is queued and an ingestion job
  starts within ~1 minute (batching window). Re-uploading the same name replaces it.
- **Delete a document**: delete from the KB tab. The next ingestion job removes its vectors.
- **Manual sync**: "Sync now" starts a job immediately; if one is already running you get HTTP 409.
- **Bulk load**: `aws s3 sync ./my-docs s3://<DocumentsBucket>/docs/` — events are batched into a
  small number of ingestion jobs.
- **Job history and failures**: shown in the KB tab (last 5 jobs with statistics), and in the
  Bedrock console under the Knowledge Base's data source.
- **Re-embedding with a different model or chunking**: create a new Knowledge Base (change in
  `rag_stack.py`), let it sync, then switch the MCP tool's `KNOWLEDGE_BASE_ID`. Do not change the
  embedding model of an existing index in place — dimensions and vector spaces differ.

## MCP tool lifecycle

1. Add a function decorated with `@mcp.tool(...)` in `services/mcp_server/server.py`, with a typed
   signature, a docstring the model will read, and `READ_ONLY` annotations if appropriate.
2. Add unit tests in `tests/unit/test_mcp_server.py`.
3. Bump `CATALOG_VERSION` (semver: new tool = minor, changed/removed tool = major).
4. `cdk deploy AgenticAi-Tools`. Agents pick up the new list within 5 minutes (tool cache TTL).
5. Removing a tool: deprecate first (description says so), remove in the next major version.

## Changing load limits

All in `cdk.json` → `agentic:config` (or `-c key=value` at deploy time):
`apiRateLimit`, `apiBurstLimit`, `chatRateLimit`, `chatBurstLimit`, `agentReservedConcurrency`.
Raise Bedrock quotas first (Service Quotas → Amazon Bedrock → "On-demand ... tokens per minute"
for each model), otherwise extra requests only turn into retries and errors.

## Load testing (not run for this PR)

Suggested approach with [k6](https://k6.io/) or [Locust](https://locust.io/):

1. Create a test user, obtain an ID token (`aws cognito-idp admin-initiate-auth` with
   `ADMIN_USER_PASSWORD_AUTH` on a separate, test-only app client).
2. Ramp `POST /api/chat` from 1 to 20 virtual users with Nova Micro.
3. Expect HTTP 429s once you pass `chatRateLimit`; watch `BedrockRetries` and `AgentErrors`.
4. Record p50/p90 time-to-first-token from the client and `ChatLatencyMs` from the dashboard.

## Deploying changes safely

- `make synth` before every deploy; `cdk diff` to see what will change.
- Deploy only what changed: `cdk deploy AgenticAi-Api` for agent code, `AgenticAi-Tools` for tools,
  `AgenticAi-Frontend` for UI.
- Removing an output used by another stack needs two deploys (keep the export → deploy consumer
  without it → remove the export).

## Teardown

`make destroy` (or `cdk destroy --all`). With `retainData: false` the buckets, table, vector
bucket and user pool are deleted. KMS keys enter their 7-30 day pending-deletion window. The
CloudFront distribution takes several minutes to disable and delete.
