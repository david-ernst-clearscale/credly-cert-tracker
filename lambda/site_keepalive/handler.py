"""Keep the dashboard's hosting bucket from ageing out.

Where an account runs scheduled housekeeping that deletes S3 objects past a
maximum age, a static frontend is at risk: once a build lands it is never
rewritten, so its objects keep ageing and are eventually swept, leaving the
dashboard serving an S3 error page until someone redeploys by hand.

This runs weekly and does two things:

  1. Refresh — self-copies every object so LastModified resets to now. Age-based
     housekeeping keys off LastModified, so an object touched every 7 days stays
     comfortably inside any realistic threshold. This is the actual prevention.
  2. Restore — if a sweep already happened, the bucket is versioned, so a plain
     DELETE left the bytes behind a delete marker. Dropping the latest delete
     marker republishes the object with no redeploy and no access to the build.

Restore runs first: there is no point refreshing a bucket whose current
versions are all delete markers.
"""
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
cloudfront = boto3.client("cloudfront")

BUCKET = os.environ["HOSTING_BUCKET"]
DISTRIBUTION_ID = os.environ.get("DISTRIBUTION_ID", "")

# Headers that MetadataDirective=REPLACE drops unless we re-supply them. Losing
# ContentType is not cosmetic: index.html would come back as binary/octet-stream
# and browsers would download the file instead of rendering the app.
_PRESERVED_HEADERS = (
    "ContentType",
    "CacheControl",
    "ContentEncoding",
    "ContentDisposition",
    "ContentLanguage",
)


def _restore_deleted(bucket):
    """Remove latest-version delete markers, republishing the objects under them."""
    restored = []
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        for marker in page.get("DeleteMarkers", []):
            # Only the *latest* marker hides an object. Older markers are history.
            if not marker.get("IsLatest"):
                continue
            s3.delete_object(
                Bucket=bucket, Key=marker["Key"], VersionId=marker["VersionId"]
            )
            restored.append(marker["Key"])
    if restored:
        logger.warning(
            "Restored %d object(s) hidden by delete markers — something had "
            "swept the bucket: %s",
            len(restored),
            ", ".join(sorted(restored)[:10]),
        )
    return restored


def _refresh(bucket):
    """Self-copy every current object so its LastModified resets to now."""
    refreshed = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            head = s3.head_object(Bucket=bucket, Key=key)
            # REPLACE (not COPY): S3 rejects a self-copy that changes nothing,
            # and REPLACE lets us restate the metadata verbatim.
            params = {
                "MetadataDirective": "REPLACE",
                "Metadata": head.get("Metadata", {}),
            }
            for header in _PRESERVED_HEADERS:
                if head.get(header):
                    params[header] = head[header]
            s3.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={"Bucket": bucket, "Key": key},
                **params,
            )
            refreshed += 1
    return refreshed


def _invalidate():
    if not DISTRIBUTION_ID:
        return None
    resp = cloudfront.create_invalidation(
        DistributionId=DISTRIBUTION_ID,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"site-keepalive-{os.urandom(8).hex()}",
        },
    )
    return resp["Invalidation"]["Id"]


def lambda_handler(event, context):
    restored = _restore_deleted(BUCKET)
    refreshed = _refresh(BUCKET)

    # Only invalidate when we actually republished something. Refreshing
    # LastModified doesn't change any bytes, so the edge cache stays valid.
    invalidation = _invalidate() if restored else None

    if refreshed == 0:
        # Nothing to refresh and nothing behind a delete marker means the build
        # is genuinely gone (versions hard-deleted). Needs a real deploy.
        logger.error(
            "Hosting bucket %s is empty with no recoverable versions — "
            "the frontend must be redeployed from source.",
            BUCKET,
        )

    logger.info(
        "keepalive complete: refreshed=%d restored=%d invalidation=%s",
        refreshed,
        len(restored),
        invalidation,
    )
    return {
        "bucket": BUCKET,
        "refreshed": refreshed,
        "restored": restored,
        "invalidation": invalidation,
    }
