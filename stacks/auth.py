from aws_cdk import (
    SecretValue,
    aws_cognito as cognito,
    CfnOutput,
)
from constructs import Construct


class AuthConstruct(Construct):
    def __init__(self, scope, id, cloudfront_domain, google_client_id, google_client_secret, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name="cert-tracker-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
        )

        self.user_pool_domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix="clearscale-cert-tracker"
            ),
        )

        google_idp = cognito.UserPoolIdentityProviderGoogle(
            self, "GoogleIdP",
            user_pool=self.user_pool,
            client_id=google_client_id,
            client_secret_value=SecretValue.unsafe_plain_text(google_client_secret),
            scopes=["openid", "email", "profile"],
            attribute_mapping=cognito.AttributeMapping(
                email=cognito.ProviderAttribute.GOOGLE_EMAIL,
                fullname=cognito.ProviderAttribute.GOOGLE_NAME,
            ),
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name="cert-tracker-web",
            auth_flows=cognito.AuthFlow(user_srp=True),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.GOOGLE
            ],
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=["https://" + cloudfront_domain],
                logout_urls=["https://" + cloudfront_domain],
            ),
        )
        self.user_pool_client.node.add_dependency(google_idp)

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain",
            value="https://" + self.user_pool_domain.domain_name + ".auth.us-east-1.amazoncognito.com")
