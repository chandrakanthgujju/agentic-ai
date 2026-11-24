"""cdk-nag (AwsSolutions) wiring and *justified* suppressions.

Every suppression states why the finding does not apply or what compensating
control exists. cdk-nag v3 plugs into CDK's native policy-validation framework, so findings are
acknowledged with ``Validations.of(scope).acknowledge`` and reported in
``cdk.out/policy-validation-report.json``.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import Acknowledgment, Stack, Validations

CDK_HELPERS = (
    "CDK-managed helper Lambdas (BucketDeployment, S3 auto-delete, bucket notifications, "
    "log retention) use AWS-managed basic execution policies and runtimes chosen by CDK."
)


@dataclass(frozen=True)
class Ack:
    rule: str
    reason: str


def _s(rule: str, reason: str) -> Ack:
    return Ack(rule, reason)


def _acknowledge(stack: Stack, acks: list[Ack]) -> None:
    for ack in acks:
        # A bare rule id is recorded as acknowledged-rule metadata, which cdk-nag reads for
        # the rule *and* all of its per-finding variants (e.g. IAM5[Resource::*]).
        Validations.of(stack).acknowledge(Acknowledgment(id=ack.rule, reason=ack.reason))


def suppress_common(stack: Stack) -> None:
    _acknowledge(
        stack,
        [
            _s("AwsSolutions-IAM4", CDK_HELPERS + " Application functions use dedicated roles with inline least-privilege policies."),
            _s(
                "AwsSolutions-IAM5",
                "Wildcards are limited to: X-Ray PutTraceSegments (no resource-level support), log-stream ARNs under the "
                "function's own log group, object keys under a single bucket prefix, KMS data-key operations on one key, "
                "cross-region inference-profile destinations for allowlisted models, and CDK helper functions.",
            ),
            _s(
                "AwsSolutions-L1",
                "Runtime pinned to python3.12 for all app functions (tested dependency set); CDK helper runtimes are managed by CDK.",
            ),
        ],
    )


def suppress_auth(stack: Stack) -> None:
    _acknowledge(
        stack,
        [
            _s(
                "AwsSolutions-COG2",
                "MFA is OPTIONAL (TOTP) so the demo can be used without an authenticator; set Mfa.REQUIRED for production tenants.",
            ),
            _s(
                "AwsSolutions-COG3",
                "Threat protection (advanced security) requires the Cognito Plus tier and adds cost; enable it for production.",
            ),
            _s(
                "AwsSolutions-COG8",
                "Essentials tier is sufficient for a personal deployment; Plus tier (threat protection) is a per-MAU cost decision.",
            ),
        ],
    )


def suppress_rag(stack: Stack) -> None:
    _acknowledge(
        stack,
        [
            _s("AwsSolutions-S1", "The access-logs bucket is the log destination; logging it to itself would recurse."),
            _s("AwsSolutions-SQS3", "This queue is the dead-letter queue for the ingestion queue."),
        ],
    )


def suppress_api(stack: Stack, waf_enabled: bool) -> None:
    rules = [
        _s(
            "AwsSolutions-APIG4",
            "All methods use the Cognito authorizer; this finding is raised only for CDK-generated resources without methods.",
        ),
        _s("AwsSolutions-COG4", "All methods use the Cognito user pool authorizer."),
    ]
    if not waf_enabled:
        rules.append(
            _s(
                "AwsSolutions-APIG3",
                "WAF is behind the enableWaf flag (default off to avoid ~$10+/month fixed cost); stage throttling is always on.",
            )
        )
    _acknowledge(stack, rules)


def suppress_frontend(stack: Stack, waf_enabled: bool) -> None:
    rules = [
        _s("AwsSolutions-S1", "The edge-logs bucket is the log destination; logging it to itself would recurse."),
        _s("AwsSolutions-CFR1", "No geo restriction: the app is meant to be reachable from anywhere; auth is enforced by Cognito."),
        _s(
            "AwsSolutions-CFR4",
            "The default *.cloudfront.net certificate does not allow pinning a minimum TLS policy; use a custom domain + ACM "
            "certificate (TLSv1.2_2021) for production. Viewer traffic is still HTTPS-only.",
        ),
    ]
    if not waf_enabled or stack.region != "us-east-1":
        rules.append(_s("AwsSolutions-CFR2", "CloudFront WAF is behind the enableWaf flag (us-east-1 only)."))
    _acknowledge(stack, rules)
