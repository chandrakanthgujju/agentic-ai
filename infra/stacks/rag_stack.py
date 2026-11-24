"""Retrieval-augmented generation: documents, vectors and ingestion.

    Admin UI --presigned POST--> S3 docs bucket (KMS)  --EventBridge-->  SQS (+DLQ)
                                                                          |
                                        Ingestion Lambda <---batch 60s----+
                                              |
                                  StartIngestionJob (Bedrock Knowledge Base)
                                              |
                  chunk (fixed 300 tokens) -> Titan Text Embeddings v2 (1024-d)
                                              |
                                  Amazon S3 Vectors index (cosine)

Why S3 Vectors: it is pay-per-use storage + queries with no hourly floor, unlike
OpenSearch Serverless (minimum OCUs) or Aurora (instance/ACU hours). See
docs/adr/0003-vector-store-s3-vectors.md.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.components.service_function import ServiceFunction
from infra.config import AppConfig

DOCS_PREFIX = "docs/"


class RagStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: AppConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if config.retain_data else RemovalPolicy.DESTROY
        auto_delete = not config.retain_data

        self.key = kms.Key(
            self,
            "RagKey",
            alias=f"alias/{config.project_name}/rag",
            description="Encrypts knowledge-base source documents and the ingestion queue",
            enable_key_rotation=True,
            removal_policy=removal,
        )
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowEventBridgeToUseKeyForSqs",
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        self.access_logs = s3.Bucket(
            self,
            "AccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=removal,
            auto_delete_objects=auto_delete,
        )

        upload_origins = ["https://*.cloudfront.net", *config.extra_upload_origins]
        self.docs_bucket = s3.Bucket(
            self,
            "Documents",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            event_bridge_enabled=True,
            server_access_logs_bucket=self.access_logs,
            server_access_logs_prefix="documents/",
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST],
                    allowed_origins=upload_origins,
                    allowed_headers=["*"],
                    max_age=300,
                )
            ],
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(30))],
            removal_policy=removal,
            auto_delete_objects=auto_delete,
        )

        # ------------------------------------------------------------ vectors
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            encryption_configuration=s3vectors.CfnVectorBucket.EncryptionConfigurationProperty(sse_type="AES256"),
        )
        self.vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
            index_name=f"{config.project_name}-kb",
            data_type="float32",
            dimension=config.embedding_dimensions,
            distance_metric="cosine",
            # Bedrock stores chunk text and its own metadata on each vector. They exceed
            # the 2 KB filterable-metadata limit, so they must be non-filterable.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
            ),
        )
        if config.retain_data:
            self.vector_bucket.apply_removal_policy(RemovalPolicy.RETAIN)
            self.vector_index.apply_removal_policy(RemovalPolicy.RETAIN)

        # ---------------------------------------------------- knowledge base
        embedding_model_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{config.embedding_model_id}"
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"},
                },
            ),
            description="Bedrock Knowledge Base service role (read docs, embed, write vectors)",
        )
        kb_policy = iam.Policy(
            self,
            "KnowledgeBasePolicy",
            roles=[kb_role],
            statements=[
                iam.PolicyStatement(sid="Embed", actions=["bedrock:InvokeModel"], resources=[embedding_model_arn]),
                iam.PolicyStatement(
                    sid="ListDocs",
                    actions=["s3:ListBucket"],
                    resources=[self.docs_bucket.bucket_arn],
                    conditions={"StringEquals": {"aws:ResourceAccount": self.account}},
                ),
                iam.PolicyStatement(
                    sid="ReadDocs",
                    actions=["s3:GetObject"],
                    resources=[self.docs_bucket.arn_for_objects(f"{DOCS_PREFIX}*")],
                    conditions={"StringEquals": {"aws:ResourceAccount": self.account}},
                ),
                iam.PolicyStatement(sid="DecryptDocs", actions=["kms:Decrypt"], resources=[self.key.key_arn]),
                iam.PolicyStatement(
                    sid="Vectors",
                    actions=[
                        "s3vectors:PutVectors",
                        "s3vectors:GetVectors",
                        "s3vectors:DeleteVectors",
                        "s3vectors:QueryVectors",
                        "s3vectors:GetIndex",
                    ],
                    resources=[self.vector_index.attr_index_arn],
                ),
            ],
        )

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{config.project_name}-kb",
            description="Documents uploaded through the admin UI",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=config.embedding_dimensions,
                            embedding_data_type="FLOAT32",
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=self.vector_index.attr_index_arn,
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(kb_policy)

        self.data_source = bedrock.CfnDataSource(
            self,
            "DocumentsSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="documents",
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.docs_bucket.bucket_arn,
                    inclusion_prefixes=[DOCS_PREFIX],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=300, overlap_percentage=20
                    ),
                )
            ),
        )

        # --------------------------------------------------------- ingestion
        self.dlq = sqs.Queue(
            self,
            "IngestionDLQ",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.queue = sqs.Queue(
            self,
            "IngestionQueue",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            # A redelivery is how we wait out a running ingestion job; 5 min x 10 tries.
            visibility_timeout=Duration.minutes(5),
            dead_letter_queue=sqs.DeadLetterQueue(queue=self.dlq, max_receive_count=10),
        )

        events.Rule(
            self,
            "DocumentChanged",
            description="Queue a knowledge-base sync when a document is added, replaced or deleted",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created", "Object Deleted"],
                detail={"bucket": {"name": [self.docs_bucket.bucket_name]}, "object": {"key": [{"prefix": DOCS_PREFIX}]}},
            ),
            targets=[targets.SqsQueue(self.queue)],
        )

        self.ingestion_fn = ServiceFunction(
            self,
            "Ingestion",
            service="ingestion",
            description="Starts Bedrock KB ingestion jobs for batched document changes",
            environment={
                "KNOWLEDGE_BASE_ID": self.knowledge_base.attr_knowledge_base_id,
                "DATA_SOURCE_ID": self.data_source.attr_data_source_id,
            },
            memory_size=256,
            timeout=Duration.seconds(30),
        )
        self.ingestion_fn.function.add_event_source(
            sources.SqsEventSource(
                self.queue,
                batch_size=100,
                max_batching_window=Duration.seconds(60),
                report_batch_item_failures=True,
                max_concurrency=2,
            )
        )
        self.ingestion_fn.role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[self.knowledge_base.attr_knowledge_base_arn],
            )
        )

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source.attr_data_source_id)
        CfnOutput(self, "DocumentsBucket", value=self.docs_bucket.bucket_name)
        CfnOutput(self, "VectorIndexArn", value=self.vector_index.attr_index_arn)
