from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
)
from constructs import Construct


class StaticHostingConstruct(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        build_path: str = "./frontend/dist",
        alert_email: str = "",
    ):
        super().__init__(scope, id)

        self.bucket = s3.Bucket(
            self,
            "DashboardBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            # Recoverability: if objects are ever deleted (accidental empty,
            # a deploy pruning against an empty dist), versioning leaves
            # delete-markers we can roll back instead of losing them silently.
            versioned=True,
            lifecycle_rules=[
                # The keep-alive below self-copies every object weekly, which
                # creates a new version each time. Without this the bucket would
                # accumulate a version per object per week forever. 60 days is a
                # deliberate floor: it must comfortably outlive any age-based
                # sweep so a delete marker always still has a real version
                # underneath it to restore.
                s3.LifecycleRule(
                    id="ExpireOldFrontendVersions",
                    noncurrent_version_expiration=Duration.days(60),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

        self.distribution = cloudfront.Distribution(
            self,
            # Logical ID bumped to force replacement, which assigns a new
            # cloudfront.net domain. The previous one had been published.
            "DistributionV2",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                # With OAC, S3 returns 403 (not 404) for a missing object.
                # Map it to the SPA shell so a missing file degrades to the app
                # instead of leaking raw S3 AccessDenied XML to users.
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        s3_deploy.BucketDeployment(
            self,
            "Deploy",
            sources=[s3_deploy.Source.asset(build_path)],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
        )

        # ─── Keep-alive: keep the deployed frontend from ageing out ───
        # Some accounts run scheduled housekeeping that deletes S3 objects past a
        # maximum age. A static frontend is never rewritten after a deploy, so its
        # objects age indefinitely and can be swept, leaving the site serving an
        # S3 error page until someone redeploys by hand.
        # Versioning (above) makes that recoverable and the alarms (below) make it
        # visible — this is the piece that stops it happening at all, by refreshing
        # every object's LastModified on a schedule well inside any such window.
        keepalive_fn = _lambda.Function(
            self, "SiteKeepAliveFn",
            function_name="credly-site-keepalive",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/site_keepalive"),
            environment={
                "HOSTING_BUCKET": self.bucket.bucket_name,
                "DISTRIBUTION_ID": self.distribution.distribution_id,
            },
            timeout=Duration.minutes(5),
            memory_size=256,
        )
        self.bucket.grant_read_write(keepalive_fn)
        # grant_read_write covers object CRUD, but restoring a swept bucket also
        # needs the version-scoped calls: listing versions to find delete markers
        # and deleting the marker itself.
        keepalive_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucketVersions", "s3:GetObjectVersion"],
                resources=[self.bucket.bucket_arn, self.bucket.arn_for_objects("*")],
            )
        )
        keepalive_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObjectVersion"],
                resources=[self.bucket.arn_for_objects("*")],
            )
        )
        keepalive_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                # CreateInvalidation can be scoped to the distribution ARN, which
                # is safer than the "*" this would otherwise need.
                resources=[
                    Stack.of(self).format_arn(
                        service="cloudfront",
                        region="",
                        resource="distribution",
                        resource_name=self.distribution.distribution_id,
                    )
                ],
            )
        )

        # Weekly, not daily: a 7-day refresh leaves a wide margin under any
        # realistic age threshold, so a single missed run still can't lose the site.
        events.Rule(
            self, "SiteKeepAliveRule",
            rule_name="credly-site-keepalive",
            schedule=events.Schedule.rate(Duration.days(7)),
            targets=[targets.LambdaFunction(keepalive_fn)],
        )

        # ─── Site-health alarms ───
        # This is the detection layer the original outage was missing: the bucket
        # sat empty for ~17 days with nothing watching. These alarms notify a human.
        # They publish to a dedicated topic (with a real email subscription) rather
        # than the expiration topic, whose only subscriber is a Lambda.
        self.alerts_topic = sns.Topic(
            self,
            "SiteAlertsTopic",
            display_name="CertTracker-Site-Health",
        )
        if alert_email:
            self.alerts_topic.add_subscription(subs.EmailSubscription(alert_email))
        alarm_action = cw_actions.SnsAction(self.alerts_topic)

        # Empty-bucket alarm — the exact failure that took the site down. S3 emits
        # NumberOfObjects once a day; 0 objects means the deployed frontend is gone.
        # (With the 403->index.html rewrite an empty bucket now returns 200, so a
        # CloudFront 4xx alarm would NOT catch this — object count is the true signal.)
        empty_bucket = cloudwatch.Alarm(
            self,
            "BucketEmptyAlarm",
            alarm_name="CertTracker-Hosting-BucketEmpty",
            metric=cloudwatch.Metric(
                namespace="AWS/S3",
                metric_name="NumberOfObjects",
                dimensions_map={
                    "BucketName": self.bucket.bucket_name,
                    "StorageType": "AllStorageTypes",
                },
                period=Duration.days(1),
                statistic="Average",
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Hosting bucket has 0 objects — the dashboard frontend is missing.",
        )
        empty_bucket.add_alarm_action(alarm_action)

        # Serving-error alarm — catches origin/serving failures the object-count
        # metric can't (e.g. broken OAC/bucket policy) and does so in ~10 min.
        serving_errors = cloudwatch.Alarm(
            self,
            "Cf5xxAlarm",
            alarm_name="CertTracker-Hosting-5xx",
            metric=cloudwatch.Metric(
                namespace="AWS/CloudFront",
                metric_name="5xxErrorRate",
                dimensions_map={
                    "DistributionId": self.distribution.distribution_id,
                    "Region": "Global",
                },
                period=Duration.minutes(5),
                statistic="Average",
            ),
            threshold=5,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="CloudFront 5xx error rate above 5% for 10 minutes.",
        )
        serving_errors.add_alarm_action(alarm_action)
