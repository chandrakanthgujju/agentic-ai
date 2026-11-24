# ADR-0003: Bedrock Knowledge Base on Amazon S3 Vectors

- Status: Accepted
- Date: 2026-11-24

## Context

RAG needs a vector store that a hobby account can afford to leave running, that Bedrock Knowledge
Bases manages end to end (parse, chunk, embed, index, retrieve), and that CDK can create.

## Options

| Option | Idle cost | Ops | Notes |
| --- | --- | --- | --- |
| OpenSearch Serverless (classic collections) | Minimum OCUs bill 24/7 (~$175-350/month in us-east-1) | Low | Hybrid search, filters; the classic Bedrock KB default |
| OpenSearch Serverless (newer scale-to-zero collection groups) | ~$0 idle, but a cold resume can take tens of seconds | Low | Worth re-evaluating when hybrid search is needed |
| Aurora PostgreSQL Serverless v2 + pgvector | ~$44/month at 0.5 ACU, or ~$0 auto-paused with a ~15 s resume; VPC required | Medium | Familiar SQL; needs schema bootstrap |
| Pinecone / Redis / MongoDB Atlas | Third-party account and secret | Medium | Off-AWS dependency |
| **Amazon S3 Vectors (chosen)** | $0 floor; pay per GB stored ($0.06/GB-month), per query ($2.50/M) and data processed | Lowest | Native Bedrock KB support; CloudFormation `AWS::S3Vectors::VectorBucket` / `Index` |

Prices are us-east-1 list prices as of Nov 2026; check the AWS pricing pages before relying on them.

## Decision

Bedrock Knowledge Base with `storageConfiguration.type = S3_VECTORS`:

- Vector bucket + index created in `AgenticAi-Rag` (`float32`, 1024 dimensions, cosine).
- `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` are declared non-filterable metadata keys, so
  chunk text does not count against the filterable-metadata size limit.
- Embeddings: `amazon.titan-embed-text-v2:0` at 1024 dimensions.
- Chunking: fixed size, 300 tokens with 20% overlap. Simple and predictable; semantic/hierarchical
  chunking can be switched on in `rag_stack.py` if retrieval quality needs it.
- Source documents live in a separate, KMS-encrypted, versioned S3 bucket under `docs/`.

## Consequences

- Near-zero idle cost; good for spiky, low-QPS RAG.
- S3 Vectors query latency is higher than an in-memory index (sub-second rather than tens of ms) and
  it does not offer hybrid (keyword + vector) search. For a support-style assistant this is acceptable.
- Migrating later (e.g. to OpenSearch for hybrid search) is a new Knowledge Base plus a re-sync; the
  `search_knowledge_base` MCP tool hides the change from the agent.
- **Not verified on a live account by the author of this PR** — see README "Verification status".
