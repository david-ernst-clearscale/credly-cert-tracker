"""REST API for dashboard — pulls data directly from DynamoDB."""
import os, json, boto3
from collections import defaultdict
from datetime import datetime, timezone

dynamodb = boto3.resource("dynamodb")
CERTS_TABLE = os.environ["CERTS_TABLE"]
USERS_TABLE = os.environ["USERS_TABLE"]

AWS_REQS = {"Foundational": 10, "Technical": 25, "Professional/Specialty": 10}
CLAUDE_REQS = {"CCAR-F": 10, "CCAR-P": 0, "CCDV-F": 0, "CCAO-F": 0}
FOUNDATIONAL = ["Cloud Practitioner", "AI Practitioner"]
PROFESSIONAL = ["Professional", "Specialty"]

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

def lambda_handler(event, context):
    table = dynamodb.Table(CERTS_TABLE)
    items = table.scan().get("Items", [])
    aws_grouped = {"Foundational": [], "Technical": [], "Professional/Specialty": []}
    claude_grouped = {"CCAR-F": [], "CCAR-P": [], "CCDV-F": [], "CCAO-F": []}
    aws_counts = defaultdict(int)
    claude_counts = defaultdict(int)
    for item in items:
        if not is_active(item):
            continue
        name = item.get("certification_name", "")
        employee = item.get("employee_id", "")
        entry = {"name": name, "employee": employee, "expires_at": item.get("expires_at", ""), "status": item.get("status", "")}
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
    aws_top = sorted(aws_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    claude_top = sorted(claude_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    def with_ranks(entries):
        # Dense ranking: ties share a rank, and the next distinct count is rank+1
        # (not rank + number of people tied), e.g. 1,1,1,1,2,2,3 rather than 1,1,1,1,5,5,7.
        ranked = []
        rank = 0
        prev_count = None
        for emp, cnt in entries:
            if cnt != prev_count:
                rank += 1
                prev_count = cnt
            ranked.append({"employee": emp, "count": cnt, "rank": rank})
        return ranked
    result["leaderboard"]["aws"] = with_ranks(aws_top)
    result["leaderboard"]["claude"] = with_ranks(claude_top)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}, "body": json.dumps(result)}
