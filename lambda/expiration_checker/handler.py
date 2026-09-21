import os
import logging
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
CERTS_TABLE = os.environ["CERTS_TABLE"]
certs_table = dynamodb.Table(CERTS_TABLE)


def parse_expiration(expires_at: str | None) -> datetime | None:
    """Parse an expiration timestamp, returning None for missing or invalid dates."""
    if not expires_at or expires_at == "no-expiry":
        return None

    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"Flagging invalid expiration date as bad_expiry: {expires_at}")
        return None

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry


def compute_status(expires_at: str | None) -> str | None:
    """Compute certification status, or None when no status update should run."""
    if not expires_at or expires_at == "no-expiry":
        return None

    expiry = parse_expiration(expires_at)
    if expiry is None:
        return "bad_expiry"

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


def lambda_handler(event: dict, context) -> dict:
    """Daily job to update status of all certs based on current date."""
    logger.info("Running expiration status check")

    response = certs_table.scan()
    certs = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = certs_table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        certs.extend(response.get("Items", []))

    updated = 0
    now = datetime.now(timezone.utc)

    for cert in certs:
        expires_at = cert.get("expires_at")
        new_status = compute_status(expires_at)
        if new_status is None:
            continue

        if cert.get("status") != new_status:
            certs_table.update_item(
                Key={
                    "employee_id": cert["employee_id"],
                    "certification_id": cert["certification_id"],
                },
                UpdateExpression="SET #s = :s, last_status_change = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": new_status,
                    ":ts": now.isoformat(),
                },
            )
            updated += 1

    logger.info(f"Updated {updated} cert statuses out of {len(certs)} total")
    return {"updated": updated, "total": len(certs)}
