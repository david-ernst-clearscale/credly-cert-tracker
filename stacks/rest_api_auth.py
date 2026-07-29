"""Patch to add Cognito authorizer to REST API."""
from aws_cdk import aws_apigateway as apigw


def add_cognito_auth(api: apigw.RestApi, user_pool):
    """Add Cognito authorizer to all methods on the API."""
    authorizer = apigw.CognitoUserPoolsAuthorizer(
        api, "CertTrackerAuthorizer",
        cognito_user_pools=[user_pool],
    )
    return authorizer
