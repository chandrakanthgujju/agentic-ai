# Architecture Decision Records

Each ADR records one decision, the options considered, and the trade-offs accepted.
Status values: **Accepted**, **Superseded by ADR-NNNN**, **Proposed**.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-serverless-multi-stack-layout.md) | Serverless-first, seven CDK stacks split by lifecycle | Accepted |
| [0002](0002-cloudfront-instead-of-public-s3-website.md) | Serve the UI from a private S3 bucket through CloudFront, not a public S3 website | Accepted |
| [0003](0003-vector-store-s3-vectors.md) | Bedrock Knowledge Base on Amazon S3 Vectors | Accepted |
| [0004](0004-mcp-tool-server-on-lambda.md) | Tools are an MCP server on Lambda behind an IAM-auth Function URL | Accepted |
| [0005](0005-streaming-rest-api-lambda-web-adapter.md) | Stream tokens with API Gateway REST response streaming + Lambda Web Adapter | Accepted |
| [0006](0006-model-catalog-and-allowlist.md) | Three low-cost Bedrock models behind a server-side allowlist | Accepted |
| [0007](0007-authentication-and-authorization.md) | Cognito user pool, hosted UI + PKCE, `admin` group for KB management | Accepted |
| [0008](0008-load-management.md) | Load management: throttling layers, concurrency caps, backoff, queue-buffered ingestion | Accepted |
| [0009](0009-kubernetes-deferred-to-v2.md) | Kubernetes (EKS) variant deferred to v2 | Accepted |
