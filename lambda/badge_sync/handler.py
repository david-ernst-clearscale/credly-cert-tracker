import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any
import urllib.request
import urllib.error

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")

USERS_TABLE = os.environ["USERS_TABLE"]
CERTS_TABLE = os.environ["CERTS_TABLE"]
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
SCHEDULER_GROUP = os.environ.get("SCHEDULER_GROUP", "credly-expiry-reminders")
REMINDER_DAYS = [90, 60, 30]

users_table = dynamodb.Table(USERS_TABLE)
certs_table = dynamodb.Table(CERTS_TABLE)

AWS_CERT_ISSUERS = [
    "Amazon Web Services Training and Certification",
    "Amazon Web Services",
    "AWS",
]


def lambda_handler(event: dict, context: Any) -> dict:
    """Daily badge sync: fetch badges from Credly for all opted-in users."""
    logger.info("Starting daily badge sync")

    users = get_opted_in_users()
    logger.info(f"Processing {len(users)} users")

    results = {"synced": 0, "errors": 0, "new_certs": 0, "updated": 0}

    for user in users:
        try:
            sync_user_badges(user, results)
        except Exception as e:
            logger.error(f"Error syncing user {user.get('credly_username')}: {e}")
            results["errors"] += 1

    logger.info(f"Sync complete: {results}")
    return results


def get_opted_in_users() -> list:
    """Get all users who have consented to tracking."""
    response = users_table.scan(
        FilterExpression="consent_status = :status",
        ExpressionAttributeValues={":status": "opted_in"},
    )
    users = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = users_table.scan(
            FilterExpression="consent_status = :status",
            ExpressionAttributeValues={":status": "opted_in"},
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        users.extend(response.get("Items", []))
    return users


def sync_user_badges(user: dict, results: dict):
    """Fetch and process badges for a single user."""
    username = user["credly_username"]
    employee_id = user["employee_id"]

    badges = fetch_credly_badges(username)
    aws_badges = [b for b in badges if is_aws_badge(b)]

    logger.info(f"User {username}: {len(badges)} total badges, {len(aws_badges)} AWS")

    for badge in aws_badges:
        cert_id = badge["id"]
        cert_name = badge["badge_template"]["name"]
        issued_at = badge.get("issued_at")
        expires_at = badge.get("expires_at")

        # Determine status
        status = compute_status(expires_at)

        # Determine partner tier category
        category = classify_certification(cert_name)

        # Upsert to DynamoDB
        existing = get_existing_cert(employee_id, cert_id)

        item = {
            "employee_id": employee_id,
            "certification_id": cert_id,
            "certification_name": cert_name,
            "credly_username": username,
            "issued_at": issued_at,
            "expires_at": expires_at or "no-expiry",
            "status": status,
            "partner_tier_category": category,
            "badge_url": badge.get("badge_url", ""),
            "last_synced": datetime.now(timezone.utc).isoformat(),
        }

        certs_table.put_item(Item=item)

        if existing:
            results["updated"] += 1
        else:
            results["new_certs"] += 1
            # Create expiry reminder schedules for new certs
            if expires_at:
                create_reminder_schedules(employee_id, cert_id, cert_name, expires_at)

    results["synced"] += 1


def fetch_credly_badges(username: str) -> list:
    """Fetch badges from Credly's public JSON endpoint."""
    url = f"https://www.credly.com/users/{username}/badges.json"
    headers = {"Accept": "application/json", "User-Agent": "CertTracker/1.0"}

    all_badges = []
    page = 1

    while True:
        page_url = f"{url}?page={page}&page_size=48"
        req = urllib.request.Request(page_url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.warning(f"User {username} not found on Credly")
                return []
            raise

        badges = data.get("data", [])
        if not badges:
            break

        all_badges.extend(badges)
        page += 1

        if len(badges) < 48:
            break

    return all_badges


def is_aws_badge(badge: dict) -> bool:
    """Check if a badge is a valid certification (AWS or Claude)."""
    name = badge.get("badge_template", {}).get("name", "")
    if "AWS Certified" in name:
        return True
    if "Claude Certified" in name:
        return True
    return False

def compute_status(expires_at: str | None) -> str:
    """Compute certification status based on expiration date."""
    if not expires_at:
        return "active"

    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days_until = (expiry - now).days

    if days_until < 0:
        return "expired"
    elif days_until <= 30:
        return "critical"
    elif days_until <= 60:
        return "expiring_soon"
    elif days_until <= 90:
        return "upcoming_renewal"
    return "active"


def classify_certification(cert_name: str) -> str:
    """Classify cert into APN partner tier category."""
    name_lower = cert_name.lower()

    if "professional" in name_lower or "specialty" in name_lower:
        return "Professional/Specialty"
    elif "practitioner" in name_lower or "foundational" in name_lower:
        return "Foundational"
    elif "associate" in name_lower:
        return "Technical"
    return "Technical"


def get_existing_cert(employee_id: str, cert_id: str) -> dict | None:
    """Check if cert already exists in DynamoDB."""
    try:
        response = certs_table.get_item(
            Key={"employee_id": employee_id, "certification_id": cert_id}
        )
        return response.get("Item")
    except Exception:
        return None


def create_reminder_schedules(employee_id: str, cert_id: str, cert_name: str, expires_at: str):
    """Create EventBridge one-time schedules for expiry reminders."""
    if not SCHEDULER_ROLE_ARN:
        logger.info("Skipping scheduler - no SCHEDULER_ROLE_ARN configured")
        return
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

    for days in REMINDER_DAYS:
        reminder_date = expiry - timedelta(days=days)
        if reminder_date <= datetime.now(timezone.utc):
            continue

        schedule_name = f"{employee_id}-{cert_id}-{days}d"

        try:
            scheduler.create_schedule(
                Name=schedule_name,
                GroupName=SCHEDULER_GROUP,
                ScheduleExpression=f"at({reminder_date.strftime('%Y-%m-%dT%H:%M:%S')})",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": os.environ["NOTIFICATION_LAMBDA_ARN"],
                    "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
                    "Input": json.dumps({
                        "type": "expiry_reminder",
                        "employee_id": employee_id,
                        "certification_id": cert_id,
                        "certification_name": cert_name,
                        "expires_at": expires_at or "no-expiry",
                        "days_remaining": days,
                    }),
                },
                ActionAfterCompletion="DELETE",
            )
        except scheduler.exceptions.ConflictException:
            pass  # Schedule already exists
