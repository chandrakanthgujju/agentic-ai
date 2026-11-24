"""Render the architecture diagrams in this folder.

    pip install -r requirements-dev.txt   # includes `diagrams`
    sudo apt-get install graphviz          # or: brew install graphviz
    python docs/architecture/render_diagrams.py   # or: make diagrams

The PNGs are committed so the README renders on GitHub without a build step.
Every node maps to a construct in ``infra/stacks``; keep them in sync.
"""

from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.cost import Budgets
from diagrams.aws.database import DynamodbTable
from diagrams.aws.devtools import XRay
from diagrams.aws.general import Users
from diagrams.aws.integration import SNS, SQS, Eventbridge
from diagrams.aws.management import Cloudwatch, CloudwatchAlarm, CloudwatchLogs
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway, CloudFront
from diagrams.aws.security import KMS, WAF, Cognito, IAMRole
from diagrams.aws.storage import S3

OUT = Path(__file__).resolve().parent

GRAPH = {"fontsize": "20", "pad": "0.4", "splines": "spline", "nodesep": "0.7", "ranksep": "1.0"}
NODE = {"fontsize": "12"}
CLUSTER = {"fontsize": "14"}

SYNC = {"color": "#1f6feb"}
ASYNC = {"color": "#b35900", "style": "dashed"}
SECURITY = {"color": "#8250df", "style": "dotted"}
TELEMETRY = {"color": "#6e7781", "style": "dotted"}


def production_architecture() -> None:
    with Diagram(
        "agentic-ai v1 - serverless production architecture (us-east-1)",
        filename=str(OUT / "production-architecture"),
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
    ):
        users = Users("Chat users\n& KB admins")

        with Cluster("Edge (global)", graph_attr=CLUSTER):
            waf_edge = WAF("WAF (optional)\nrate limit + managed rules")
            cdn = CloudFront("CloudFront\nHTTPS, security headers\n/api/* -> API GW")

        with Cluster("AgenticAi-Frontend", graph_attr=CLUSTER):
            site = S3("Private site bucket\n(OAC only, SSE-S3)")

        with Cluster("AgenticAi-Auth", graph_attr=CLUSTER):
            cognito = Cognito("Cognito user pool\nhosted UI, PKCE,\n'admin' group")

        with Cluster("AgenticAi-Api", graph_attr=CLUSTER):
            waf_api = WAF("Regional WAF\n(optional)")
            api = APIGateway("REST API (prod)\nCognito authorizer\nthrottling, validation\nresponse streaming")
            agent = Lambda("Agent\nPython 3.12 + LWA\nBedrock ConverseStream")
            conv_api = Lambda("Conversations API")
            kb_admin = Lambda("KB admin API\n(admin group only)")
            guardrail = Bedrock("Bedrock Guardrail")

        with Cluster("Amazon Bedrock (on-demand)", graph_attr=CLUSTER):
            models = Bedrock("Nova Micro / Nova Lite /\nClaude Haiku 4.5\n(cross-region profiles)")

        with Cluster("AgenticAi-Tools (MCP)", graph_attr=CLUSTER):
            mcp = Lambda("MCP server\nFunction URL (AWS_IAM)\nstreamable HTTP")
            mockdb = DynamodbTable("Mock commerce DB\n(in-memory, fake SQL)")

        with Cluster("AgenticAi-Data", graph_attr=CLUSTER):
            table = DynamodbTable("Conversations\n(on-demand, KMS, PITR, TTL)")

        with Cluster("AgenticAi-Rag", graph_attr=CLUSTER):
            docs = S3("Docs bucket\n(KMS, versioned)")
            bus = Eventbridge("S3 events")
            queue = SQS("Ingestion queue\n+ DLQ")
            ingest = Lambda("Ingestion trigger\n(batched, concurrency 2)")
            kb = Bedrock("Knowledge Base\nTitan Embed v2")
            vectors = S3("S3 Vectors index\n1024-d cosine")

        with Cluster("AgenticAi-Observability", graph_attr=CLUSTER):
            dash = Cloudwatch("Dashboard")
            alarms = CloudwatchAlarm("Alarms")
            topic = SNS("Alarm topic")
            budget = Budgets("Budget (optional)")
            traces = XRay("X-Ray traces")

        users >> Edge(label="HTTPS", **SYNC) >> waf_edge >> cdn
        users >> Edge(label="sign-in (PKCE)", **SECURITY) >> cognito
        cdn >> Edge(label="OAC", **SYNC) >> site
        cdn >> Edge(label="/api/*", **SYNC) >> waf_api >> api
        api >> Edge(label="JWT check", **SECURITY) >> cognito
        api >> Edge(label="POST /chat (stream)", **SYNC) >> agent
        api >> Edge(**SYNC) >> conv_api
        api >> Edge(**SYNC) >> kb_admin
        agent >> Edge(label="ConverseStream", **SYNC) >> models
        agent >> Edge(**SYNC) >> guardrail
        agent >> Edge(label="MCP over SigV4", **SYNC) >> mcp
        agent >> Edge(**SYNC) >> table
        conv_api >> Edge(**SYNC) >> table
        mcp >> Edge(**SYNC) >> mockdb
        mcp >> Edge(label="Retrieve", **SYNC) >> kb
        kb_admin >> Edge(label="presigned POST", **SYNC) >> docs
        users >> Edge(label="upload (presigned)", **ASYNC) >> docs
        docs >> Edge(**ASYNC) >> bus >> Edge(**ASYNC) >> queue >> Edge(**ASYNC) >> ingest
        ingest >> Edge(label="StartIngestionJob", **ASYNC) >> kb
        kb >> Edge(**SYNC) >> vectors
        kb >> Edge(label="read", **ASYNC) >> docs
        [agent, mcp, api] >> Edge(**TELEMETRY) >> traces
        [api, agent, queue] >> Edge(**TELEMETRY) >> alarms >> Edge(**TELEMETRY) >> topic
        alarms - Edge(**TELEMETRY) - dash
        topic - Edge(**TELEMETRY) - budget


def chat_request_path() -> None:
    with Diagram(
        "Chat request path (one user turn)",
        filename=str(OUT / "chat-request-path"),
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
    ):
        user = Users("Browser\n(SSE reader)")
        cdn = CloudFront("CloudFront\n/api/* no cache")
        api = APIGateway("API Gateway\nauthorizer -> validator\n-> throttle 5 rps chat")
        with Cluster("Agent Lambda (response streaming)", graph_attr=CLUSTER):
            lwa = Lambda("Lambda Web Adapter\n/opt/bootstrap")
            app = Lambda("Starlette app\nagent loop <= 5 steps\n90 s budget")
        table = DynamodbTable("Conversations\nload / save")
        guard = Bedrock("Guardrail\n(input + output)")
        model = Bedrock("Selected model\n(allowlisted)")
        mcp = Lambda("MCP server\ntools/list (cached 5 min)\ntools/call (15 s timeout)")
        kb = Bedrock("Knowledge Base\nRetrieve")

        user >> Edge(label="1 POST /api/chat\nBearer ID token", **SYNC) >> cdn >> Edge(**SYNC) >> api
        api >> Edge(label="2 STREAM invoke", **SYNC) >> lwa >> Edge(**SYNC) >> app
        app >> Edge(label="3 history", **SYNC) >> table
        app >> Edge(label="4 ConverseStream", **SYNC) >> model
        model - Edge(**SECURITY) - guard
        app >> Edge(label="5 tool calls (parallel)", **SYNC) >> mcp >> Edge(**SYNC) >> kb
        app >> Edge(label="6 SSE: text / tool_use /\ntool_result / done", **ASYNC) >> user


def rag_ingestion() -> None:
    with Diagram(
        "RAG knowledge-base maintenance",
        filename=str(OUT / "rag-ingestion"),
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
    ):
        admin = Users("Admin\n(Cognito 'admin' group)")
        api = APIGateway("/api/kb/*")
        kb_admin = Lambda("KB admin\nlist / upload URL /\ndelete / sync / jobs")
        docs = S3("Docs bucket\ndocs/ prefix")
        bus = Eventbridge("Object Created /\nObject Deleted")
        queue = SQS("Queue\n60 s batching window")
        dlq = SQS("DLQ")
        trigger = Lambda("Ingestion trigger\nmax concurrency 2\nidempotent client token")
        with Cluster("Bedrock Knowledge Base", graph_attr=CLUSTER):
            ds = Bedrock("Data source\nfixed 300-token chunks")
            embed = Bedrock("Titan Text\nEmbeddings v2")
            vectors = S3("S3 Vectors\nindex")

        admin >> Edge(**SYNC) >> api >> Edge(**SYNC) >> kb_admin
        kb_admin >> Edge(label="presigned POST\n(type + size bound)", **SYNC) >> admin
        admin >> Edge(label="upload", **SYNC) >> docs
        docs >> Edge(**ASYNC) >> bus >> Edge(**ASYNC) >> queue >> Edge(**ASYNC) >> trigger
        queue >> Edge(label="after 10 receives", **ASYNC) >> dlq
        trigger >> Edge(label="StartIngestionJob\n(409 -> retry later)", **ASYNC) >> ds
        kb_admin >> Edge(label="manual sync", **ASYNC) >> ds
        ds >> Edge(label="read + chunk", **ASYNC) >> docs
        ds >> Edge(**ASYNC) >> embed >> Edge(**ASYNC) >> vectors


def security_boundaries() -> None:
    with Diagram(
        "Security boundaries and identities",
        filename=str(OUT / "security-boundaries"),
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
    ):
        user = Users("Untrusted internet")
        with Cluster("Trust boundary 1: edge", graph_attr=CLUSTER):
            waf = WAF("WAF (optional)")
            cdn = CloudFront("CloudFront\nCSP, HSTS, TLS")
        with Cluster("Trust boundary 2: authenticated API", graph_attr=CLUSTER):
            cognito = Cognito("Cognito\nno self sign-up")
            api = APIGateway("API Gateway\nID-token authorizer")
        with Cluster("Trust boundary 3: workload identities (one role per function)", graph_attr=CLUSTER):
            agent_role = IAMRole("agent role\nInvoke allowlisted models,\nApplyGuardrail, table RW,\nInvokeFunctionUrl (MCP)")
            mcp_role = IAMRole("mcp role\nbedrock:Retrieve on one KB")
            admin_role = IAMRole("kb-admin role\ndocs/ prefix only,\nStart/Get ingestion")
            kb_role = IAMRole("KB service role\nread docs, s3vectors RW,\ninvoke embed model")
        with Cluster("Trust boundary 4: data at rest", graph_attr=CLUSTER):
            kms = KMS("Customer-managed keys\n(rotation on)")
            table = DynamodbTable("Conversations")
            docs = S3("Docs bucket")
            logs = CloudwatchLogs("Logs (1 month)")

        user >> Edge(**SYNC) >> waf >> cdn >> Edge(**SYNC) >> api
        user >> Edge(**SECURITY) >> cognito
        api >> Edge(**SECURITY) >> cognito
        api >> Edge(**SYNC) >> agent_role
        api >> Edge(label="admin group check\nin code", **SYNC) >> admin_role
        agent_role >> Edge(label="SigV4", **SECURITY) >> mcp_role
        mcp_role >> Edge(**SYNC) >> kb_role
        kms >> Edge(**SECURITY) >> [table, docs]
        [agent_role, mcp_role, admin_role] >> Edge(**TELEMETRY) >> logs


if __name__ == "__main__":
    production_architecture()
    chat_request_path()
    rag_ingestion()
    security_boundaries()
    print(f"wrote PNGs to {OUT}")
