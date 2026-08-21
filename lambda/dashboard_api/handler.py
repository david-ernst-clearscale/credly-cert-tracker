"""REST API for dashboard — pulls data directly from DynamoDB."""

import os, json, csv, io, base64, logging, urllib.request, urllib.parse, boto3
from collections import defaultdict
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
CERTS_TABLE = os.environ["CERTS_TABLE"]
USERS_TABLE = os.environ["USERS_TABLE"]
ROSTER_BUCKET = os.environ.get("ROSTER_BUCKET", "")
ROSTER_KEY = "apn_roster.json"
MAX_APN_CSV_BYTES = 10 * 1024 * 1024
MAX_APN_CSV_ROWS = 25000
MAX_ADMIN_IDENTIFIER_LENGTH = 128
ADMIN_IDENTIFIER_DELIMITERS = ("/", "?", "#", "\\")

# Comma-separated allowlist of emails permitted to add/edit users or trigger a sync.
# Reads/list are open to any authenticated user; writes require membership here.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}
BADGE_SYNC_FUNCTION = os.environ.get("BADGE_SYNC_FUNCTION", "")
lambda_client = boto3.client("lambda")
secrets = boto3.client("secretsmanager")
SLACK_WEBHOOK_SECRET = os.environ.get("SLACK_WEBHOOK_SECRET", "")
SLACK_BOT_TOKEN_SECRET = os.environ.get("SLACK_BOT_TOKEN_SECRET", "")
# Optional second webhook for the Anthropic channel's leaderboard.
SLACK_ANTHROPIC_WEBHOOK_SECRET = os.environ.get("SLACK_ANTHROPIC_WEBHOOK_SECRET", "")
# We don't store work emails, so derive one from employee_id (first.last) for the Slack
# lookup: first.last@<EMAIL_DOMAIN>. A real stored email (if present) always wins.
EMAIL_DOMAIN = os.environ.get("EMAIL_DOMAIN", "clearscale.com")

# Slack member ID for Salome (the cert-sharing consent contact). Replace with her real
# member ID (in Slack: her profile → ⋮ More → Copy member ID) so the digest @-tags her.
# Until then it falls back to plain text "Salome" (no ping).
SALOME_SLACK_ID = "U065UKPB942"  # salome.chimakadze
SALOME_MENTION = (
    "Salome" if SALOME_SLACK_ID.startswith("REPLACE") else f"<@{SALOME_SLACK_ID}>"
)

# Instructions posted with the weekly Slack digest. Edit this text to change the guidance.
GAP_INSTRUCTIONS = (
    "*How to get counted toward Clearscale's AWS Partner tier:*\n"
    "1. You do *not* need a @clearscale.com email; keep your personal AWS / Builder ID account.\n"
    "2. Go to *AWS Skill Builder → My Profile → AWS Training and Certification badges → Certification "
    "Data* and make sure the checkbox allowing AWS to share your certification data with Clearscale is "
    "selected. Even if your account is already linked, this box can be *unchecked* — that's what makes "
    "you show in APN as a redacted “XXX-XXX-XXX” record instead of under your name.\n"
    f"3. If that checkbox is greyed out or disabled, that's a known AWS-side issue we're working "
    f"through; reach out to {SALOME_MENTION} and she can help.\n"
    "_After you enable it, it can take a few days to show up in APN. You're listed because Credly shows "
    "you hold an active AWS cert and we can't confirm it's shared with Clearscale._"
)

_webhook_cache = {}


def get_webhook(secret_name):
    """Fetch an incoming-webhook URL from Secrets Manager, cached per secret name."""
    if not secret_name:
        return ""
    if secret_name in _webhook_cache:
        return _webhook_cache[secret_name]
    try:
        val = secrets.get_secret_value(SecretId=secret_name)["SecretString"].strip()
    except Exception as e:
        logger.error(f"Could not read webhook secret {secret_name}: {e}")
        val = ""
    _webhook_cache[secret_name] = val
    return val


def post_slack(text, webhook_secret=None):
    """Post to Slack via an incoming webhook (default channel unless webhook_secret given)."""
    url = get_webhook(webhook_secret or SLACK_WEBHOOK_SECRET)
    if not url:
        logger.warning("No Slack webhook configured; skipping post.")
        return False
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)
    return True


_slack_bot_token_cache = None
_slack_id_cache = {}


def get_slack_bot_token():
    """Slack bot token (xoxb-…) from Secrets Manager, for users.lookupByEmail. Cached."""
    global _slack_bot_token_cache
    if _slack_bot_token_cache is not None:
        return _slack_bot_token_cache
    if not SLACK_BOT_TOKEN_SECRET:
        _slack_bot_token_cache = ""
        return ""
    try:
        _slack_bot_token_cache = secrets.get_secret_value(
            SecretId=SLACK_BOT_TOKEN_SECRET
        )["SecretString"].strip()
    except Exception as e:
        logger.error(f"Could not read Slack bot token secret: {e}")
        _slack_bot_token_cache = ""
    return _slack_bot_token_cache


def slack_lookup_user_id(email):
    """Resolve a work email to a Slack member ID via users.lookupByEmail.

    Returns the member ID (e.g. 'U0123ABCD') or None if there's no token, no match,
    or the API errors — callers fall back to plain-text names.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    if email in _slack_id_cache:
        return _slack_id_cache[email]
    token = get_slack_bot_token()
    if not token:
        return None
    uid = None
    try:
        url = "https://slack.com/api/users.lookupByEmail?email=" + urllib.parse.quote(
            email
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if data.get("ok"):
            uid = data.get("user", {}).get("id")
        else:
            logger.info(f"Slack lookup for {email}: {data.get('error')}")
    except Exception as e:
        logger.warning(f"Slack lookup failed for {email}: {e}")
    _slack_id_cache[email] = uid
    return uid


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
    for row_number, row in enumerate(reader, start=1):
        if row_number > MAX_APN_CSV_ROWS:
            raise ValueError(
                f"APN CSV has more than {MAX_APN_CSV_ROWS:,} rows. Please upload a smaller export."
            )
        name = (row.get("User name") or "").strip()
        email = (row.get("User work email") or "").strip()
        cert = {
            "name": (row.get("Certification name") or "").strip(),
            "level": (row.get("Certification level") or "").strip(),
            "award_date": (row.get("Award date") or "").split(" ")[0],
            "expiration_date": (row.get("Expiration date") or "").split(" ")[0],
        }
        if (
            not email
            or email.upper().startswith("XXX")
            or name.upper().startswith("XXX")
        ):
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
    if item.get("status") == "bad_expiry":
        return False

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
        return False


def is_bad_expiry(item):
    if item.get("status") == "bad_expiry":
        return True

    expires = item.get("expires_at", "")
    if not expires or expires == "no-expiry":
        return False
    try:
        datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return False
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
        display = (u.get("name") or "").strip() or " ".join(
            w.capitalize() for w in employee_id.replace(".", " ").split()
        )
        accounts.append(
            {
                "keys": keys,
                "credly_username": u.get("credly_username", ""),
                "name": display,
                "email": u.get("email", ""),
                "employee_id": employee_id,
            }
        )
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
        person_keys = _identity_keys(
            name=person.get("name", ""), email=person.get("email", "")
        )
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
        {
            "name": a["name"],
            "credly_username": a["credly_username"],
            "aws_cert_count": aws_counts.get(a["employee_id"], 0),
        }
        for a in accounts
        if a["employee_id"] in aws_holder_ids and not (a["keys"] & all_roster_keys)
    ]
    # Most AWS certs first (prioritize who to get onto the APN list), then alphabetical.
    credly_only.sort(key=lambda e: (-e.get("aws_cert_count", 0), e["name"].lower()))
    # Subtotal the redacted ("Not Trackable") records by certification so we can see
    # what APN reports in that bucket even though the owners are masked.
    redacted_certs = roster.get("redacted_certs", [])
    tier_map = {t: defaultdict(int) for t in AWS_REQS}
    for r in redacted_certs:
        t = classify_aws(r.get("name", ""))
        if t in tier_map:
            tier_map[t][r.get("name", "")] += 1
    # Nested breakdown: each tier group, with its individual certifications under it.
    redacted_breakdown = []
    for t in AWS_REQS:  # Foundational, Technical, Professional/Specialty
        certs = [
            {"name": name, "count": cnt}
            for name, cnt in sorted(tier_map[t].items(), key=lambda x: (-x[1], x[0]))
        ]
        redacted_breakdown.append(
            {"tier": t, "count": sum(c["count"] for c in certs), "certs": certs}
        )
    # APN-side tier totals as distinct NAMED individuals (same tier logic as the Credly
    # side), for the Credly-vs-APN comparison. Redacted records can't be attributed to a
    # person, so they're excluded here (a lower bound on AWS's true count).
    apn_found, apn_tech, apn_ps = set(), set(), set()
    for person in roster.get("people", []):
        pid = _norm(person.get("name", ""))
        for cert in person.get("certs", []):
            band = classify_aws(cert.get("name", ""))
            if band == "Foundational":
                apn_found.add(pid)
            else:
                apn_tech.add(pid)
                if band == "Professional/Specialty":
                    apn_ps.add(pid)
    apn_tiers = {
        "Foundational": len(apn_found),
        "Technical": len(apn_tech),
        "Professional/Specialty": len(apn_ps),
    }
    return {
        "matched": matched,
        "missing": missing,
        "credly_only": credly_only,
        "redacted_count": roster.get("redacted_count", len(redacted_certs)),
        "redacted_breakdown": redacted_breakdown,
        "redacted_certs": redacted_certs,
        "apn_tiers": apn_tiers,
        "total_named": len(matched) + len(missing),
        "uploaded_at": roster.get("uploaded_at", ""),
    }


def _headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", ""),
    }


def _resp(status, body):
    return {"statusCode": status, "headers": _headers(), "body": json.dumps(body)}


def handle_roster_upload(event):
    """POST /apn-roster — parse an uploaded CSV, store it in S3, return a summary."""
    if not _is_admin(event):
        return _resp(403, {"error": "You don't have permission to upload APN rosters."})
    if not ROSTER_BUCKET:
        return _resp(
            500, {"error": "Roster storage is not configured (ROSTER_BUCKET unset)."}
        )
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_bytes = base64.b64decode(body)
        if len(body_bytes) > MAX_APN_CSV_BYTES:
            return _resp(
                413,
                {
                    "error": "APN CSV upload is too large. Please upload a CSV no larger than 10 MB."
                },
            )
        body = body_bytes.decode("utf-8", errors="replace")
    elif len(body.encode("utf-8")) > MAX_APN_CSV_BYTES:
        return _resp(
            413,
            {
                "error": "APN CSV upload is too large. Please upload a CSV no larger than 10 MB."
            },
        )
    if not body.strip():
        return _resp(400, {"error": "Empty upload — no CSV content received."})
    try:
        roster = parse_apn_csv(body)
    except ValueError as e:
        return _resp(400, {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to parse APN CSV: {e}")
        return _resp(
            400,
            {
                "error": "Could not parse the CSV. Check that it's the APN certification export."
            },
        )
    s3.put_object(
        Bucket=ROSTER_BUCKET,
        Key=ROSTER_KEY,
        Body=json.dumps(roster).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(
        f"Stored APN roster: {len(roster['people'])} people, {roster['redacted_count']} redacted"
    )
    return _resp(
        200,
        {
            "ok": True,
            "named_people": len(roster["people"]),
            "redacted_count": roster["redacted_count"],
            "uploaded_at": roster["uploaded_at"],
        },
    )


def handle_compliance(event, context=None):
    table = dynamodb.Table(CERTS_TABLE)
    items = table.scan().get("Items", [])
    # employee_ids that hold at least one *active* AWS cert on Credly (used to scope
    # the "On Credly, Not on APN" list). Active-only on purpose: someone whose AWS
    # certs are all expired has nothing to share toward the partner tier, so they
    # shouldn't be flagged or notified. This also drops rows mis-attributed via a
    # wrong/shared credly_username, which were showing up with no active-cert count.
    aws_holder_ids = {
        it.get("employee_id", "")
        for it in items
        if "AWS Certified" in it.get("certification_name", "")
        and is_real_cert(it.get("certification_name", ""))
        and is_active(it)
    }
    aws_grouped = {"Foundational": [], "Technical": [], "Professional/Specialty": []}
    claude_grouped = {"CCAR-F": [], "CCAR-P": [], "CCDV-F": [], "CCAO-F": []}
    aws_counts = defaultdict(int)
    claude_counts = defaultdict(int)
    # AWS partner tiers are measured in DISTINCT CERTIFIED INDIVIDUALS, not certs — a
    # person holding 3 Technical certs counts once. "Technical" = any non-Foundational
    # cert (Associate/Professional/Specialty); Professional/Specialty is a SUBSET of it.
    aws_individuals = {
        "Foundational": set(),
        "Technical": set(),
        "Professional/Specialty": set(),
    }
    claude_individuals = {
        "CCAR-F": set(),
        "CCAR-P": set(),
        "CCDV-F": set(),
        "CCAO-F": set(),
    }

    # A person can hold more than one Credly badge for the SAME credential (e.g. a
    # re-certification issues a new badge id, and Credly sometimes returns one record
    # with no expiry and another with a real one). Collapse those to a single entry per
    # (person, certification_name) so tier totals and the leaderboard don't double-count.
    # Prefer a record with a real expiry over a "no-expiry" placeholder, and the latest
    # expiry among real ones.
    def expiry_rank(entry):
        exp = entry.get("expires_at", "")
        if not exp or exp == "no-expiry":
            return (0, "")  # placeholder — lowest priority
        return (1, exp)  # real expiry — later ISO string sorts higher

    best = {}
    expiry_warnings = []
    for item in items:
        name = item.get("certification_name", "")
        employee = item.get("employee_id", "")
        if is_bad_expiry(item):
            expiry_warnings.append(
                {
                    "name": name,
                    "employee": employee,
                    "expires_at": item.get("expires_at", ""),
                    "status": "bad_expiry",
                }
            )
        if not is_active(item):
            continue
        entry = {
            "name": name,
            "employee": employee,
            "expires_at": item.get("expires_at", ""),
            "status": item.get("status", ""),
        }
        key = (employee, name)
        if key not in best or expiry_rank(entry) > expiry_rank(best[key]):
            best[key] = entry
    for entry in best.values():
        name = entry["name"]
        if not is_real_cert(name):
            continue
        employee = entry["employee"]
        if "AWS Certified" in name:
            band = classify_aws(name)
            aws_counts[employee] += 1
            if band == "Foundational":
                aws_grouped["Foundational"].append(entry)
                aws_individuals["Foundational"].add(employee)
            else:
                # Associate, Professional, and Specialty all count as "Technical".
                aws_grouped["Technical"].append(entry)
                aws_individuals["Technical"].add(employee)
                if band == "Professional/Specialty":
                    aws_grouped["Professional/Specialty"].append(entry)
                    aws_individuals["Professional/Specialty"].add(employee)
        elif "Claude Certified" in name:
            cband = classify_claude(name)
            claude_grouped[cband].append(entry)
            claude_counts[employee] += 1
            claude_individuals[cband].add(employee)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "aws_tiers": {},
        "claude_tiers": {},
        "leaderboard": {},
        "expiry_warnings": sorted(
            expiry_warnings, key=lambda e: (e["employee"], e["name"], e["expires_at"])
        ),
    }
    for t, req in AWS_REQS.items():
        c = aws_grouped.get(t, [])
        n = len(aws_individuals[t])  # distinct certified individuals, not cert count
        # One row per distinct person in this tier (each counted once), with how many
        # certs they hold in it — for the "individuals" compliance view on the APN tab.
        per_person = defaultdict(int)
        for e in c:
            per_person[e["employee"]] += 1
        individuals = sorted(
            ({"employee": emp, "count": cnt} for emp, cnt in per_person.items()),
            key=lambda x: (-x["count"], x["employee"]),
        )
        result["aws_tiers"][t] = {
            "current": n,
            "required": req,
            "percentage": round((n / req) * 100, 1) if req else 0,
            "certifications": c,
            "cert_count": len(c),
            "individuals": individuals,
        }
    for t, req in CLAUDE_REQS.items():
        c = claude_grouped.get(t, [])
        n = len(claude_individuals[t])
        result["claude_tiers"][t] = {
            "current": n,
            "required": req if req > 0 else None,
            "percentage": round((n / req) * 100, 1) if req else None,
            "certifications": c,
            "cert_count": len(c),
        }
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
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", ""),
        },
        "body": json.dumps(result),
    }


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


def _validate_admin_identifier(value, field_name, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        return "", f"{field_name} must be a string."

    normalized = value.strip()
    if required and not normalized:
        return "", f"{field_name} is required (e.g. first.last)."
    if not normalized:
        return "", None
    if len(normalized) > MAX_ADMIN_IDENTIFIER_LENGTH:
        return (
            "",
            f"{field_name} must be {MAX_ADMIN_IDENTIFIER_LENGTH} characters or fewer.",
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        return "", f"{field_name} cannot contain control characters."
    if any(delimiter in normalized for delimiter in ADMIN_IDENTIFIER_DELIMITERS):
        return "", f"{field_name} cannot contain path, query, or fragment delimiters."
    return normalized, None


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
    return _resp(
        200, {"users": users, "count": len(users), "is_admin": _is_admin(event)}
    )


def handle_upsert_user(event):
    """POST /users — add or edit a user. Admin-only. Keyed on employee_id, so the
    same endpoint creates a new record or updates an existing one."""
    if not _is_admin(event):
        return _resp(403, {"error": "You don't have permission to add or edit users."})
    try:
        data = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "Invalid JSON body."})
    employee_id, error = _validate_admin_identifier(
        data.get("employee_id"), "employee_id", required=True
    )
    if error:
        return _resp(400, {"error": error})
    credly_username, error = _validate_admin_identifier(
        data.get("credly_username"), "credly_username"
    )
    if error:
        return _resp(400, {"error": error})
    # update_item upserts (creates if the key is absent) and preserves any other
    # attributes on the record that this form doesn't manage.
    dynamodb.Table(USERS_TABLE).update_item(
        Key={"employee_id": employee_id},
        UpdateExpression="SET credly_username = :c",
        ExpressionAttributeValues={":c": credly_username},
    )
    logger.info(f"User {employee_id} upserted by {_caller_email(event)}")
    return _resp(
        200,
        {
            "ok": True,
            "user": {
                "employee_id": employee_id,
                "credly_username": credly_username,
            },
        },
    )


def handle_delete_user(event):
    """DELETE /users — remove a user and all their cert records. Admin-only."""
    if not _is_admin(event):
        return _resp(403, {"error": "You don't have permission to delete users."})
    try:
        data = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "Invalid JSON body."})
    employee_id = (data.get("employee_id") or "").strip()
    if not employee_id:
        return _resp(400, {"error": "employee_id is required."})
    # Remove the user's cert records first (keyed on the same employee_id) so they
    # don't linger in the tiers/leaderboard, then remove the user record itself.
    certs = dynamodb.Table(CERTS_TABLE)
    resp = certs.query(KeyConditionExpression=Key("employee_id").eq(employee_id))
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = certs.query(
            KeyConditionExpression=Key("employee_id").eq(employee_id),
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    with certs.batch_writer() as bw:
        for it in items:
            bw.delete_item(
                Key={
                    "employee_id": employee_id,
                    "certification_id": it["certification_id"],
                }
            )
    dynamodb.Table(USERS_TABLE).delete_item(Key={"employee_id": employee_id})
    logger.info(
        f"User {employee_id} deleted ({len(items)} certs) by {_caller_email(event)}"
    )
    return _resp(200, {"ok": True, "deleted": employee_id, "certs_removed": len(items)})


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
    return _resp(
        202, {"ok": True, "message": "Sync started — badges refresh in ~1-2 minutes."}
    )


def _email_for(acct):
    """Best-effort work email for a Credly account: a stored email if present, else
    derived as employee_id@EMAIL_DOMAIN (employee_id is first.last)."""
    if not acct:
        return ""
    email = (acct.get("email") or "").strip()
    if email:
        return email
    eid = (acct.get("employee_id") or "").strip()
    return f"{eid}@{EMAIL_DOMAIN}" if eid else ""


def post_apn_gap_digest(dry_run=False):
    """Weekly digest: the 'AWS cert on Credly but not on APN' list, @-tagging each person.

    Reuses handle_compliance so the list matches the dashboard exactly, and resolves each
    person's Slack member ID via users.lookupByEmail (email derived from employee_id).
    Skips entirely when nobody is in the gap. dry_run=True composes the message (tags and
    all) and RETURNS it without posting — preview without pinging anyone.
    """
    data = json.loads(handle_compliance({})["body"])
    gap = data.get("apn_network", {}).get("credly_only", [])
    if not gap:
        logger.info("APN gap digest: nobody in the gap — skipping Slack post.")
        return _resp(200, {"posted": False, "count": 0, "message": ""})
    # credly_username -> account (has employee_id/email). Server-side only; email is never
    # sent to the browser. Used to derive the lookup email for Slack tagging.
    acct_by_user = {
        a["credly_username"]: a
        for a in get_credly_accounts()
        if a.get("credly_username")
    }
    lines, tagged, unresolved = [], 0, []
    for p in gap:
        acct = acct_by_user.get(p.get("credly_username", ""))
        uid = slack_lookup_user_id(_email_for(acct))
        if uid:
            name_part = f"<@{uid}>"
            tagged += 1
        else:
            # Fall back to plain name (+ Credly handle to help identify who we couldn't tag).
            name_part = f"*{p['name']}*"
            if p.get("credly_username"):
                name_part += f" (Credly: {p['credly_username']})"
            unresolved.append(p.get("name", ""))
        line = f"• {name_part}"
        if p.get("aws_cert_count"):
            line += f" — {p['aws_cert_count']} AWS cert(s)"
        lines.append(line)
    header = (
        f":warning: *{len(gap)} teammate(s)* hold an active AWS certification on Credly that we "
        "*can't confirm* is being credited to Clearscale's AWS Partner tier. AWS's export only "
        "lists people who've enabled cert sharing; everyone else appears as a redacted "
        "“XXXXXXX” record we can't match, or doesn't appear at all, so from our side we can't "
        "tell whether you're already counted:\n"
    )
    message = header + "\n" + "\n".join(lines) + "\n\n" + GAP_INSTRUCTIONS
    if dry_run:
        logger.info(
            f"APN gap digest DRY RUN: {len(gap)} people, {tagged} tagged, {len(unresolved)} unresolved (not posted)"
        )
        return _resp(
            200,
            {
                "posted": False,
                "dry_run": True,
                "count": len(gap),
                "tagged": tagged,
                "unresolved": unresolved,
                "message": message,
            },
        )
    posted = post_slack(message)
    logger.info(f"APN gap digest: {len(gap)} people, {tagged} tagged, posted={posted}")
    return _resp(
        200,
        {
            "posted": posted,
            "count": len(gap),
            "tagged": tagged,
            "unresolved": unresolved,
        },
    )


def _title(eid):
    """employee_id (first.last) -> 'First Last' for display."""
    return " ".join(
        w.capitalize() for w in (eid or "").replace(".", " ").replace("-", " ").split()
    ) or (eid or "")


# kind -> (leaderboard key, title, unit label)
LEADERBOARD_META = {
    "aws": ("aws", "AWS Certification Leaderboard", "AWS certs"),
    "claude": ("claude", "Anthropic Certification Leaderboard", "Anthropic certs"),
}


def post_leaderboard(kind="aws", dry_run=False, webhook_secret=None):
    """Monthly certification leaderboard — a monospace bar chart, NO @-pings.

    Uses the same dense-ranked leaderboard the dashboard shows (kind = 'aws' or 'claude').
    Large rank groups (the long tail on 1 cert) collapse to one line. dry_run returns the
    message without posting; webhook_secret picks the destination channel.
    """
    key, title, unit = LEADERBOARD_META.get(kind, LEADERBOARD_META["aws"])
    data = json.loads(handle_compliance({})["body"])
    lb = data.get("leaderboard", {}).get(key, [])
    if not lb:
        logger.info(f"Leaderboard[{kind}]: nobody certified — skipping.")
        return _resp(200, {"posted": False, "count": 0, "message": ""})
    MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
    COLLAPSE_OVER = 5  # groups bigger than this collapse to one summary line
    BAR_CAP = 12
    # Group consecutive entries by rank (already sorted by rank ascending).
    groups = []
    for e in lb:
        if groups and groups[-1][0] == e["rank"]:
            groups[-1][1].append(e)
        else:
            groups.append((e["rank"], [e]))

    # Collapse only the deep tail (rank 4+); medal ranks (1-3) always show every name.
    def collapsed(rank, members):
        return rank > 3 and len(members) > COLLAPSE_OVER

    indiv = [
        _title(m["employee"])
        for r, members in groups
        if not collapsed(r, members)
        for m in members
    ]
    w = min(max((len(n) for n in indiv), default=10), 22)
    lines = []
    for rank, members in groups:
        if collapsed(rank, members):
            c = members[0]["count"]
            lines.append(
                f"{rank:>2}  …and {len(members)} teammates with {c} cert{'s' if c != 1 else ''} each"
            )
        else:
            for m in members:
                c = m["count"]
                bar = ("█" * min(c, BAR_CAP)).ljust(BAR_CAP)
                medal = f"  {MEDALS[rank]}" if rank in MEDALS else ""
                lines.append(
                    f"{rank:>2}  {_title(m['employee']).ljust(w)}  {bar}  {c}{medal}"
                )
    total_certs = sum(e["count"] for e in lb)
    people = len(lb)
    rule = "─" * 50
    body = (
        f"🏆 {title} — Team Clearscale\n"
        f"{rule}\n" + "\n".join(lines) + f"\n{rule}\n"
        f"{total_certs} active {unit} · {people} certified teammates 🎖"
    )
    message = "```\n" + body + "\n```"
    if dry_run:
        logger.info(f"Leaderboard[{kind}] DRY RUN: {people} people (not posted)")
        return _resp(
            200,
            {"posted": False, "dry_run": True, "people": people, "message": message},
        )
    posted = post_slack(message, webhook_secret=webhook_secret)
    logger.info(f"Leaderboard[{kind}]: {people} people, posted={posted}")
    return _resp(200, {"posted": posted, "people": people})


def lambda_handler(event, context):
    """Router. Uses the API Gateway resource path + method to dispatch. Falls back
    to the compliance payload for GET /compliance (the original behaviour)."""
    # Scheduled EventBridge trigger (no HTTP method) → weekly Slack digest.
    # Pass {"task":"apn_slack_digest","dry_run":true} to preview without posting/pinging.
    if event.get("task") == "apn_slack_digest":
        return post_apn_gap_digest(dry_run=bool(event.get("dry_run")))
    # {"task":"aws_leaderboard","dry_run":true} to preview the monthly leaderboard.
    if event.get("task") == "aws_leaderboard":
        return post_leaderboard("aws", dry_run=bool(event.get("dry_run")))
    # {"task":"anthropic_leaderboard","dry_run":true} — posts to the Anthropic channel.
    if event.get("task") == "anthropic_leaderboard":
        return post_leaderboard(
            "claude",
            dry_run=bool(event.get("dry_run")),
            webhook_secret=SLACK_ANTHROPIC_WEBHOOK_SECRET,
        )
    method = (event.get("httpMethod") or "").upper()
    resource = event.get("resource") or event.get("path") or ""
    if method == "OPTIONS":
        return _resp(200, {})
    if resource.endswith("/users"):
        if method == "GET":
            return handle_list_users(event)
        if method == "POST":
            return handle_upsert_user(event)
        if method == "DELETE":
            return handle_delete_user(event)
    if resource.endswith("/sync") and method == "POST":
        return handle_trigger_sync(event)
    if resource.endswith("/apn-roster") and method == "POST":
        return handle_roster_upload(event)
    return handle_compliance(event, context)
