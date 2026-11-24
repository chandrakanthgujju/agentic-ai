"""Static web UI: private S3 bucket + CloudFront (OAC) + Cognito hosted login.

The browser only ever talks to one origin, the CloudFront domain:

    /            -> S3 (Origin Access Control; bucket is not public)
    /api/*       -> API Gateway REST API (no caching, all viewer headers but Host)

Same-origin API calls mean the API needs no CORS at all, which is the strictest
CORS posture there is. See docs/adr/0002-cloudfront-not-public-s3-website.md.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from infra.config import AppConfig
from infra.stacks.waf import managed_web_acl

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


class FrontendStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        user_pool: cognito.IUserPool,
        api: apigw.RestApi,
        docs_bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if config.retain_data else RemovalPolicy.DESTROY
        auto_delete = not config.retain_data

        logs_bucket = s3.Bucket(
            self,
            "EdgeLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # CloudFront standard logs are delivered with ACLs.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=removal,
            auto_delete_objects=auto_delete,
        )
        self.site_bucket = s3.Bucket(
            self,
            "Site",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="s3-site/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        domain_prefix = config.cognito_domain_prefix or f"{config.project_name}-{self.account}"
        cognito_domain = cognito.UserPoolDomain(
            self,
            "HostedUiDomain",
            user_pool=user_pool,
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )
        cognito_host = f"{domain_prefix}.auth.{self.region}.amazoncognito.com"
        upload_host = f"{docs_bucket_name}.s3.{self.region}.amazonaws.com"

        csp = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self'",
                "img-src 'self' data:",
                "font-src 'self'",
                f"connect-src 'self' https://{cognito_host} https://{upload_host}",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "object-src 'none'",
            ]
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            comment="Strict security headers for the chat UI",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(content_security_policy=csp, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, preload=True, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override=True
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(protection=True, mode_block=True, override=True),
            ),
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Permissions-Policy", value="camera=(), microphone=(), geolocation=()", override=True
                    ),
                ]
            ),
        )

        web_acl_arn = None
        if config.enable_waf:
            if self.region == "us-east-1":
                web_acl_arn = managed_web_acl(self, "EdgeWebAcl", scope="CLOUDFRONT", name=f"{config.project_name}-edge").attr_arn
            else:
                Annotations.of(self).add_warning_v2(
                    "agentic:waf-region",
                    "CloudFront WAF web ACLs must live in us-east-1; skipped. The regional API WAF is still applied.",
                )

        site_origin = origins.S3BucketOrigin.with_origin_access_control(self.site_bucket)
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{config.project_name} chat UI",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,  # includes India edge locations
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_logging=True,
            log_bucket=logs_bucket,
            log_file_prefix="cloudfront/",
            web_acl_id=web_acl_arn,
            default_behavior=cloudfront.BehaviorOptions(
                origin=site_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                compress=True,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.RestApiOrigin(api, read_timeout=Duration.seconds(60), keepalive_timeout=Duration.seconds(60)),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=headers,
                    compress=False,  # compression would buffer the SSE stream
                ),
                "/config.json": cloudfront.BehaviorOptions(
                    origin=site_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    response_headers_policy=headers,
                ),
            },
            error_responses=[],
        )
        app_url = f"https://{self.distribution.distribution_domain_name}/"

        self.client = cognito.UserPoolClient(
            self,
            "WebClient",
            user_pool=user_pool,
            user_pool_client_name=f"{config.project_name}-web",
            generate_secret=False,  # public SPA client; PKCE protects the code exchange
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[app_url],
                logout_urls=[app_url],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.hours(12),
            enable_token_revocation=True,
        )

        runtime_config = {
            "region": self.region,
            "userPoolId": user_pool.user_pool_id,
            "clientId": self.client.user_pool_client_id,
            "cognitoDomain": f"https://{cognito_host}",
            "apiBase": "/api",
            "redirectUri": app_url,
        }
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[
                s3deploy.Source.asset(str(FRONTEND_DIR), exclude=["*.md", "node_modules", "dev-config.json"]),
                s3deploy.Source.json_data("config.json", runtime_config),
            ],
            destination_bucket=self.site_bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
            memory_limit=256,
        )

        CfnOutput(self, "AppUrl", value=app_url, description="Open this URL in a browser")
        CfnOutput(self, "CognitoHostedUi", value=f"https://{cognito_host}")
        CfnOutput(self, "UserPoolClientId", value=self.client.user_pool_client_id)
        self.cognito_domain = cognito_domain
