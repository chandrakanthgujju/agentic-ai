#!/usr/bin/env python3
"""CDK app entry point: ``cdk deploy --all`` from the repository root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_cdk as cdk  # noqa: E402
from cdk_nag import AwsSolutionsChecks  # noqa: E402

from infra.config import AppConfig  # noqa: E402
from infra.nag import suppress_api, suppress_auth, suppress_common, suppress_frontend, suppress_rag  # noqa: E402
from infra.stacks.api_stack import ApiStack  # noqa: E402
from infra.stacks.auth_stack import AuthStack  # noqa: E402
from infra.stacks.data_stack import DataStack  # noqa: E402
from infra.stacks.frontend_stack import FrontendStack  # noqa: E402
from infra.stacks.observability_stack import ObservabilityStack  # noqa: E402
from infra.stacks.rag_stack import RagStack  # noqa: E402
from infra.stacks.tools_stack import ToolsStack  # noqa: E402


def build_app(app: cdk.App | None = None) -> cdk.App:
    app = app or cdk.App()
    config = AppConfig.from_node(app.node)
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    )
    p = config.project_name.title().replace("-", "")

    auth = AuthStack(app, f"{p}-Auth", config=config, env=env, description="Cognito user pool")
    data = DataStack(app, f"{p}-Data", config=config, env=env, description="KMS key and conversation table")
    rag = RagStack(app, f"{p}-Rag", config=config, env=env, description="Knowledge base (S3 Vectors) and ingestion")
    tools = ToolsStack(
        app,
        f"{p}-Tools",
        config=config,
        env=env,
        description="MCP tool server",
        knowledge_base_arn=rag.knowledge_base.attr_knowledge_base_arn,
        knowledge_base_id=rag.knowledge_base.attr_knowledge_base_id,
    )
    api = ApiStack(
        app,
        f"{p}-Api",
        config=config,
        env=env,
        description="REST API, agent and admin functions",
        user_pool=auth.user_pool,
        conversations=data.conversations,
        docs_bucket=rag.docs_bucket,
        rag_key=rag.key,
        knowledge_base_id=rag.knowledge_base.attr_knowledge_base_id,
        knowledge_base_arn=rag.knowledge_base.attr_knowledge_base_arn,
        data_source_id=rag.data_source.attr_data_source_id,
        mcp_function=tools.mcp.function,
        mcp_url=tools.url.url,
    )
    frontend = FrontendStack(
        app,
        f"{p}-Frontend",
        config=config,
        env=env,
        description="CloudFront + S3 web UI and Cognito app client",
        user_pool=auth.user_pool,
        api=api.api,
        docs_bucket_name=rag.docs_bucket.bucket_name,
    )
    obs = ObservabilityStack(
        app,
        f"{p}-Observability",
        config=config,
        env=env,
        description="Dashboard, alarms and budget",
        api=api.api,
        functions={
            "Agent": api.agent.function,
            "ConversationsApi": api.api_fn.function,
            "KbAdmin": api.kb_admin.function,
            "McpServer": tools.mcp.function,
            "Ingestion": rag.ingestion_fn.function,
        },
        dlq=rag.dlq,
    )

    for stack in (auth, data, rag, tools, api, frontend, obs):
        cdk.Tags.of(stack).add("project", config.project_name)
        cdk.Tags.of(stack).add("managed-by", "cdk")
        suppress_common(stack)
    suppress_auth(auth)
    suppress_rag(rag)
    suppress_api(api, config.enable_waf)
    suppress_frontend(frontend, config.enable_waf)

    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    return app


if __name__ == "__main__":
    build_app().synth()
