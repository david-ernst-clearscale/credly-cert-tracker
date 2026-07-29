import os
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
lambda_client = boto3.client("lambda")

CERTS_TABLE = os.environ["CERTS_TABLE"]
USERS_TABLE = os.environ["USERS_TABLE"]
NOTIFICATION_LAMBDA = os.environ.get("NOTIFICATION_LAMBDA_ARN", "")

certs_table = dynamodb.Table(CERTS_TABLE)
users_table = dynamodb.Table(USERS_TABLE)

THRESHOLDS = {
    "Foundational": 10,
    "Technical": 25,
    "Professional/Specialty": 10,
}


def lambda_handler(event: dict, context) -> dict:
    """Check compliance status and publish CloudWatch metrics."""
    logger.info("Running compliance report")

    certs = get_active_certs()
    counts = defaultdict(int)
    expiring_soon = defaultdict(int)
    unique_holders = defaultdict(set)

    for cert in certs:
        category = cert.get("partner_tier_category", "Other")
        if category == "Other":
            continue
        counts[category] += 1
        unique_holders[category].add(cert["employee_id"])
        if cert.get("status") in ("upcoming_renewal", "expiring_soon", "critical"):
            expiring_soon[category] += 1

    # Publish CloudWatch metrics and check thresholds
    breaches = []

    for tier, required in THRESHOLDS.items():
        current = counts.get(tier, 0)
        at_risk = expiring_soon.get(tier, 0)
        percentage = (current / required * 100) if required > 0 else 100
        projected = current - at_risk
        projected_pct = (projected / required * 100) if required > 0 else 100

        if percentage >= 100:
            risk_level = "GREEN"
        elif percentage >= 80:
            risk_level = "YELLOW"
        else:
            risk_level = "RED"

        # Publish metrics
        publish_metrics(tier, current, required, percentage, at_risk, projected_pct, risk_level)

        # Check for breach
        if risk_level == "RED":
            breaches.append({
                "tier": tier,
                "current": current,
                "required": required,
                "risk_level": risk_level,
            })

    # Trigger notifications for breaches
    for breach in breaches:
        notify_breach(breach)

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tiers": {
            tier: {
                "current": counts.get(tier, 0),
                "required": req,
                "percentage": round((counts.get(tier, 0) / req * 100), 1) if req > 0 else 100,
            }
            for tier, req in THRESHOLDS.items()
        },
        "breaches": len(breaches),
    }

    logger.info(f"Compliance report: {result}")
    return result


def get_active_certs() -> list:
    response = certs_table.scan(
        FilterExpression=Attr("status").ne("expired")
    )
    certs = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = certs_table.scan(
            FilterExpression=Attr("status").ne("expired"),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        certs.extend(response.get("Items", []))
    return certs


def publish_metrics(tier, current, required, percentage, at_risk, projected_pct, risk_level):
    namespace = "CredlyCertTracker"
    dimensions = [{"Name": "Tier", "Value": tier}]

    cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {"MetricName": "CurrentCerts", "Value": current, "Unit": "Count", "Dimensions": dimensions},
            {"MetricName": "RequiredCerts", "Value": required, "Unit": "Count", "Dimensions": dimensions},
            {"MetricName": "CompliancePercentage", "Value": percentage, "Unit": "Percent", "Dimensions": dimensions},
            {"MetricName": "ExpiringWithin90Days", "Value": at_risk, "Unit": "Count", "Dimensions": dimensions},
            {"MetricName": "ProjectedCompliance", "Value": projected_pct, "Unit": "Percent", "Dimensions": dimensions},
        ],
    )


def notify_breach(breach: dict):
    if not NOTIFICATION_LAMBDA:
        return
    try:
        lambda_client.invoke(
            FunctionName=NOTIFICATION_LAMBDA,
            InvocationType="Event",
            Payload=json.dumps({"type": "compliance_breach", **breach}),
        )
    except Exception as e:
        logger.error(f"Failed to invoke notification: {e}")
