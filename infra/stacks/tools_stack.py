"""MCP tool server: Lambda + Function URL protected by IAM (SigV4).

Only principals granted ``lambda:InvokeFunctionUrl`` on this function (today:
the agent's execution role) can call it. Nothing about it is reachable from a
browser, so there is no CORS and no bearer token to leak.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

from infra.components.service_function import ServiceFunction
from infra.config import AppConfig


class ToolsStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, config: AppConfig, knowledge_base_arn: str, knowledge_base_id: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.mcp = ServiceFunction(
            self,
            "McpServer",
            service="mcp_server",
            description=f"MCP (Streamable HTTP, stateless) tool server for {config.project_name}: mock commerce DB + KB search",
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "MOCK_DB_MIN_LATENCY_MS": "40",
                "MOCK_DB_MAX_LATENCY_MS": "180",
            },
            memory_size=512,
            timeout=Duration.seconds(20),
        )
        self.mcp.role.add_to_policy(iam.PolicyStatement(actions=["bedrock:Retrieve"], resources=[knowledge_base_arn]))

        self.url = self.mcp.function.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=_lambda.InvokeMode.BUFFERED,
        )

        CfnOutput(self, "McpServerUrl", value=self.url.url)
