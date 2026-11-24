# ADR-0006: Three low-cost Bedrock models behind a server-side allowlist

- Status: Accepted
- Date: 2026-11-24

## Decision

The UI offers three on-demand, low-cost models with tool-use support, invoked through US
cross-region inference profiles (default region `us-east-1`):

| Model | ID | Why |
| --- | --- | --- |
| Amazon Nova Micro | `us.amazon.nova-micro-v1:0` | Cheapest and fastest; text only |
| Amazon Nova Lite (default) | `us.amazon.nova-lite-v1:0` | Good quality/cost balance |
| Anthropic Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Most reliable tool use of the three |

"Deployed in Bedrock" here means **enabled and invocable**: these are serverless, pay-per-token
models; there is nothing to provision (Provisioned Throughput would cost thousands per month).

- The list lives in `cdk.json` (`agentic:config.models`) and is passed to Lambdas as `MODELS_JSON`.
- The client sends a model ID (or none, meaning the default); the server resolves it against the
  allowlist and rejects anything else with HTTP 400 — a caller cannot invoke arbitrary models.
- IAM grants `bedrock:InvokeModel*` only on those inference-profile ARNs and their underlying
  foundation models (any region, as cross-region profiles route between US regions).
- All calls use the Converse API, so adding/removing a model is a config change, not a code change.

## Consequences

- You must enable access to these models in the Bedrock console (Anthropic models also require the
  one-time use-case form). Without access, the UI shows "The model call failed" for that model.
- Inference profile IDs differ outside the US (`eu.`/`apac.` prefixes); change them with the region.
