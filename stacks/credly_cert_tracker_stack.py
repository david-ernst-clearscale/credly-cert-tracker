import os
"""Main CDK Stack — wires all constructs together."""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
)
from constructs import Construct
from stacks.auth import AuthConstruct
from stacks.cloudwatch_alarms import CertTrackerAlarmsConstruct
from stacks.websocket_dashboard import WebSocketDashboardConstruct
from stacks.static_hosting import StaticHostingConstruct
from stacks.rest_api import DashboardRestApiConstruct


class CredlyCertTrackerStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Authentication (Google OAuth via Cognito)
        auth = AuthConstruct(
            self, "Auth",
            cloudfront_domain="d3ekm65uptt6j8.cloudfront.net",
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        )




        # ─── DynamoDB Tables ───
        users_table = dynamodb.Table(
            self, "UsersTable",
            table_name="credly-users",
            partition_key=dynamodb.Attribute(
                name="employee_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        certs_table = dynamodb.Table(
            self, "CertsTable",
            table_name="credly-certifications",
            partition_key=dynamodb.Attribute(
                name="employee_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="certification_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
        )

        certs_table.add_global_secondary_index(
            index_name="by-expiration",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="expires_at", type=dynamodb.AttributeType.STRING
            ),
        )

        # ─── SNS Topic ───
        expiration_topic = sns.Topic(
            self, "ExpirationTopic",
            display_name="CertTracker-Expiration-Alerts",
        )

        # ─── Lambda: Badge Sync ───
        badge_sync_fn = _lambda.Function(
            self, "BadgeSyncFn",
            function_name="credly-badge-sync",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/badge_sync"),
            environment={
                "USERS_TABLE": users_table.table_name,
                "CERTS_TABLE": certs_table.table_name,
            },
            timeout=Duration.minutes(5),
            memory_size=512,
        )
        users_table.grant_read_data(badge_sync_fn)
        certs_table.grant_read_write_data(badge_sync_fn)

        # ─── Lambda: Expiration Checker ───
        expiration_checker_fn = _lambda.Function(
            self, "ExpirationCheckerFn",
            function_name="credly-expiration-checker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/expiration_checker"),
            environment={
                "CERTS_TABLE": certs_table.table_name,
                "TOPIC_ARN": expiration_topic.topic_arn,
            },
            timeout=Duration.minutes(2),
            memory_size=256,
        )
        certs_table.grant_read_data(expiration_checker_fn)
        expiration_topic.grant_publish(expiration_checker_fn)

        # ─── Lambda: Notification Handler ───
        notification_handler_fn = _lambda.Function(
            self, "NotificationHandlerFn",
            function_name="credly-notification-handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/notification_handler"),
            environment={
                "USERS_TABLE": users_table.table_name,
                "CERTS_TABLE": certs_table.table_name,
            },
            timeout=Duration.seconds(30),
            memory_size=256,
        )
        users_table.grant_read_data(notification_handler_fn)
        certs_table.grant_read_data(notification_handler_fn)
        expiration_topic.add_subscription(
            subs.LambdaSubscription(notification_handler_fn)
        )

        # ─── Lambda: Compliance Reporter ───
        compliance_reporter_fn = _lambda.Function(
            self, "ComplianceReporterFn",
            function_name="credly-compliance-reporter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/compliance_reporter"),
            environment={
                "CERTS_TABLE": certs_table.table_name,
                "USERS_TABLE": users_table.table_name,
            },
            timeout=Duration.minutes(2),
            memory_size=256,
        )
        certs_table.grant_read_data(compliance_reporter_fn)
        compliance_reporter_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )
        users_table.grant_read_data(compliance_reporter_fn)

        # ─── Add cross-references ───
        badge_sync_fn.add_environment("NOTIFICATION_LAMBDA_ARN", notification_handler_fn.function_arn)
        badge_sync_fn.add_environment("SCHEDULER_ROLE_ARN", "")

        # ─── EventBridge: Daily Sync Schedule ───
        events.Rule(
            self, "DailySyncRule",
            rule_name="credly-daily-badge-sync",
            schedule=events.Schedule.cron(hour="6", minute="0"),
            targets=[targets.LambdaFunction(badge_sync_fn)],
        )

        # ─── EventBridge: Hourly Expiration Check ───
        events.Rule(
            self, "HourlyExpirationCheck",
            rule_name="credly-hourly-expiration-check",
            schedule=events.Schedule.rate(Duration.hours(1)),
            targets=[targets.LambdaFunction(expiration_checker_fn)],
        )

        # ─── CloudWatch Alarms ───
        CertTrackerAlarmsConstruct(
            self, "Alarms",
            notification_topic=expiration_topic,
            pagerduty_endpoint="",  # Add your PagerDuty endpoint
            badge_sync_fn=badge_sync_fn,
            expiration_checker_fn=expiration_checker_fn,
            compliance_reporter_fn=compliance_reporter_fn,
            notification_handler_fn=notification_handler_fn,
        )

        # ─── WebSocket Dashboard ───
        WebSocketDashboardConstruct(
            self, "WebSocketDashboard",
            certs_table=certs_table,
            users_table=users_table,
        )

        # ─── REST API for Dashboard ───
        DashboardRestApiConstruct(
            self, "DashboardApi",
            certs_table=certs_table,
            users_table=users_table,
            user_pool=auth.user_pool,
        )

        # ─── Static Hosting ───
        StaticHostingConstruct(
            self, "Hosting",
            build_path="./frontend/dist",
            allowed_ips=["47.194.165.13"],
        )
