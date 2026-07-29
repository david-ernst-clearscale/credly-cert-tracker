"""REST API construct for dashboard data."""
from aws_cdk import (
    Duration, CfnOutput,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class DashboardRestApiConstruct(Construct):
    def __init__(self, scope: Construct, id: str, *,
                 certs_table: dynamodb.Table,
                 users_table: dynamodb.Table):
        super().__init__(scope, id)

        self.handler = _lambda.Function(
            self, "DashboardApiFn",
            function_name="credly-dashboard-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/dashboard_api"),
            environment={
                "CERTS_TABLE": certs_table.table_name,
                "USERS_TABLE": users_table.table_name,
            },
            timeout=Duration.seconds(10),
            memory_size=256,
        )
        certs_table.grant_read_data(self.handler)
        users_table.grant_read_data(self.handler)

        self.api = apigw.RestApi(
            self, "DashboardApi",
            rest_api_name="CertTracker-Dashboard-API",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "OPTIONS"],
            ),
        )

        compliance_resource = self.api.root.add_resource("compliance")
        compliance_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.handler),
        )

        CfnOutput(scope, "DashboardApiUrl",
                  value=self.api.url)
