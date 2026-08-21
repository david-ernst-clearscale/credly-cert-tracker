AWS_REQS = {"Foundational": 10, "Technical": 25, "Professional/Specialty": 10}
CLAUDE_REQS = {"CCAR-F": 10, "CCAR-P": 0, "CCDV-F": 0, "CCAO-F": 0}

FOUNDATIONAL = ("Cloud Practitioner", "AI Practitioner")
PROFESSIONAL = ("Professional", "Specialty")
NON_CERT_MARKERS = ("Early Adopter",)


def is_real_cert(name):
    return not any(marker in name for marker in NON_CERT_MARKERS)


def is_certification_badge(badge):
    name = badge.get("badge_template", {}).get("name", "")
    return classify_certification(name) is not None


def classify_certification(name):
    if not is_real_cert(name):
        return None
    if "AWS Certified" in name:
        return classify_aws(name)
    if "Claude Certified" in name:
        return classify_claude(name)
    return None


def classify_aws(name):
    for keyword in FOUNDATIONAL:
        if keyword in name:
            return "Foundational"
    for keyword in PROFESSIONAL:
        if keyword in name:
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
