# Security model

This document lists the threats considered for v1 and the control for each. Controls marked
**(optional)** are off by default to keep a personal deployment cheap; turn them on in `cdk.json`.

## Identity and access

| Concern | Control | Where |
| --- | --- | --- |
| Who can use the app | Cognito user pool, self sign-up **disabled**; users are created by an admin | `infra/stacks/auth_stack.py` |
| Login flow | Hosted UI, authorization code + **PKCE**, public client without secret, callback = app URL only | `infra/stacks/frontend_stack.py`, `frontend/js/auth.js` |
| Passwords / MFA | 12+ chars with all classes; optional TOTP MFA; email-only recovery | `auth_stack.py` |
| API authentication | API Gateway Cognito authorizer on every `/api/*` method | `api_stack.py` |
| KB admin authorization | `admin` group required, enforced server-side from verified claims | `services/kb_admin/handler.py` |
| Tenant isolation | Conversations keyed by `USER#<sub>`; no route accepts a user id from the client | `services/shared/conversations.py` |
| Service-to-service | MCP Function URL uses `AWS_IAM`; only the agent role may invoke it; requests SigV4-signed | `tools_stack.py`, `services/shared/sigv4.py` |
| Least privilege | One IAM role per function; Bedrock invoke limited to allowlisted model ARNs; KB admin limited to the `docs/` prefix; MCP may only `bedrock:Retrieve` on one KB | `api_stack.py`, `rag_stack.py`, `tools_stack.py` |

Every remaining wildcard in IAM (for example `s3:*Object` on `docs/*`, Lambda log streams, X-Ray)
is acknowledged with a written reason via cdk-nag in `infra/nag.py`. Synth fails on any new,
unacknowledged AwsSolutions finding.

## Data protection

- Customer-managed KMS keys (rotation on) for the conversations table, the docs bucket and ingestion
  queues, and the alarm topic.
- DynamoDB point-in-time recovery; TTL deletes conversations after `conversationTtlDays` (30).
- All buckets: Block Public Access and a TLS-only bucket policy. The site and docs buckets write
  server access logs to dedicated log buckets; the docs bucket is versioned.
- `retainData: false` (default) destroys data on `cdk destroy` — right for a demo, wrong for
  production. Set it to `true` before storing anything you care about.

## Edge and browser

- Site bucket is private; CloudFront reads it with Origin Access Control (ADR-0002).
- Response headers policy: strict CSP (`default-src 'self'`; `connect-src` limited to self, the Cognito
  domain and the docs bucket for uploads; `frame-ancestors 'none'`), HSTS, `X-Content-Type-Options`,
  `Referrer-Policy`, `X-Frame-Options: DENY`.
- The UI renders model output with `textContent` only — no `innerHTML` — so model or document
  content cannot inject markup. No inline scripts, no third-party scripts or CDNs.
- Tokens live in `sessionStorage` (see ADR-0007 for the trade-off and the BFF alternative).
- **(optional)** WAF on CloudFront and the API stage: per-IP rate limit, AWS IP reputation list,
  Common Rule Set, Known Bad Inputs.

## LLM-specific risks

| Risk | Mitigation |
| --- | --- |
| Prompt injection via user input | Bedrock Guardrail with `PROMPT_ATTACK` filter (HIGH) on input; content filters on input and output |
| Indirect prompt injection via tool results or retrieved documents | System prompt marks tool output as data, not instructions; tools are **read-only**; the agent cannot call anything that writes; KB uploads are admin-only |
| Excessive agency / runaway loops | ≤5 model iterations, 90 s deadline, 15 s per tool, capped `maxTokens` |
| Model choice abuse (cost) | Server-side model allowlist + IAM limited to those models |
| Data leakage between users | Per-user partition keys; tools only expose mock data |
| Large/malicious uploads | Presigned POST with exact `Content-Type`, `content-length-range` ≤ 50 MB, filename regex, extension allowlist, 5-minute expiry, prefix-scoped key |
| Logging sensitive content | Application logs record ids, sizes, timings and tool names — not message bodies. API access logs omit request bodies |

Known limits:

- Guardrails reduce, but do not eliminate, prompt injection. Treat tool output as untrusted when you
  add tools that write data: require confirmation from the user and enforce authorization in the tool
  itself, using the caller identity — not the model's claims.
- The mock tools return the same data to every user. A real deployment must pass the caller's
  identity to the MCP server (e.g. a signed header) and authorize each query.

## Supply chain

- Python dependencies pinned in every `requirements.txt`; Lambda bundles built with
  `--only-binary=:all:` for the exact target platform.
- Lambda Web Adapter comes from AWS's public layer account `753240598075`, pinned by version.
- CI runs lint, tests and `cdk synth` (with cdk-nag) on every PR and has **no AWS credentials**.

## Reporting

Open a private security advisory on the GitHub repository rather than a public issue.
