"""Synthesis tests: the whole app synthesizes with cdk-nag (AwsSolutions) enabled
and the security/scaling properties we rely on are present in the templates.

Bundling is skipped here (context ``aws:cdk:bundling-stacks=[]``) so tests run in
seconds; `make synth` exercises the real bundling.
"""

import json
import os
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.app import build_app

ROOT = Path(__file__).resolve().parents[2]


def _synth(overrides=None, region="us-east-1"):
    os.environ["CDK_DEFAULT_ACCOUNT"] = "123456789012"
    os.environ["CDK_DEFAULT_REGION"] = region
    ctx = json.loads((ROOT / "cdk.json").read_text())["context"]
    ctx["aws:cdk:bundling-stacks"] = []
    ctx.update(overrides or {})
    app = build_app(cdk.App(context=ctx))
    assembly = app.synth()  # raises if any un-acknowledged cdk-nag error exists
    return app, assembly


@pytest.fixture(scope="module")
def app_and_assembly():
    return _synth()


def template(app_and_assembly, name):
    app, _ = app_and_assembly
    return Template.from_stack(app.node.find_child(f"AgenticAi-{name}"))


def test_all_stacks_synthesize(app_and_assembly):
    _, asm = app_and_assembly
    assert sorted(s.stack_name for s in asm.stacks) == sorted(
        f"AgenticAi-{n}" for n in ("Auth", "Data", "Rag", "Tools", "Api", "Frontend", "Observability")
    )


def test_knowledge_base_uses_s3_vectors(app_and_assembly):
    t = template(app_and_assembly, "Rag")
    t.has_resource_properties(
        "AWS::S3Vectors::Index",
        {
            "Dimension": 1024,
            "DistanceMetric": "cosine",
            "DataType": "float32",
            "MetadataConfiguration": {"NonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]},
        },
    )
    t.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase", {"StorageConfiguration": {"Type": "S3_VECTORS", "S3VectorsConfiguration": Match.any_value()}}
    )
    t.has_resource_properties("AWS::Bedrock::DataSource", {"DataDeletionPolicy": "DELETE"})
    t.has_resource_properties("AWS::SQS::Queue", {"RedrivePolicy": {"maxReceiveCount": 10, "deadLetterTargetArn": Match.any_value()}})
    t.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {"BatchSize": 100, "MaximumBatchingWindowInSeconds": 60, "FunctionResponseTypes": ["ReportBatchItemFailures"]},
    )


def test_buckets_block_public_access_and_enforce_tls(app_and_assembly):
    for name in ("Rag", "Frontend"):
        t = template(app_and_assembly, name)
        for bucket in t.find_resources("AWS::S3::Bucket").values():
            assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        policies = json.dumps(t.find_resources("AWS::S3::BucketPolicy"))
        assert '"aws:SecureTransport": "false"' in policies


def test_mcp_server_requires_iam_auth(app_and_assembly):
    t = template(app_and_assembly, "Tools")
    t.has_resource_properties("AWS::Lambda::Url", {"AuthType": "AWS_IAM", "InvokeMode": "BUFFERED"})


def test_chat_route_streams_and_everything_is_authorized(app_and_assembly):
    t = template(app_and_assembly, "Api")
    methods = t.find_resources("AWS::ApiGateway::Method")
    assert methods, "no API methods"
    for method in methods.values():
        assert method["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS"
    t.has_resource_properties(
        "AWS::ApiGateway::Method", {"HttpMethod": "POST", "Integration": {"ResponseTransferMode": "STREAM", "TimeoutInMillis": 120000}}
    )
    stage = next(iter(t.find_resources("AWS::ApiGateway::Stage").values()))["Properties"]
    settings = {s["HttpMethod"] + s["ResourcePath"]: s for s in stage["MethodSettings"]}
    assert settings["*/*"]["ThrottlingRateLimit"] == 20
    assert settings["POST/~1api~1chat"]["ThrottlingRateLimit"] == 5
    assert stage["TracingEnabled"] is True


def test_agent_can_only_invoke_allowlisted_models(app_and_assembly):
    t = template(app_and_assembly, "Api")
    docs = json.dumps(t.find_resources("AWS::IAM::Policy"))
    assert "inference-profile/us.amazon.nova-micro-v1:0" in docs
    assert "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0" in docs
    assert '"bedrock:*"' not in docs and '"Action": "*"' not in docs
    t.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "ContentPolicyConfig": {
                "FiltersConfig": Match.array_with([{"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}])
            }
        },
    )


def test_app_functions_are_arm64_python312_with_tracing(app_and_assembly):
    found = 0
    for name in ("Rag", "Tools", "Api"):
        t = template(app_and_assembly, name)
        for fn in t.find_resources("AWS::Lambda::Function", {"Properties": {"TracingConfig": {"Mode": "Active"}}}).values():
            props = fn["Properties"]
            assert props["Architectures"] == ["arm64"]
            assert props["Runtime"] == "python3.12"
            found += 1
    assert found == 5


def test_frontend_is_private_s3_behind_cloudfront_with_security_headers(app_and_assembly):
    t = template(app_and_assembly, "Frontend")
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    dist = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert dist["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert any(b["PathPattern"] == "/api/*" for b in dist["CacheBehaviors"])
    headers = next(iter(t.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()))
    sec = headers["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert sec["FrameOptions"]["FrameOption"] == "DENY"
    assert "StrictTransportSecurity" in sec and "ContentSecurityPolicy" in sec
    t.has_resource_properties("AWS::Cognito::UserPoolClient", {"GenerateSecret": False, "AllowedOAuthFlows": ["code"]})


def test_waf_flag_adds_web_acls():
    app, _ = _synth({"enableWaf": True})
    api = Template.from_stack(app.node.find_child("AgenticAi-Api"))
    api.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "REGIONAL"})
    api.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)
    fe = Template.from_stack(app.node.find_child("AgenticAi-Frontend"))
    fe.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT"})


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError):
        _synth({"defaultModelId": "not-in-list"})
