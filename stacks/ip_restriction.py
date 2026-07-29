from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from constructs import Construct


class IpRestrictionConstruct(Construct):
    """CloudFront Function that restricts access to allowed IPs."""

    def __init__(self, scope: Construct, id: str, *, allowed_ips: list[str]):
        super().__init__(scope, id)

        # Build the IP check logic
        ip_list = ", ".join([f'"{ip}"' for ip in allowed_ips])

        function_code = f"""
function handler(event) {{
    var allowedIps = [{ip_list}];
    var clientIp = event.viewer.ip;

    var allowed = false;
    for (var i = 0; i < allowedIps.length; i++) {{
        if (clientIp === allowedIps[i]) {{
            allowed = true;
            break;
        }}
    }}

    if (!allowed) {{
        return {{
            statusCode: 403,
            statusDescription: 'Forbidden',
            headers: {{ 'content-type': {{ value: 'text/html' }} }},
            body: '<h1>403 - Access Denied</h1><p>Your IP is not authorized.</p>'
        }};
    }}

    return event.request;
}}
"""

        self.function = cloudfront.Function(
            self, "IpCheckFunction",
            function_name="credly-dashboard-ip-check",
            code=cloudfront.FunctionCode.from_inline(function_code),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )
