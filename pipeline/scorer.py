"""
Hireability scoring logic for AI startup candidates.
Operates on normalized extraction dicts.
"""

import re


# ── Tier lists ──────────────────────────────────────────────────

TIER1_COLLEGES = [
    "indian institute of technology", "iit", "indian institute of science", "iisc",
    "mit", "stanford", "carnegie mellon", "cmu", "uc berkeley",
    "university of california, berkeley", "caltech", "georgia tech",
    "university of texas", "university of washington", "eth zurich",
    "oxford", "cambridge", "harvard", "princeton", "columbia university",
    "university of illinois", "bits pilani", "iiit hyderabad",
]

TIER2_COLLEGES = [
    "nit", "national institute of technology", "dtu", "delhi technological",
    "jadavpur", "vit", "manipal", "thapar", "psg",
    "university of michigan", "university of toronto", "purdue", "ucla",
    "nyu", "university of southern california", "usc",
    "university of maryland", "northeastern",
]

AI_FIELDS = [
    "artificial intelligence", "machine learning", "data science",
    "computer science", "cse", "cs", "ece", "electrical",
    "mathematics", "statistics", "applied math",
]

AI_ROLE_KEYWORDS = [
    "ai ", "ml ", "machine learning", "data scien", "deep learning",
    "nlp", "computer vision", "generative ai", "genai", "llm",
]

SENIOR_TITLES = ["architect", "principal", "director", "vp", "head", "chief", "cto", "founder"]
LEAD_TITLES = ["lead", "senior", "staff", "manager"]

STRONG_SIGNAL_COMPANIES = [
    "google", "deepmind", "openai", "anthropic", "meta", "apple",
    "microsoft", "amazon", "nvidia", "tesla", "samsung research",
    "adobe", "salesforce", "netflix", "uber", "spotify", "bytedance",
]


def _lower(val):
    return (val or "").lower().strip()


def score_college(education):
    best = 0
    reasons = []
    for edu in education:
        college = _lower(edu.get("college"))
        if not college:
            continue
        if any(t in college for t in TIER1_COLLEGES):
            if best < 15:
                best = 15
                reasons = [f"Tier-1 college: {edu['college']}"]
        elif any(t in college for t in TIER2_COLLEGES):
            if best < 9:
                best = 9
                reasons = [f"Tier-2 college: {edu['college']}"]
        else:
            if best < 4:
                best = 4
                reasons = [f"Other college: {edu['college']}"]
    return best, reasons or ["No education data"]


def score_degree(education):
    best = 0
    reasons = []
    for edu in education:
        degree = _lower(edu.get("degree"))
        field = _lower(edu.get("field_of_study"))
        combined = f"{degree} {field}"
        is_pg = any(k in degree for k in ["master", "post grad", "phd", "m.tech", "ms ", "m.s."])
        is_ai_field = any(f in combined for f in AI_FIELDS)
        if is_pg and is_ai_field:
            score, label = 10, "PG in AI/CS-related field"
        elif is_pg:
            score, label = 7, "PG in non-CS field"
        elif is_ai_field:
            score, label = 7, "UG in AI/CS-related field"
        else:
            score, label = 3, "Other degree"
        if score > best:
            best = score
            reasons = [f"{label}: {edu.get('degree', '?')} — {edu.get('field_of_study', '?')}"]
    return best, reasons or ["No degree data"]


def score_years(candidate_info):
    raw = candidate_info.get("years_of_experience") or ""
    match = re.search(r"(\d+\.?\d*)", str(raw))
    if not match:
        return 3, ["Years not stated"]
    years = float(match.group(1))
    if years >= 7:
        return 15, [f"{raw} years — senior, high impact"]
    elif years >= 4:
        return 13, [f"{raw} years — mid-senior, startup sweet spot"]
    elif years >= 2:
        return 8, [f"{raw} years — early career"]
    return 4, [f"{raw} years — very early career"]


def score_role_relevance(experience):
    if not experience:
        return 0, ["No experience data"]
    ai_roles = [e for e in experience if any(k in _lower(e.get("role")) for k in AI_ROLE_KEYWORDS)]
    ratio = len(ai_roles) / len(experience)
    reasons = []
    if ratio >= 0.8:
        score = 25
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS")
    elif ratio >= 0.5:
        score = 18
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS")
    elif ai_roles:
        score = 10
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS")
    else:
        score = 3
        reasons.append("No AI/ML/DS roles found")
    for r in ai_roles[:3]:
        reasons.append(f"  + {r.get('role')} @ {r.get('company')}")
    return score, reasons


def score_seniority(experience):
    if not experience:
        return 0, ["No experience data"]
    has_senior = False
    reasons = []
    has_lead = False
    for exp in experience:
        role = _lower(exp.get("role"))
        if any(t in role for t in SENIOR_TITLES):
            has_senior = True
            reasons.append(f"Senior+: {exp.get('role')} @ {exp.get('company')}")
        elif any(t in role for t in LEAD_TITLES):
            has_lead = True
    grew = False
    if len(experience) >= 2:
        first = _lower(experience[-1].get("role"))
        latest = _lower(experience[0].get("role"))
        grew = (any(t in latest for t in SENIOR_TITLES + LEAD_TITLES)
                and not any(t in first for t in SENIOR_TITLES + LEAD_TITLES))
    if has_senior and grew:
        return 15, reasons + ["Clear career progression"]
    elif has_senior:
        return 13, reasons
    elif has_lead:
        return 9, [f"Lead-level roles found ({len(experience)} total)"]
    elif len(experience) >= 3:
        return 5, [f"{len(experience)} roles, no senior/lead titles"]
    return 3, ["Early career"]


def score_company(experience):
    if not experience:
        return 0, ["No experience data"]
    big_tech = []
    startup_signal = []
    for exp in experience:
        company = _lower(exp.get("company"))
        if any(c in company for c in STRONG_SIGNAL_COMPANIES):
            big_tech.append(exp)
        elif any(sig in company for sig in [".ai", "pvt", "startup", "labs", "ventures"]):
            startup_signal.append(exp)
    reasons = []
    score = 0
    if big_tech:
        score += 12
        for r in big_tech[:2]:
            reasons.append(f"Strong brand: {r.get('company')}")
    if startup_signal:
        score += 8
        for r in startup_signal[:2]:
            reasons.append(f"Startup/small co: {r.get('company')}")
    elif not big_tech:
        score += 4
        reasons.append("Mid-tier companies")
    return min(score, 20), reasons


def score_candidate(data):
    """Score a candidate dict (post-normalization output). Returns dict."""
    info = data.get("candidate_info", {})
    edu = data.get("education", [])
    exp = data.get("experience", [])

    results = [
        ("college_tier",     15, score_college(edu)),
        ("degree_relevance", 10, score_degree(edu)),
        ("years_experience", 15, score_years(info)),
        ("role_relevance",   25, score_role_relevance(exp)),
        ("seniority",        15, score_seniority(exp)),
        ("company_signal",   20, score_company(exp)),
    ]

    breakdown = {}
    total = 0
    for name, max_s, (score, reasons) in results:
        breakdown[name] = {"score": score, "max": max_s, "reasons": reasons}
        total += score

    if total >= 80:
        verdict = "STRONG FIT"
    elif total >= 60:
        verdict = "GOOD FIT"
    elif total >= 40:
        verdict = "MODERATE FIT"
    else:
        verdict = "WEAK FIT"

    return {
        "candidate": info.get("candidate_name", "Unknown"),
        "total_score": total,
        "max_score": 100,
        "verdict": verdict,
        "breakdown": breakdown,
    }
