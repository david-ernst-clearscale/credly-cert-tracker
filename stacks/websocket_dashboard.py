from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_lambda as _lambda,
    aws_lambda_event_sources as event_sources,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
)
from constructs import Construct


class WebSocketDashboardConstruct(Construct):
    def __init__(self, scope: Construct, id: str, *, certs_table: dynamodb.Table, users_table: dynamodb.Table):
        super().__init__(scope, id)

        self.connections_table = dynamodb.Table(
            self, "ConnectionsTable",
            table_name="credly-ws-connections",
            partition_key=dynamodb.Attribute(name="connection_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",
        )

        connect_fn = _lambda.Function(
            self, "ConnectHandler",
            function_name="credly-ws-connect",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_inline(
                "import os, json, time, boto3\n"
                "table = boto3.resource('dynamodb').Table(os.environ['CONNECTIONS_TABLE'])\n"
                "def handler(event, ctx):\n"
                "  cid = event['requestContext']['connectionId']\n"
                "  table.put_item(Item={'connection_id': cid, 'ttl': int(time.time()) + 86400})\n"
                "  return {'statusCode': 200}\n"
            ),
            environment={"CONNECTIONS_TABLE": self.connections_table.table_name},
            timeout=Duration.seconds(10),
        )
        self.connections_table.grant_write_data(connect_fn)

        disconnect_fn = _lambda.Function(
            self, "DisconnectHandler",
            function_name="credly-ws-disconnect",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_inline(
                "import os, boto3\n"
                "table = boto3.resource('dynamodb').Table(os.environ['CONNECTIONS_TABLE'])\n"
                "def handler(event, ctx):\n"
                "  table.delete_item(Key={'connection_id': event['requestContext']['connectionId']})\n"
                "  return {'statusCode': 200}\n"
            ),
            environment={"CONNECTIONS_TABLE": self.connections_table.table_name},
            timeout=Duration.seconds(10),
        )
        self.connections_table.grant_write_data(disconnect_fn)

        self.ws_api = apigwv2.WebSocketApi(
            self, "WsApi",
            api_name="credly-cert-tracker-ws",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("ConnectInt", connect_fn),
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("DisconnectInt", disconnect_fn),
            ),
        )

        self.ws_stage = apigwv2.WebSocketStage(
            self, "ProdStage",
            web_socket_api=self.ws_api,
            stage_name="prod",
            auto_deploy=True,
        )

        CfnOutput(self, "WebSocketUrl",
            value=f"wss://{self.ws_api.api_id}.execute-api.{Stack.of(self).region}.amazonaws.com/prod")
