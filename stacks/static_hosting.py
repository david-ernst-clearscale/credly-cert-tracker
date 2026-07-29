from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput,
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from stacks.ip_restriction import IpRestrictionConstruct


class StaticHostingConstruct(Construct):
    def __init__(self, scope: Construct, id: str, *, build_path: str = "./frontend/dist", allowed_ips: list[str] = None):
        super().__init__(scope, id)

        self.bucket = s3.Bucket(
            self, "DashboardBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # IP restriction (if IPs provided)
        ip_function = None
        if allowed_ips:
            ip_restrict = IpRestrictionConstruct(self, "IpRestrict", allowed_ips=allowed_ips)
            ip_function = ip_restrict.function

        # Build behavior options
        behavior_kwargs = {
            "origin": origins.S3BucketOrigin.with_origin_access_control(self.bucket),
            "viewer_protocol_policy": cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        }

        if ip_function:
            behavior_kwargs["function_associations"] = [
                cloudfront.FunctionAssociation(
                    function=ip_function,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )
            ]

        self.distribution = cloudfront.Distribution(
            self, "Distribution",
            default_behavior=cloudfront.BehaviorOptions(**behavior_kwargs),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        s3_deploy.BucketDeployment(
            self, "Deploy",
            sources=[s3_deploy.Source.asset(build_path)],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "DashboardUrl",
            value=f"https://{self.distribution.distribution_domain_name}")
