# Cost guide

All numbers are **estimates** using us-east-1 on-demand list prices checked in November 2026.
Always confirm on the AWS pricing pages and set `monthlyBudgetUsd` in `cdk.json`.

## Fixed monthly cost (idle)

| Item | Approx. per month |
| --- | --- |
| KMS customer-managed keys (data, RAG, alarm topic) | ~$3 ($1 per key) |
| CloudWatch dashboard (first 3 free) and ~15 alarms (first 10 free) | ~$0.50 |
| CloudWatch Logs storage (1-month retention) | cents |
| WAF, if `enableWaf` (2 web ACLs + rules) | ~$12+ |
| Everything else (Lambda, API Gateway, DynamoDB, S3, S3 Vectors, CloudFront) | ~$0 when idle |

## Variable cost drivers

| Driver | Price (us-east-1) | Notes |
| --- | --- | --- |
| Nova Micro | $0.035 / 1M input tokens, $0.14 / 1M output | Cheapest option |
| Nova Lite | $0.06 / 1M input, $0.24 / 1M output | Default |
| Claude Haiku 4.5 | $1 / 1M input, $5 / 1M output | ~15-20x Nova Lite |
| Titan Text Embeddings v2 | $0.02 / 1M tokens | Ingestion and each KB query |
| S3 Vectors | $0.06 / GB-month storage, $0.20 / GB written, $2.50 / 1M queries + data processed | Tiny for a few hundred docs |
| Guardrails | Charged per text unit evaluated | Can exceed model cost for Nova models on short chats; set `enableGuardrail: false` to compare |
| Lambda arm64 | Per GB-second + per request | The agent (1 GB) is billed while it waits on Bedrock |
| API Gateway REST | Per million requests | Streaming responses are billed per request plus data transfer |

## Worked example (illustrative)

1,000 chats/month, each ~3 model calls of ~3,000 input + 300 output tokens on Nova Lite, 1 KB query:

- Model: 1,000 × 3 × (3,000 × $0.06 + 300 × $0.24) / 1M ≈ **$0.76**
- Agent Lambda: 1,000 × ~8 s × 1 GB ≈ 8,000 GB-s ≈ **$0.11**
- Guardrail: depends on text volume; budget a few dollars and check Cost Explorer
- Everything else: cents

The same traffic on Claude Haiku 4.5 is roughly **$14** in model cost.

## Cost controls built in

- Model allowlist and capped `maxTokens` per model.
- Throttling at API Gateway; optional agent reserved concurrency.
- Conversation history trimmed before each model call; DynamoDB TTL.
- Optional AWS Budget with email alerts at 80% of actual and 100% of forecasted spend.
