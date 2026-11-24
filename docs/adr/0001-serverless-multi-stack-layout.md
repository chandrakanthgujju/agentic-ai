# ADR-0001: Serverless-first, seven CDK stacks split by lifecycle

- Status: Accepted
- Date: 2026-11-24

## Context

The goal is a chatbot a single developer can `cdk deploy` into a personal account, that costs close
to nothing when idle, and that still shows how a production team would structure it. The traffic
pattern is spiky and mostly idle. The team (one person) cannot operate clusters or databases.

## Decision

Use only managed, pay-per-request services (Lambda, API Gateway, DynamoDB on-demand, S3, S3 Vectors,
Bedrock, Cognito, CloudFront) and write all infrastructure in CDK (Python). Split it into stacks by
**rate of change and blast radius**:

| Stack | Owns | Changes |
| --- | --- | --- |
| `AgenticAi-Auth` | Cognito user pool, `admin` group | Rarely; deleting it logs everyone out |
| `AgenticAi-Data` | Conversations table, its KMS key | Rarely; stateful |
| `AgenticAi-Rag` | Docs bucket, vector bucket/index, Knowledge Base, ingestion pipeline | Occasionally; stateful |
| `AgenticAi-Tools` | MCP server Lambda + Function URL | Often (new tools) |
| `AgenticAi-Api` | REST API, agent, conversations API, KB admin API, guardrail | Often |
| `AgenticAi-Frontend` | Site bucket, CloudFront, Cognito app client + domain, UI deployment | Often |
| `AgenticAi-Observability` | Dashboard, alarms, SNS topic, budget | Occasionally |

Stateful stacks sit at the bottom of the dependency graph so that redeploying application code never
touches data. Cross-stack references use CDK's default "strong" exports (set explicitly in `cdk.json`).

## Consequences

- Idle cost is close to zero apart from KMS keys (~$1/key/month) and CloudWatch storage.
- `cdk deploy --all` takes ~10-15 minutes the first time (CloudFront dominates).
- Strong exports mean you cannot remove an output a downstream stack consumes in a single deploy;
  this is the usual CDK two-step and is documented in `docs/operations.md`.
- Lambda limits (15-minute max, cold starts, 250 MB unzipped package) apply. The agent bundle is ~86 MB.
