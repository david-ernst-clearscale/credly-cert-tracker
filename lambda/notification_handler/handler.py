import os
import json
import logging
from datetime import datetime, timezone
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ses = boto3.client("ses")
dynamodb = boto3.resource("dynamodb")

USERS_TABLE = os.environ["USERS_TABLE"]
FROM_EMAIL = os.environ.get("FROM_EMAIL", "certs@yourcompany.com")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

users_table = dynamodb.Table(USERS_TABLE)


def lambda_handler(event: dict, context) -> dict:
    """Handle notification events (expiry reminders, status changes)."""
    event_type = event.get("type", "")

    if event_type == "expiry_reminder":
        return handle_expiry_reminder(event)
    elif event_type == "compliance_breach":
        return handle_compliance_breach(event)
    else:
        logger.warning(f"Unknown event type: {event_type}")
        return {"statusCode": 400, "body": f"Unknown type: {event_type}"}


def handle_expiry_reminder(event: dict) -> dict:
    """Send expiry reminder to employee."""
    employee_id = event["employee_id"]
    cert_name = event["certification_name"]
    days_remaining = event["days_remaining"]
    expires_at = event["expires_at"]

    user = get_user(employee_id)
    if not user:
        logger.warning(f"User not found: {employee_id}")
        return {"statusCode": 404}

    email = user.get("email")
    name = user.get("name", employee_id)

    # Send email via SES
    if email:
        subject = f"⚠️ AWS Cert Expiring in {days_remaining} Days: {cert_name}"
        body = f"""Hi {name},

Your AWS certification "{cert_name}" will expire on {expires_at[:10]}.

You have {days_remaining} days remaining to renew.

Action needed:
- Schedule your recertification exam at aws.training
- AWS Partner tier compliance requires active certifications (no grace period)

Renew here: https://www.aws.training/certification

— Cert Tracker Bot
"""
        send_email(email, subject, body)

    # Send Slack notification
    if SLACK_WEBHOOK_URL:
        emoji = "🔴" if days_remaining <= 30 else "🟡" if days_remaining <= 60 else "📋"
        slack_msg = f"{emoji} *{cert_name}* for {name} expires in *{days_remaining} days* ({expires_at[:10]})"
        send_slack(slack_msg)

    return {"statusCode": 200, "sent_to": email}


def handle_compliance_breach(event: dict) -> dict:
    """Alert leadership about compliance breach."""
    tier = event.get("tier", "Unknown")
    current = event.get("current", 0)
    required = event.get("required", 0)
    risk_level = event.get("risk_level", "RED")

    subject = f"🚨 APN Compliance Breach: {tier} Tier"
    body = f"""ALERT: AWS Partner Network compliance breach detected.

Tier: {tier}
Current: {current} certifications
Required: {required} certifications
Status: {risk_level}

Immediate action required — AWS does not provide a grace period for lapsed certifications.

— Cert Tracker Bot
"""

    # Send to all admins
    admins = get_admins()
    for admin in admins:
        if admin.get("email"):
            send_email(admin["email"], subject, body)

    # Slack alert
    if SLACK_WEBHOOK_URL:
        send_slack(f"🚨 *COMPLIANCE BREACH* — {tier}: {current}/{required} certs. Action required!")

    return {"statusCode": 200, "notified": len(admins)}


def get_user(employee_id: str) -> dict | None:
    try:
        resp = users_table.get_item(Key={"employee_id": employee_id})
        return resp.get("Item")
    except Exception:
        return None


def get_admins() -> list:
    resp = users_table.scan(
        FilterExpression="contains(#r, :admin)",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={":admin": "admin"},
    )
    return resp.get("Items", [])


def send_email(to: str, subject: str, body: str):
    try:
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        logger.info(f"Email sent to {to}: {subject}")
    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")


def send_slack(message: str):
    try:
        data = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Slack notification sent")
    except Exception as e:
        logger.error(f"Failed to send Slack: {e}")
