"""REST API construct for dashboard data."""
from aws_cdk import (
    Duration, CfnOutput, RemovalPolicy,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_s3 as s3,
)
from constructs import Construct


class DashboardRestApiConstruct(Construct):
    def __init__(self, scope, id, certs_table, users_table, allowed_origin, user_pool=None,
                 badge_sync_fn=None, admin_emails="", **kwargs):
        super().__init__(scope, id, **kwargs)

        # Private bucket holding the uploaded APN roster (parsed CSV → JSON). The
        # dashboard API reads it to build the APN Network tab and overwrites it on
        # each upload. Not public — only the API Lambda can read/write it.
        roster_bucket = s3.Bucket(
            self, "ApnRosterBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Cognito authorizer (if user_pool provided)
        self.authorizer = None
        if user_pool:
            self.authorizer = apigw.CognitoUserPoolsAuthorizer(
                self, "CognitoAuth",
                cognito_user_pools=[user_pool],
            )

        # Lambda handler
        api_handler = _lambda.Function(
            self, "DashboardApiFn",
            function_name="credly-dashboard-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/dashboard_api"),
            environment={
                "CERTS_TABLE": certs_table.table_name,
                "USERS_TABLE": users_table.table_name,
                "ALLOWED_ORIGIN": allowed_origin,
                "ROSTER_BUCKET": roster_bucket.bucket_name,
                # Emails allowed to add/edit users and trigger a sync from the UI.
                "ADMIN_EMAILS": admin_emails,
                # Name of the badge-sync function the "Sync now" button invokes.
                "BADGE_SYNC_FUNCTION": badge_sync_fn.function_name if badge_sync_fn else "",
            },
            timeout=Duration.seconds(30),
            memory_size=256,
        )
        certs_table.grant_read_data(api_handler)
        # read/write: the Users tab adds and edits records in the users table.
        users_table.grant_read_write_data(api_handler)
        roster_bucket.grant_read_write(api_handler)
        # Let the API invoke the badge-sync Lambda for the "Sync now" button.
        if badge_sync_fn:
            badge_sync_fn.grant_invoke(api_handler)

        # REST API
        api = apigw.RestApi(
            self, "DashboardApi",
            rest_api_name="cert-tracker-dashboard",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[allowed_origin],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        integration = apigw.LambdaIntegration(api_handler)

        # Attach authorizer if available
        method_options = {}
        if self.authorizer:
            method_options["authorizer"] = self.authorizer
            method_options["authorization_type"] = apigw.AuthorizationType.COGNITO

        compliance_resource = api.root.add_resource("compliance")
        compliance_resource.add_method("GET", integration, **method_options)

        # POST /apn-roster — upload a new APN CSV export; the Lambda parses and stores it.
        roster_resource = api.root.add_resource("apn-roster")
        roster_resource.add_method("POST", integration, **method_options)

        # /users — GET lists all Credly users (any signed-in user); POST adds/edits
        # one (admin-only, enforced in the Lambda).
        users_resource = api.root.add_resource("users")
        users_resource.add_method("GET", integration, **method_options)
        users_resource.add_method("POST", integration, **method_options)

        # POST /sync — trigger an on-demand badge sync (admin-only).
        sync_resource = api.root.add_resource("sync")
        sync_resource.add_method("POST", integration, **method_options)

        # Add CORS headers to API Gateway's own error responses (401/403/etc.) —
        # otherwise a rejected/expired auth token comes back with no CORS headers
        # and the browser reports it as a CORS failure instead of the real status.
        for resp_type, resp_id in [
            (apigw.ResponseType.DEFAULT_4_XX, "Default4xxCors"),
            (apigw.ResponseType.DEFAULT_5_XX, "Default5xxCors"),
        ]:
            apigw.GatewayResponse(
                self, resp_id,
                rest_api=api,
                type=resp_type,
                response_headers={
                    "Access-Control-Allow-Origin": f"'{allowed_origin}'",
                    "Access-Control-Allow-Headers": "'Authorization,Content-Type'",
                },
            )

        CfnOutput(self, "ApiUrl", value=api.url + "compliance")
