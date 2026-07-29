import os
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
CERTS_TABLE = os.environ["CERTS_TABLE"]
certs_table = dynamodb.Table(CERTS_TABLE)


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
        if not expires_at:
            continue

        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        days_until = (expiry - now).days

        if days_until < 0:
            new_status = "expired"
        elif days_until <= 30:
            new_status = "critical"
        elif days_until <= 60:
            new_status = "expiring_soon"
        elif days_until <= 90:
            new_status = "upcoming_renewal"
        else:
            new_status = "active"

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
