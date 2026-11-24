"""Stateful data shared by the services: the KMS key and conversation table."""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from constructs import Construct

from infra.config import AppConfig


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: AppConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if config.retain_data else RemovalPolicy.DESTROY

        self.key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/{config.project_name}/data",
            description="Encrypts conversations, documents and queues",
            enable_key_rotation=True,
            removal_policy=removal,
        )

        self.conversations = dynamodb.TableV2(
            self,
            "Conversations",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(self.key),
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            deletion_protection=config.retain_data,
            removal_policy=removal,
        )

        CfnOutput(self, "ConversationsTable", value=self.conversations.table_name)
