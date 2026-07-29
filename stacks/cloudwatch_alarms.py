from aws_cdk import (
    Duration,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_lambda as _lambda,
)
from constructs import Construct


class CertTrackerAlarmsConstruct(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        notification_topic: sns.Topic,
        pagerduty_endpoint: str = "",
        badge_sync_fn: _lambda.Function,
        expiration_checker_fn: _lambda.Function,
        compliance_reporter_fn: _lambda.Function,
        notification_handler_fn: _lambda.Function,
    ):
        super().__init__(scope, id)

        namespace = "CredlyCertTracker"
        action = cw_actions.SnsAction(notification_topic)

        # ─── Tier Compliance Alarms ───
        tiers = {
            "Foundational": 10,
            "Technical": 25,
            "Professional/Specialty": 10,
        }

        for tier, required in tiers.items():
            tier_safe = tier.replace("/", "")

            # RED: below 80%
            red = cloudwatch.Alarm(
                self, f"{tier_safe}Red",
                alarm_name=f"CertTracker-{tier_safe}-RED",
                metric=cloudwatch.Metric(
                    namespace=namespace,
                    metric_name="CompliancePercentage",
                    dimensions_map={"Tier": tier},
                    period=Duration.hours(1),
                    statistic="Average",
                ),
                threshold=80,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description=f"{tier} below 80% - NON-COMPLIANT",
            )
            red.add_alarm_action(action)

            # YELLOW: below 100%
            yellow = cloudwatch.Alarm(
                self, f"{tier_safe}Yellow",
                alarm_name=f"CertTracker-{tier_safe}-YELLOW",
                metric=cloudwatch.Metric(
                    namespace=namespace,
                    metric_name="CompliancePercentage",
                    dimensions_map={"Tier": tier},
                    period=Duration.hours(1),
                    statistic="Average",
                ),
                threshold=100,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description=f"{tier} below 100% - AT RISK",
            )
            yellow.add_alarm_action(action)

        # ─── Lambda Error Alarms ───
        for fn_name, fn in [
            ("BadgeSync", badge_sync_fn),
            ("ExpirationChecker", expiration_checker_fn),
            ("ComplianceReporter", compliance_reporter_fn),
            ("NotificationHandler", notification_handler_fn),
        ]:
            cloudwatch.Alarm(
                self, f"{fn_name}Errors",
                alarm_name=f"CertTracker-{fn_name}-Errors",
                metric=fn.metric_errors(period=Duration.minutes(5)),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
                alarm_description=f"{fn_name} Lambda errors detected",
            ).add_alarm_action(action)
