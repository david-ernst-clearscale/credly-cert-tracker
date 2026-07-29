#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.credly_cert_tracker_stack import CredlyCertTrackerStack

app = cdk.App()
CredlyCertTrackerStack(app, "CredlyCertTrackerStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region="us-east-1",
    ),
)
app.synth()
