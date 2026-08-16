"""REST API for dashboard — pulls data directly from DynamoDB."""
import os, json, csv, io, base64, logging, boto3
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
CERTS_TABLE = os.environ["CERTS_TABLE"]
USERS_TABLE = os.environ["USERS_TABLE"]
ROSTER_BUCKET = os.environ.get("ROSTER_BUCKET", "")
ROSTER_KEY = "apn_roster.json"

# Comma-separated allowlist of emails permitted to add/edit users or trigger a sync.
# Reads/list are open to any authenticated user; writes require membership here.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
BADGE_SYNC_FUNCTION = os.environ.get("BADGE_SYNC_FUNCTION", "")
lambda_client = boto3.client("lambda")

# Bundled APN roster (parsed from the last CSV export committed to the repo). Used as a
# seed/fallback until someone uploads a fresh CSV on the tab, after which the uploaded
# copy in S3 (ROSTER_BUCKET/ROSTER_KEY) takes over.
_APN_ROSTER_PATH = os.path.join(os.path.dirname(__file__), "apn_roster.json")
try:
    with open(_APN_ROSTER_PATH) as _f:
        BUNDLED_ROSTER = json.load(_f)
except (OSError, ValueError):
    BUNDLED_ROSTER = {"people": [], "redacted_count": 0, "redacted_certs": []}


def get_roster():
    """Return the active APN roster: the uploaded copy in S3 if present, else bundled."""
    if ROSTER_BUCKET:
        try:
            obj = s3.get_object(Bucket=ROSTER_BUCKET, Key=ROSTER_KEY)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except s3.exceptions.NoSuchKey:
            pass
        except Exception as e:  # bucket/permission/parse issue — fall back, don't 500
            logger.warning(f"Could not read roster from S3, using bundled: {e}")
    return BUNDLED_ROSTER


def parse_apn_csv(text):
    """Parse an APN certification CSV export into the roster structure.

    Expected headers: "User name", "User work email", "Certification name",
    "Certification level", "Award date", "Expiration date". Rows whose name/email are
    redacted ("XXXXXXX") keep their cert info but are bucketed as not-trackable.
    """
    people, redacted_certs = {}, []
    text = text.lstrip("﻿")  # strip UTF-8 BOM if the export includes one
    reader = csv.DictReader(io.StringIO(text))
    required = {"User name", "User work email", "Certification name"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError(
            "CSV is missing required columns. Expected at least: "
            "'User name', 'User work email', 'Certification name'."
        )
    for row in reader:
        name = (row.get("User name") or "").strip()
        email = (row.get("User work email") or "").strip()
        cert = {
            "name": (row.get("Certification name") or "").strip(),
            "level": (row.get("Certification level") or "").strip(),
            "award_date": (row.get("Award date") or "").split(" ")[0],
            "expiration_date": (row.get("Expiration date") or "").split(" ")[0],
        }
        if not email or email.upper().startswith("XXX") or name.upper().startswith("XXX"):
            redacted_certs.append(cert)
            continue
        # Work email is read only to detect redacted rows and de-dupe; not stored.
        p = people.setdefault(name.lower(), {"name": name, "certs": []})
        p["certs"].append(cert)
    for p in people.values():
        if p["name"] and p["name"] == p["name"].lower():
            p["name"] = p["name"].title()
    return {
        "source_file": "uploaded CSV",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "people": sorted(people.values(), key=lambda x: x["name"].lower()),
        "redacted_count": len(redacted_certs),
        "redacted_certs": redacted_certs,
    }

AWS_REQS = {"Foundational": 10, "Technical": 25, "Professional/Specialty": 10}
CLAUDE_REQS = {"CCAR-F": 10, "CCAR-P": 0, "CCDV-F": 0, "CCAO-F": 0}
FOUNDATIONAL = ["Cloud Practitioner", "AI Practitioner"]
PROFESSIONAL = ["Professional", "Specialty"]

# Badges that contain "AWS Certified" but aren't real credentials (e.g. the
# "AWS Certified AI Practitioner Early Adopter" beta badge). Excluded from all counts
# so a stale record already in DynamoDB doesn't inflate tiers/leaderboard.
NON_CERT_MARKERS = ("Early Adopter",)
def is_real_cert(name):
    return not any(m in name for m in NON_CERT_MARKERS)

def classify_aws(name):
    for kw in FOUNDATIONAL:
        if kw in name:
            return "Foundational"
    for kw in PROFESSIONAL:
        if kw in name:
            return "Professional/Specialty"
    return "Technical"

def classify_claude(name):
    if "Architect" in name and "Professional" in name:
        return "CCAR-P"
    if "Architect" in name and "Foundations" in name:
        return "CCAR-F"
    if "Developer" in name and "Foundations" in name:
        return "CCDV-F"
    if "Associate" in name and "Foundations" in name:
        return "CCAO-F"
    return "CCAO-F"

def is_active(item):
    expires = item.get("expires_at", "")
    if not expires or expires == "no-expiry":
        return True
    try:
        exp_date = datetime.fromisoformat(expires)
        now = datetime.now(timezone.utc)
        if exp_date.tzinfo is None:
            exp_date = exp_date.replace(tzinfo=timezone.utc)
        return exp_date > now
    except (ValueError, TypeError):
        return True

def _norm(s):
    """Lower-case, trim, collapse internal whitespace."""
    return " ".join((s or "").strip().lower().split())


def _identity_keys(name="", email="", employee_id="", credly_username=""):
    """Build a set of normalized identity keys for a person, tolerant of the
    different alias conventions across APN and Credly.

    The APN export and the Credly users table don't share a single reliable key:
    e.g. APN has "Jimmy Chui" / jimmy@clearscale.com while Credly stores
    employee_id "jimmy.chui". So we generate several forms of each identifier
    (dotted, spaced, email local-part) and match if ANY overlap.
    """
    keys = set()

    def add(v):
        v = _norm(v)
        if not v:
            return
        spaced = v.replace(".", " ").replace("-", " ")
        spaced = " ".join(spaced.split())
        keys.add(v)
        keys.add(spaced)
        keys.add(spaced.replace(" ", "."))

    add(name)
    add(employee_id)
    add(credly_username)
    email = _norm(email)
    if email:
        keys.add(email)
        if "@" in email:
            add(email.split("@", 1)[0])  # local part, e.g. "david.ernst"
    return {k for k in keys if k}


def get_credly_accounts():
    """Return Credly accounts (users table) each with its set of identity keys.

    A person is "on Credly" if they exist in the users table (i.e. an account has
    been connected/tracked), regardless of how many badges have synced.
    """
    table = dynamodb.Table(USERS_TABLE)
    resp = table.scan()
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    accounts = []
    for u in items:
        employee_id = u.get("employee_id", "")
        keys = _identity_keys(
            name=u.get("name", ""),
            email=u.get("email", ""),
            employee_id=employee_id,
            credly_username=u.get("credly_username", ""),
        )
        # Display name: prefer the stored name, else derive from employee_id (first.last)
        display = (u.get("name") or "").strip() or " ".join(w.capitalize() for w in employee_id.replace(".", " ").split())
        accounts.append({
            "keys": keys,
            "credly_username": u.get("credly_username", ""),
            "name": display,
            "email": u.get("email", ""),
            "employee_id": employee_id,
        })
    return accounts


def build_apn_network(roster, aws_holder_ids, aws_counts=None):
    """Cross-reference the APN roster against Credly accounts.

    matched  = APN person who has a Credly account (green check)
    missing  = APN person with no Credly account (red X — needs account connected)
    credly_only = Credly users who hold an AWS cert but aren't on the APN list.
                  aws_holder_ids = employee_ids that have at least one AWS cert synced;
                  people with only Anthropic certs (or no AWS certs) are excluded.
    """
    accounts = get_credly_accounts()
    matched, missing = [], []
    all_roster_keys = set()
    for person in roster.get("people", []):
        person_keys = _identity_keys(name=person.get("name", ""), email=person.get("email", ""))
        all_roster_keys |= person_keys
        entry = {
            "name": person.get("name", ""),
            "apn_cert_count": len(person.get("certs", [])),
            "certs": person.get("certs", []),
        }
        account = next((a for a in accounts if a["keys"] & person_keys), None)
        if account:
            entry["credly_username"] = account["credly_username"]
            matched.append(entry)
        else:
            missing.append(entry)
    matched.sort(key=lambda e: e["name"].lower())
    missing.sort(key=lambda e: e["name"].lower())

    # Reverse direction: Credly users who hold an AWS cert but are NOT on the APN list.
    # Excludes anyone without an AWS cert (e.g. Anthropic-only or no certs).
    aws_counts = aws_counts or {}
    credly_only = [
        {"name": a["name"], "credly_username": a["credly_username"],
         "aws_cert_count": aws_counts.get(a["employee_id"], 0)}
        for a in accounts
        if a["employee_id"] in aws_holder_ids and not (a["keys"] & all_roster_keys)
    ]
    # Most AWS certs first (prioritize who to get onto the APN list), then alphabetical.
    credly_only.sort(key=lambda e: (-e.get("aws_cert_count", 0), e["name"].lower()))
    # Subtotal the redacted ("Not Trackable") records by certification so we can see
    # what APN reports in that bucket even though the owners are masked.
    redacted_certs = roster.get("redacted_certs", [])
    counts = defaultdict(int)
    for r in redacted_certs:
        counts[classify_aws(r.get("name", ""))] += 1
    redacted_breakdown = [
        {"tier": t, "count": counts.get(t, 0)}
        for t in AWS_REQS  # Foundational, Technical, Professional/Specialty — same 3 groupings
    ]
    return {
        "matched": matched,
        "missing": missing,
        "credly_only": credly_only,
        "redacted_count": roster.get("redacted_count", len(redacted_certs)),
        "redacted_breakdown": redacted_breakdown,
        "total_named": len(matched) + len(missing),
        "uploaded_at": roster.get("uploaded_at", ""),
    }


def _headers():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "")}


def _resp(status, body):
    return {"statusCode": status, "headers": _headers(), "body": json.dumps(body)}


def handle_roster_upload(event):
    """POST /apn-roster — parse an uploaded CSV, store it in S3, return a summary."""
    if not ROSTER_BUCKET:
        return _resp(500, {"error": "Roster storage is not configured (ROSTER_BUCKET unset)."})
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    if not body.strip():
        return _resp(400, {"error": "Empty upload — no CSV content received."})
    try:
        roster = parse_apn_csv(body)
    except ValueError as e:
        return _resp(400, {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to parse APN CSV: {e}")
        return _resp(400, {"error": "Could not parse the CSV. Check that it's the APN certification export."})
    s3.put_object(
        Bucket=ROSTER_BUCKET, Key=ROSTER_KEY,
        Body=json.dumps(roster).encode("utf-8"), ContentType="application/json",
    )
    logger.info(f"Stored APN roster: {len(roster['people'])} people, {roster['redacted_count']} redacted")
    return _resp(200, {
        "ok": True,
        "named_people": len(roster["people"]),
        "redacted_count": roster["redacted_count"],
        "uploaded_at": roster["uploaded_at"],
    })


def handle_compliance(event, context=None):
    table = dynamodb.Table(CERTS_TABLE)
    items = table.scan().get("Items", [])
    # employee_ids that hold at least one AWS cert on Credly (used to scope the
    # "In App, Not on APN" list to AWS-cert holders only).
    aws_holder_ids = {
        it.get("employee_id", "") for it in items
        if "AWS Certified" in it.get("certification_name", "")
        and is_real_cert(it.get("certification_name", ""))
    }
    aws_grouped = {"Foundational": [], "Technical": [], "Professional/Specialty": []}
    claude_grouped = {"CCAR-F": [], "CCAR-P": [], "CCDV-F": [], "CCAO-F": []}
    aws_counts = defaultdict(int)
    claude_counts = defaultdict(int)
    # A person can hold more than one Credly badge for the SAME credential (e.g. a
    # re-certification issues a new badge id, and Credly sometimes returns one record
    # with no expiry and another with a real one). Collapse those to a single entry per
    # (person, certification_name) so tier totals and the leaderboard don't double-count.
    # Prefer a record with a real expiry over a "no-expiry" placeholder, and the latest
    # expiry among real ones.
    def expiry_rank(entry):
        exp = entry.get("expires_at", "")
        if not exp or exp == "no-expiry":
            return (0, "")          # placeholder — lowest priority
        return (1, exp)             # real expiry — later ISO string sorts higher
    best = {}
    for item in items:
        if not is_active(item):
            continue
        name = item.get("certification_name", "")
        employee = item.get("employee_id", "")
        entry = {"name": name, "employee": employee, "expires_at": item.get("expires_at", ""), "status": item.get("status", "")}
        key = (employee, name)
        if key not in best or expiry_rank(entry) > expiry_rank(best[key]):
            best[key] = entry
    for entry in best.values():
        name = entry["name"]
        if not is_real_cert(name):
            continue
        employee = entry["employee"]
        if "AWS Certified" in name:
            aws_grouped[classify_aws(name)].append(entry)
            aws_counts[employee] += 1
        elif "Claude Certified" in name:
            claude_grouped[classify_claude(name)].append(entry)
            claude_counts[employee] += 1
    result = {"timestamp": datetime.now(timezone.utc).isoformat(), "aws_tiers": {}, "claude_tiers": {}, "leaderboard": {}}
    for t, req in AWS_REQS.items():
        c = aws_grouped.get(t, [])
        result["aws_tiers"][t] = {"current": len(c), "required": req, "percentage": round((len(c)/req)*100, 1) if req else 0, "certifications": c}
    for t, req in CLAUDE_REQS.items():
        c = claude_grouped.get(t, [])
        result["claude_tiers"][t] = {"current": len(c), "required": req if req > 0 else None, "percentage": round((len(c)/req)*100, 1) if req else None, "certifications": c}
    aws_sorted = sorted(aws_counts.items(), key=lambda x: x[1], reverse=True)
    claude_sorted = sorted(claude_counts.items(), key=lambda x: x[1], reverse=True)
    def with_ranks(entries, max_rank=10):
        # Dense ranking: ties share a rank, and the next distinct count is rank+1
        # (not rank + number of people tied), e.g. 1,1,1,1,2,2,3 rather than 1,1,1,1,5,5,7.
        # Cutoff is by number of distinct rank groups (max_rank), not by number of
        # individuals — so a heavily-tied #1 doesn't crowd out the rest of the top 10.
        ranked = []
        rank = 0
        prev_count = None
        for emp, cnt in entries:
            if cnt != prev_count:
                rank += 1
                prev_count = cnt
            if rank > max_rank:
                break
            ranked.append({"employee": emp, "count": cnt, "rank": rank})
        return ranked
    result["leaderboard"]["aws"] = with_ranks(aws_sorted)
    result["leaderboard"]["claude"] = with_ranks(claude_sorted)
    result["apn_network"] = build_apn_network(get_roster(), aws_holder_ids, aws_counts)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "")}, "body": json.dumps(result)}


# ───────────────────────── User management ─────────────────────────

def _caller_email(event):
    """Email of the authenticated caller, from the Cognito authorizer claims."""
    try:
        claims = event["requestContext"]["authorizer"]["claims"]
        return (claims.get("email") or "").strip().lower()
    except (KeyError, TypeError):
        return ""


def _is_admin(event):
    email = _caller_email(event)
    return bool(email) and email in ADMIN_EMAILS


def handle_list_users(event):
    """GET /users — list all tracked Credly users (read-open to any signed-in user)."""
    table = dynamodb.Table(USERS_TABLE)
    resp = table.scan()
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    users = sorted(
        (
            {
                "employee_id": u.get("employee_id", ""),
                "credly_username": u.get("credly_username", ""),
            }
            for u in items
        ),
        key=lambda x: x["employee_id"].lower(),
    )
    # is_admin tells the frontend whether to show edit controls; the real
    # enforcement is on the write endpoints below, not here.
    return _resp(200, {"users": users, "count": len(users), "is_admin": _is_admin(event)})


def handle_upsert_user(event):
    """POST /users — add or edit a user. Admin-only. Keyed on employee_id, so the
    same endpoint creates a new record or updates an existing one."""
    if not _is_admin(event):
        return _resp(403, {"error": "You don't have permission to add or edit users."})
    try:
        data = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "Invalid JSON body."})
    employee_id = (data.get("employee_id") or "").strip()
    if not employee_id:
        return _resp(400, {"error": "employee_id is required (e.g. first.last)."})
    credly_username = (data.get("credly_username") or "").strip()
    # update_item upserts (creates if the key is absent) and preserves any other
    # attributes on the record that this form doesn't manage.
    dynamodb.Table(USERS_TABLE).update_item(
        Key={"employee_id": employee_id},
        UpdateExpression="SET credly_username = :c",
        ExpressionAttributeValues={":c": credly_username},
    )
    logger.info(f"User {employee_id} upserted by {_caller_email(event)}")
    return _resp(200, {"ok": True, "user": {
        "employee_id": employee_id, "credly_username": credly_username,
    }})


def handle_trigger_sync(event):
    """POST /sync — kick off the badge-sync Lambda asynchronously. Admin-only."""
    if not _is_admin(event):
        return _resp(403, {"error": "You don't have permission to trigger a sync."})
    if not BADGE_SYNC_FUNCTION:
        return _resp(500, {"error": "Badge sync function is not configured."})
    lambda_client.invoke(
        FunctionName=BADGE_SYNC_FUNCTION,
        InvocationType="Event",  # fire-and-forget; the full sync takes ~1-2 min
        Payload=b"{}",
    )
    logger.info(f"Badge sync triggered by {_caller_email(event)}")
    return _resp(202, {"ok": True, "message": "Sync started — badges refresh in ~1-2 minutes."})


def lambda_handler(event, context):
    """Router. Uses the API Gateway resource path + method to dispatch. Falls back
    to the compliance payload for GET /compliance (the original behaviour)."""
    method = (event.get("httpMethod") or "").upper()
    resource = event.get("resource") or event.get("path") or ""
    if method == "OPTIONS":
        return _resp(200, {})
    if resource.endswith("/users"):
        if method == "GET":
            return handle_list_users(event)
        if method == "POST":
            return handle_upsert_user(event)
    if resource.endswith("/sync") and method == "POST":
        return handle_trigger_sync(event)
    if resource.endswith("/apn-roster") and method == "POST":
        return handle_roster_upload(event)
    return handle_compliance(event, context)
