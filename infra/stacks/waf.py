"""Shared AWS WAF web ACL definition (AWS managed rules + per-IP rate limit)."""

from __future__ import annotations

from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct


def _managed(name: str, priority: int) -> wafv2.CfnWebACL.RuleProperty:
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(vendor_name="AWS", name=name)
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True, metric_name=name, sampled_requests_enabled=True
        ),
    )


def managed_web_acl(
    scope_construct: Construct, construct_id: str, *, scope: str, name: str, rate_limit_per_5min: int = 600
) -> wafv2.CfnWebACL:
    """``scope`` is ``REGIONAL`` (API Gateway) or ``CLOUDFRONT`` (us-east-1 only)."""
    return wafv2.CfnWebACL(
        scope_construct,
        construct_id,
        name=name,
        scope=scope,
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True, metric_name=name, sampled_requests_enabled=True
        ),
        rules=[
            wafv2.CfnWebACL.RuleProperty(
                name="RateLimitPerIp",
                priority=0,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(limit=rate_limit_per_5min, aggregate_key_type="IP")
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name=f"{name}-rate", sampled_requests_enabled=True
                ),
            ),
            _managed("AWSManagedRulesAmazonIpReputationList", 1),
            _managed("AWSManagedRulesCommonRuleSet", 2),
            _managed("AWSManagedRulesKnownBadInputsRuleSet", 3),
        ],
    )
