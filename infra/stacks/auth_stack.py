"""Identity: Cognito user pool and the ``admin`` group.

The app client and hosted-UI domain live in the Frontend stack because their
callback URLs need the CloudFront domain, which only exists there.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from infra.config import AppConfig

ADMIN_GROUP = "admin"


class AuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: AppConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{config.project_name}-users",
            self_sign_up_enabled=False,  # admins invite users; no public sign-up
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            deletion_protection=config.retain_data,
            removal_policy=RemovalPolicy.RETAIN if config.retain_data else RemovalPolicy.DESTROY,
        )

        cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=ADMIN_GROUP,
            description="Can manage the knowledge base (upload, delete, sync).",
            precedence=1,
        )

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
