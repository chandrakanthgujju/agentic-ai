"""Opinionated Lambda function used by every service in this app.

* arm64 (Graviton): ~20% cheaper per GB-second than x86 for the same code.
* Its own IAM role with only log-write permissions to start with; every other
  permission is granted explicitly by the stack that wires the function.
* Its own log group with finite retention (no orphaned "never expire" logs).
* X-Ray active tracing and Powertools environment defaults.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.components.python_function import ARCHITECTURE, RUNTIME, service_code


class ServiceFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service: str,
        handler: str = "handler.handler",
        description: str,
        environment: dict[str, str] | None = None,
        memory_size: int = 512,
        timeout: Duration = Duration.seconds(30),
        reserved_concurrency: int | None = None,
        layers: list[_lambda.ILayerVersion] | None = None,
        metrics_namespace: str = "AgenticAI",
    ) -> None:
        super().__init__(scope, construct_id)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Execution role for {service}",
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[self.log_group.log_group_arn, f"{self.log_group.log_group_arn}:*"],
            )
        )

        env = {
            "POWERTOOLS_SERVICE_NAME": service,
            "POWERTOOLS_METRICS_NAMESPACE": metrics_namespace,
            "POWERTOOLS_LOG_LEVEL": "INFO",
            "POWERTOOLS_LOGGER_LOG_EVENT": "false",
            "POWERTOOLS_TRACER_CAPTURE_RESPONSE": "false",
        }
        env.update(environment or {})

        self.function = _lambda.Function(
            self,
            "Function",
            description=description,
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler=handler,
            code=service_code(service),
            memory_size=memory_size,
            timeout=timeout,
            role=self.role,
            log_group=self.log_group,
            tracing=_lambda.Tracing.ACTIVE,
            environment=env,
            reserved_concurrent_executions=reserved_concurrency,
            layers=layers,
        )
