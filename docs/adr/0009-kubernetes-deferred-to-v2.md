# ADR-0009: Kubernetes (EKS) variant deferred to v2

- Status: Accepted
- Date: 2026-11-24

v1 is serverless only. A v2 `AgenticAi-Eks` variant is planned to run the same Python services as
containers to compare operating models:

- EKS (Auto Mode) cluster in a dedicated VPC; agent and MCP server as Deployments behind an ALB
  (ALB supports streaming natively, so LWA is not needed).
- Same Cognito, DynamoDB, Knowledge Base and Bedrock resources reused from the v1 stacks.
- HPA on CPU/RPS, Karpenter-managed nodes, IRSA/Pod Identity for per-service IAM.
- OpenTelemetry collector → CloudWatch/X-Ray.

The service code was written to make this cheap: the agent is a plain ASGI app (`app:app`) and the
MCP server is a standard streamable-HTTP MCP app; only the Lambda wrappers are Lambda-specific.
