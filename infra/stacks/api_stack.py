"""Agent + API: API Gateway REST API, Cognito authorizer, three Lambdas, guardrail.

Routes (all behind the Cognito authorizer, all under /api so CloudFront can
route them same-origin):

    POST   /api/chat                         agent (streaming, Lambda Web Adapter)
    GET    /api/models                       api
    GET    /api/conversations                api
    GET    /api/conversations/{id}           api
    DELETE /api/conversations/{id}           api
    GET    /api/kb/documents                 kb-admin  (admin group)
    POST   /api/kb/documents                 kb-admin  (admin group)
    DELETE /api/kb/documents/{name}          kb-admin  (admin group)
    GET    /api/kb/sync | POST /api/kb/sync  kb-admin  (admin group)
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from infra.components.service_function import ServiceFunction
from infra.config import AppConfig
from infra.stacks.waf import managed_web_acl


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        user_pool: cognito.IUserPool,
        conversations: dynamodb.ITableV2,
        docs_bucket: s3.IBucket,
        rag_key: kms.IKey,
        knowledge_base_id: str,
        knowledge_base_arn: str,
        data_source_id: str,
        mcp_function: _lambda.IFunction,
        mcp_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        common_env = {
            "MODELS_JSON": config.models_json(),
            "DEFAULT_MODEL_ID": config.default_model_id,
            "CONVERSATIONS_TABLE": conversations.table_name,
            "CONVERSATION_TTL_DAYS": str(config.conversation_ttl_days),
        }

        # ------------------------------------------------------------ guardrail
        guardrail_env: dict[str, str] = {}
        self.guardrail = None
        if config.enable_guardrail:
            self.guardrail = bedrock.CfnGuardrail(
                self,
                "Guardrail",
                name=f"{config.project_name}-guardrail",
                description="Content and prompt-attack filters for the chat agent",
                blocked_input_messaging="Sorry, I can't help with that request.",
                blocked_outputs_messaging="Sorry, I can't provide that response.",
                content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                    filters_config=[
                        bedrock.CfnGuardrail.ContentFilterConfigProperty(type=t, input_strength="MEDIUM", output_strength="MEDIUM")
                        for t in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")
                    ]
                    + [
                        bedrock.CfnGuardrail.ContentFilterConfigProperty(
                            type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                        )
                    ]
                ),
            )
            guardrail_version = bedrock.CfnGuardrailVersion(
                self, "GuardrailVersion", guardrail_identifier=self.guardrail.attr_guardrail_id, description="v1"
            )
            guardrail_env = {"GUARDRAIL_ID": self.guardrail.attr_guardrail_id, "GUARDRAIL_VERSION": guardrail_version.attr_version}

        # ---------------------------------------------------------------- agent
        lwa_layer = _lambda.LayerVersion.from_layer_version_arn(
            self,
            "LambdaWebAdapter",
            f"arn:aws:lambda:{self.region}:{config.lambda_web_adapter_account}:layer:"
            f"LambdaAdapterLayerArm64:{config.lambda_web_adapter_layer_version}",
        )
        self.agent = ServiceFunction(
            self,
            "Agent",
            service="agent",
            handler="run.sh",
            description="Chat agent: Bedrock ConverseStream + MCP tools, streamed via Lambda Web Adapter",
            environment={
                **common_env,
                **guardrail_env,
                "MCP_SERVER_URL": f"{mcp_url}mcp",
                "AGENT_MAX_ITERATIONS": "5",
                "AGENT_DEADLINE_SECONDS": "90",
                "TOOL_TIMEOUT_SECONDS": "15",
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_INVOKE_MODE": "response_stream",
                "AWS_LWA_PORT": "8080",
                "AWS_LWA_READINESS_CHECK_PATH": "/healthz",
            },
            memory_size=1024,
            timeout=Duration.seconds(120),
            reserved_concurrency=config.agent_reserved_concurrency or None,
            layers=[lwa_layer],
        )
        self._grant_models(self.agent.role, config)
        if self.guardrail is not None:
            self.agent.role.add_to_policy(
                iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[self.guardrail.attr_guardrail_arn])
            )
        conversations.grant_read_write_data(self.agent.role)
        mcp_function.grant_invoke_url(self.agent.role)

        # ------------------------------------------------------------------ api
        self.api_fn = ServiceFunction(
            self,
            "ConversationsApi",
            service="api",
            description="Conversation history and model catalogue",
            environment=common_env,
            memory_size=256,
            timeout=Duration.seconds(10),
        )
        conversations.grant_read_write_data(self.api_fn.role)

        # ------------------------------------------------------------- kb admin
        self.kb_admin = ServiceFunction(
            self,
            "KnowledgeBaseAdmin",
            service="kb_admin",
            description="Admin API for knowledge-base documents and sync",
            environment={
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "DATA_SOURCE_ID": data_source_id,
            },
            memory_size=256,
            timeout=Duration.seconds(15),
        )
        self.kb_admin.role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"], resources=[docs_bucket.bucket_arn], conditions={"StringLike": {"s3:prefix": ["docs/*", "docs/"]}}
            )
        )
        self.kb_admin.role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:DeleteObject", "s3:GetObject"], resources=[docs_bucket.arn_for_objects("docs/*")]
            )
        )
        self.kb_admin.role.add_to_policy(iam.PolicyStatement(actions=["kms:GenerateDataKey", "kms:Decrypt"], resources=[rag_key.key_arn]))
        self.kb_admin.role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs", "bedrock:GetIngestionJob"],
                resources=[knowledge_base_arn],
            )
        )

        # ------------------------------------------------------------- REST API
        access_logs = logs.LogGroup(self, "ApiAccessLogs", retention=logs.RetentionDays.ONE_MONTH)
        chat_path = "/api/chat/POST"
        self.api = apigw.RestApi(
            self,
            "RestApi",
            rest_api_name=f"{config.project_name}-api",
            description="Chat, conversations and knowledge-base admin API",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            cloud_watch_role=True,
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                tracing_enabled=True,
                metrics_enabled=True,
                logging_level=apigw.MethodLoggingLevel.ERROR,
                data_trace_enabled=False,  # never log request/response bodies (prompts, tokens)
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                throttling_rate_limit=config.api_rate_limit,
                throttling_burst_limit=config.api_burst_limit,
                method_options={
                    chat_path: apigw.MethodDeploymentOptions(
                        throttling_rate_limit=config.chat_rate_limit,
                        throttling_burst_limit=config.chat_burst_limit,
                    )
                },
            ),
        )
        authorizer = apigw.CognitoUserPoolsAuthorizer(self, "Authorizer", cognito_user_pools=[user_pool])
        auth = {"authorizer": authorizer, "authorization_type": apigw.AuthorizationType.COGNITO}

        body_validator = apigw.RequestValidator(
            self, "BodyValidator", rest_api=self.api, validate_request_body=True, validate_request_parameters=True
        )
        params_validator = apigw.RequestValidator(
            self, "ParamsValidator", rest_api=self.api, validate_request_body=False, validate_request_parameters=True
        )
        chat_model = self.api.add_model(
            "ChatRequest",
            content_type="application/json",
            schema=apigw.JsonSchema(
                schema=apigw.JsonSchemaVersion.DRAFT4,
                type=apigw.JsonSchemaType.OBJECT,
                required=["message"],
                additional_properties=False,
                properties={
                    "message": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, min_length=1, max_length=4000),
                    "modelId": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, max_length=200),
                    "conversationId": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, max_length=36),
                },
            ),
        )

        api_root = self.api.root.add_resource("api")

        chat = api_root.add_resource("chat")
        chat.add_method(
            "POST",
            apigw.LambdaIntegration(
                self.agent.function,
                response_transfer_mode=apigw.ResponseTransferMode.STREAM,
                timeout=Duration.seconds(120),
            ),
            request_validator=body_validator,
            request_models={"application/json": chat_model},
            **auth,
        )

        api_integration = apigw.LambdaIntegration(self.api_fn.function)
        api_root.add_resource("models").add_method("GET", api_integration, request_validator=params_validator, **auth)
        convs = api_root.add_resource("conversations")
        convs.add_method("GET", api_integration, request_validator=params_validator, **auth)
        conv = convs.add_resource("{conversationId}")
        for verb in ("GET", "DELETE"):
            conv.add_method(
                verb,
                api_integration,
                request_validator=params_validator,
                request_parameters={"method.request.path.conversationId": True},
                **auth,
            )

        kb_integration = apigw.LambdaIntegration(self.kb_admin.function)
        kb = api_root.add_resource("kb")
        docs = kb.add_resource("documents")
        docs.add_method("GET", kb_integration, request_validator=params_validator, **auth)
        docs.add_method("POST", kb_integration, request_validator=params_validator, **auth)
        docs.add_resource("{name}").add_method(
            "DELETE",
            kb_integration,
            request_validator=params_validator,
            request_parameters={"method.request.path.name": True},
            **auth,
        )
        sync = kb.add_resource("sync")
        for verb in ("GET", "POST"):
            sync.add_method(verb, kb_integration, request_validator=params_validator, **auth)

        if config.enable_waf:
            acl = managed_web_acl(self, "ApiWebAcl", scope="REGIONAL", name=f"{config.project_name}-api")
            wafv2.CfnWebACLAssociation(
                self, "ApiWebAclAssociation", resource_arn=self.api.deployment_stage.stage_arn, web_acl_arn=acl.attr_arn
            )

        CfnOutput(self, "ApiUrl", value=self.api.url)

    def _grant_models(self, role: iam.IRole, config: AppConfig) -> None:
        """Allow Converse/ConverseStream on exactly the allowlisted models.

        Cross-region inference profiles (``us.``/``eu.``/``apac.``/``global.``
        prefixes) need permission on the profile *and* on the foundation model in
        every destination region the profile may route to.
        """
        resources: list[str] = []
        for m in config.models:
            if m.is_inference_profile:
                resources.append(f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{m.id}")
                resources.append(f"arn:aws:bedrock:*::foundation-model/{m.foundation_model_id}")
                if m.id.startswith("global."):
                    resources.append(f"arn:aws:bedrock:::foundation-model/{m.foundation_model_id}")
            else:
                resources.append(f"arn:aws:bedrock:{self.region}::foundation-model/{m.id}")
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeAllowlistedModels",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=resources,
            )
        )
