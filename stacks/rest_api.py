"""REST API construct for dashboard data."""
from aws_cdk import (
    Duration, CfnOutput,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
)
from constructs import Construct


class DashboardRestApiConstruct(Construct):
    def __init__(self, scope, id, certs_table, users_table, user_pool=None, **kwargs):
        super().__init__(scope, id, **kwargs)

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
            },
            timeout=Duration.seconds(30),
            memory_size=256,
        )
        certs_table.grant_read_data(api_handler)
        users_table.grant_read_data(api_handler)

        # REST API
        api = apigw.RestApi(
            self, "DashboardApi",
            rest_api_name="cert-tracker-dashboard",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=["https://d3ekm65uptt6j8.cloudfront.net"],
                allow_methods=["GET", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        integration = apigw.LambdaIntegration(api_handler)
        compliance_resource = api.root.add_resource("compliance")

        # Attach authorizer if available
        method_options = {}
        if self.authorizer:
            method_options["authorizer"] = self.authorizer
            method_options["authorization_type"] = apigw.AuthorizationType.COGNITO
        compliance_resource.add_method("GET", integration, **method_options)

        CfnOutput(self, "ApiUrl", value=api.url + "compliance")
