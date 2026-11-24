"""Dashboard, alarms and (optional) budget for the whole app."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.config import AppConfig

NAMESPACE = "AgenticAI"


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        api: apigw.RestApi,
        functions: dict[str, _lambda.IFunction],
        dlq: sqs.IQueue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        key = kms.Key(self, "AlarmTopicKey", enable_key_rotation=True, description="Encrypts the alarm SNS topic")
        key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
            )
        )
        self.topic = sns.Topic(self, "Alarms", display_name=f"{config.project_name} alarms", master_key=key, enforce_ssl=True)
        if config.alarm_email:
            self.topic.add_subscription(subs.EmailSubscription(config.alarm_email))
        action = cw_actions.SnsAction(self.topic)

        def alarm(id_: str, metric: cw.IMetric, threshold: float, desc: str, periods: int = 1) -> cw.Alarm:
            a = cw.Alarm(
                self,
                id_,
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=desc,
            )
            a.add_alarm_action(action)
            return a

        five_min = Duration.minutes(5)
        alarm("Api5xx", api.metric_server_error(period=five_min, statistic="Sum"), 5, "API Gateway 5xx responses")
        alarm("ApiLatencyP90", api.metric_latency(period=five_min, statistic="p90"), 30000, "API p90 latency >= 30 s", periods=3)
        alarm("ApiThrottled", api.metric_client_error(period=five_min, statistic="Sum"), 100, "Many 4xx (incl. 429 throttles)")
        for name, fn in functions.items():
            alarm(f"{name}Errors", fn.metric_errors(period=five_min, statistic="Sum"), 3, f"{name} Lambda errors")
            alarm(f"{name}Throttles", fn.metric_throttles(period=five_min, statistic="Sum"), 1, f"{name} Lambda throttles")
        alarm(
            "IngestionDLQ",
            dlq.metric_approximate_number_of_messages_visible(period=five_min, statistic="Maximum"),
            1,
            "Document changes that could not be ingested (see DLQ)",
        )
        agent_errors = cw.Metric(
            namespace=NAMESPACE, metric_name="AgentErrors", dimensions_map={"service": "agent"}, statistic="Sum", period=five_min
        )
        alarm("AgentModelErrors", agent_errors, 3, "Bedrock/agent failures surfaced to users")

        def m(name: str, service: str, stat: str = "Sum") -> cw.Metric:
            return cw.Metric(namespace=NAMESPACE, metric_name=name, dimensions_map={"service": service}, statistic=stat, period=five_min)

        dash = cw.Dashboard(self, "Dashboard", dashboard_name=f"{config.project_name}-ops", default_interval=Duration.hours(6))
        dash.add_widgets(
            cw.TextWidget(
                markdown=f"# {config.project_name}\nTraffic, latency, errors, tokens, tools and RAG ingestion.", width=24, height=2
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="API requests / 4xx / 5xx",
                left=[
                    api.metric_count(period=five_min),
                    api.metric_client_error(period=five_min),
                    api.metric_server_error(period=five_min),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="API latency p50 / p90 (ms)",
                left=[api.metric_latency(statistic="p50", period=five_min), api.metric_latency(statistic="p90", period=five_min)],
                width=8,
            ),
            cw.GraphWidget(
                title="Chat latency (ms, end of stream)",
                left=[m("ChatLatencyMs", "agent", "p50"), m("ChatLatencyMs", "agent", "p90")],
                width=8,
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(title="Tokens", left=[m("InputTokens", "agent"), m("OutputTokens", "agent")], width=8),
            cw.GraphWidget(
                title="Tool latency p90 (ms) / tool errors",
                left=[m("ToolLatencyMs", "agent", "p90")],
                right=[m("ToolErrors", "agent")],
                width=8,
            ),
            cw.GraphWidget(title="Bedrock retries / agent errors", left=[m("BedrockRetries", "agent"), m("AgentErrors", "agent")], width=8),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Lambda concurrency",
                left=[fn.metric("ConcurrentExecutions", statistic="Maximum", period=five_min) for fn in functions.values()],
                width=8,
            ),
            cw.GraphWidget(title="Lambda errors", left=[fn.metric_errors(period=five_min) for fn in functions.values()], width=8),
            cw.GraphWidget(
                title="RAG ingestion",
                left=[m("IngestionJobsStarted", "ingestion"), m("IngestionDeferred", "ingestion")],
                right=[dlq.metric_approximate_number_of_messages_visible(period=five_min)],
                width=8,
            ),
        )

        if config.monthly_budget_usd > 0 and config.alarm_email:
            budgets.CfnBudget(
                self,
                "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=config.monthly_budget_usd, unit="USD"),
                    budget_name=f"{config.project_name}-monthly",
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN", notification_type=nt, threshold=th, threshold_type="PERCENTAGE"
                        ),
                        subscribers=[budgets.CfnBudget.SubscriberProperty(address=config.alarm_email, subscription_type="EMAIL")],
                    )
                    for nt, th in (("ACTUAL", 80), ("FORECASTED", 100))
                ],
            )

        CfnOutput(self, "DashboardName", value=dash.dashboard_name)
        CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)
