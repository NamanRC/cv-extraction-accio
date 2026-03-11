"""
Hireability scorer for AI startup candidates.

Uses a simple LLM call to extract structured CV data, then scores candidates on:
  - College tier (0-15)
  - Degree relevance (0-10)
  - Years of experience (0-15)
  - Role relevance to AI/ML (0-25)
  - Role seniority & progression (0-15)
  - Company signal (0-20)

Usage:
    python3 score.py "CV PDFs/Ashutosh Tripathi.pdf"
    python3 score.py --all                              # score all PDFs in data dir
"""

import json
import os
import re
import sys
from pathlib import Path

import yaml
from google import genai

CONFIG_PATH = Path(__file__).parent / "config.yaml"
LLM_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.0-flash")


def _load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def _extract_with_llm(pdf_path: str, cfg: dict) -> dict:
    """Run a single LLM call to extract structured data from a PDF."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set. Export it before running.")
    os.environ["GOOGLE_API_KEY"] = api_key

    system_prompt = cfg.get("system_prompt", "")
    schema = cfg.get("schema", {})

    client = genai.Client()
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    response = client.models.generate_content(
        model=LLM_MODEL,
        contents=[
            genai.types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            f"""Extract structured data from this CV/resume.

{system_prompt}

Return ONLY valid JSON matching this schema:
{json.dumps(schema, indent=2)}

Return the JSON object directly, no markdown fences."""
        ],
    )

    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise

# ── Tier lists ──────────────────────────────────────────────────

TIER1_COLLEGES = [
    "indian institute of technology",
    "iit",
    "indian institute of science",
    "iisc",
    "mit",
    "stanford",
    "carnegie mellon",
    "cmu",
    "uc berkeley",
    "university of california, berkeley",
    "caltech",
    "georgia tech",
    "university of texas",
    "university of washington",
    "eth zurich",
    "oxford",
    "cambridge",
    "harvard",
    "princeton",
    "columbia university",
    "university of illinois",
    "bits pilani",
    "iiit hyderabad",
]

TIER2_COLLEGES = [
    "nit",
    "national institute of technology",
    "dtu",
    "delhi technological",
    "jadavpur",
    "vit",
    "manipal",
    "thapar",
    "psg",
    "university of michigan",
    "university of toronto",
    "purdue",
    "ucla",
    "nyu",
    "university of southern california",
    "usc",
    "university of maryland",
    "northeastern",
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
    "adobe", "salesforce", "netflix", "uber", "spotify",
    "bytedance", "tiktok",
]

STARTUP_SIGNAL_COMPANIES = [
    "ycombinator", "y combinator",
    # small/unknown companies are treated as potential startups heuristically
]


# ── Scoring functions ───────────────────────────────────────────

def _lower(val):
    return (val or "").lower().strip()


def score_college(education: list[dict]) -> tuple[int, list[str]]:
    """0-15 points. Best college wins."""
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
    if not reasons:
        reasons = ["No education data"]
    return best, reasons


def score_degree(education: list[dict]) -> tuple[int, list[str]]:
    """0-10 points. PG in AI/CS > BTech CS > other."""
    best = 0
    reasons = []
    for edu in education:
        degree = _lower(edu.get("degree"))
        field = _lower(edu.get("field_of_study"))
        combined = f"{degree} {field}"

        is_pg = any(k in degree for k in ["master", "post grad", "phd", "m.tech", "ms ", "m.s."])
        is_ai_field = any(f in combined for f in AI_FIELDS)

        if is_pg and is_ai_field:
            score = 10
            label = "PG in AI/CS-related field"
        elif is_pg:
            score = 7
            label = "PG in non-CS field"
        elif is_ai_field:
            score = 7
            label = "UG in AI/CS-related field"
        else:
            score = 3
            label = "Other degree"

        if score > best:
            best = score
            reasons = [f"{label}: {edu.get('degree', '?')} — {edu.get('field_of_study', '?')}"]
    if not reasons:
        reasons = ["No degree data"]
    return best, reasons


def score_years(candidate_info: dict) -> tuple[int, list[str]]:
    """0-15 points. Sweet spot for startup is 4-10 years."""
    raw = candidate_info.get("years_of_experience") or ""
    match = re.search(r"(\d+\.?\d*)", str(raw))
    if not match:
        return 3, ["Years not stated — assuming entry-level"]

    years = float(match.group(1))
    if years >= 7:
        return 15, [f"{raw} years — senior, high impact"]
    elif years >= 4:
        return 13, [f"{raw} years — mid-senior, startup sweet spot"]
    elif years >= 2:
        return 8, [f"{raw} years — early career, can grow"]
    else:
        return 4, [f"{raw} years — very early career"]


def score_role_relevance(experience: list[dict]) -> tuple[int, list[str]]:
    """0-25 points. How many roles are directly AI/ML/DS relevant."""
    if not experience:
        return 0, ["No experience data"]

    ai_roles = []
    other_roles = []
    for exp in experience:
        role = _lower(exp.get("role"))
        if any(k in role for k in AI_ROLE_KEYWORDS):
            ai_roles.append(exp)
        else:
            other_roles.append(exp)

    ai_ratio = len(ai_roles) / len(experience)
    reasons = []

    if ai_ratio >= 0.8:
        score = 25
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS — deeply specialized")
    elif ai_ratio >= 0.5:
        score = 18
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS — strong alignment")
    elif ai_roles:
        score = 10
        reasons.append(f"{len(ai_roles)}/{len(experience)} roles are AI/ML/DS — some alignment")
    else:
        score = 3
        reasons.append("No AI/ML/DS roles found")

    for r in ai_roles[:3]:
        reasons.append(f"  + {r.get('role')} @ {r.get('company')}")

    return score, reasons


def score_seniority(experience: list[dict]) -> tuple[int, list[str]]:
    """0-15 points. Progression from junior to senior/lead/architect."""
    if not experience:
        return 0, ["No experience data"]

    has_senior = False
    has_lead = False
    reasons = []

    for exp in experience:
        role = _lower(exp.get("role"))
        if any(t in role for t in SENIOR_TITLES):
            has_senior = True
            reasons.append(f"Senior+: {exp.get('role')} @ {exp.get('company')}")
        elif any(t in role for t in LEAD_TITLES):
            has_lead = True

    # Check progression: latest role (index 0) vs earliest role (last index)
    if len(experience) >= 2:
        first_role = _lower(experience[-1].get("role"))
        latest_role = _lower(experience[0].get("role"))
        grew = (
            any(t in latest_role for t in SENIOR_TITLES + LEAD_TITLES)
            and not any(t in first_role for t in SENIOR_TITLES + LEAD_TITLES)
        )
    else:
        grew = False

    if has_senior and grew:
        return 15, reasons + ["Clear career progression to senior/architect level"]
    elif has_senior:
        return 13, reasons
    elif has_lead:
        return 9, [f"Lead-level roles found ({len(experience)} total roles)"]
    elif len(experience) >= 3:
        return 5, [f"{len(experience)} roles but no senior/lead titles yet"]
    else:
        return 3, ["Early career, limited role history"]


def score_company(experience: list[dict]) -> tuple[int, list[str]]:
    """0-20 points. Brand-name tech + startup experience."""
    if not experience:
        return 0, ["No experience data"]

    big_tech = []
    startup_signal = []

    for exp in experience:
        company = _lower(exp.get("company"))
        if any(c in company for c in STRONG_SIGNAL_COMPANIES):
            big_tech.append(exp)
        elif any(c in company for c in STARTUP_SIGNAL_COMPANIES):
            startup_signal.append(exp)

    # Heuristic: if company name is short or contains "pvt" / "ltd" / ".ai"
    # and not in big tech list, treat as smaller/startup company
    for exp in experience:
        company = _lower(exp.get("company"))
        if exp in big_tech:
            continue
        if any(sig in company for sig in [".ai", "pvt", "startup", "labs", "ventures"]):
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
        reasons.append("Mid-tier companies — no strong brand or startup signal")

    return min(score, 20), reasons


# ── Main scorer ─────────────────────────────────────────────────

def score_candidate(data: dict) -> dict:
    """Score a single candidate's normalized extraction data. Returns breakdown."""
    candidate_info = data.get("candidate_info", {})
    education = data.get("education", [])
    experience = data.get("experience", [])

    college_score, college_reasons = score_college(education)
    degree_score, degree_reasons = score_degree(education)
    years_score, years_reasons = score_years(candidate_info)
    relevance_score, relevance_reasons = score_role_relevance(experience)
    seniority_score, seniority_reasons = score_seniority(experience)
    company_score, company_reasons = score_company(experience)

    total = college_score + degree_score + years_score + relevance_score + seniority_score + company_score

    return {
        "candidate": candidate_info.get("candidate_name", "Unknown"),
        "total_score": total,
        "max_score": 100,
        "breakdown": {
            "college_tier":      {"score": college_score,    "max": 15, "reasons": college_reasons},
            "degree_relevance":  {"score": degree_score,     "max": 10, "reasons": degree_reasons},
            "years_experience":  {"score": years_score,      "max": 15, "reasons": years_reasons},
            "role_relevance":    {"score": relevance_score,  "max": 25, "reasons": relevance_reasons},
            "seniority":         {"score": seniority_score,  "max": 15, "reasons": seniority_reasons},
            "company_signal":    {"score": company_score,    "max": 20, "reasons": company_reasons},
        },
    }


def extract_and_score(pdf_path: str) -> dict:
    """Extract from PDF using a simple LLM call, then score."""
    cfg = _load_config()
    data = _extract_with_llm(pdf_path, cfg)
    scoring = score_candidate(data)
    scoring["llm"] = LLM_MODEL
    return scoring


def print_report(scoring: dict):
    """Print a human-readable scoring report."""
    name = scoring["candidate"]
    total = scoring["total_score"]
    max_s = scoring["max_score"]

    if total >= 80:
        verdict = "STRONG FIT"
    elif total >= 60:
        verdict = "GOOD FIT"
    elif total >= 40:
        verdict = "MODERATE FIT"
    else:
        verdict = "WEAK FIT"

    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"  Hireability Score: {total}/{max_s}  —  {verdict}")
    print(f"{'=' * 60}")

    for category, info in scoring["breakdown"].items():
        label = category.replace("_", " ").title()
        bar_len = int(info["score"] / info["max"] * 20) if info["max"] else 0
        bar = "#" * bar_len + "." * (20 - bar_len)
        print(f"\n  {label} [{bar}] {info['score']}/{info['max']}")
        for r in info["reasons"]:
            print(f"    {r}")

    # Show normalization traces if any
    traces = scoring.get("norm_traces", {})
    if traces:
        print(f"\n  {'─' * 50}")
        print(f"  Normalizations applied:")
        for field, trace in traces.items():
            print(f"    {field}: {trace['raw']} -> {trace['final']}")

    print(f"\n  Model: {scoring.get('llm', '?')}  |  Cost: ${scoring.get('cost', 0):.4f}")
    print(f"{'=' * 60}\n")


# ── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 score.py <pdf_path>")
        print("       python3 score.py --all")
        sys.exit(1)

    if sys.argv[1] == "--all":
        cfg = _load_config()
        pdf_dir = Path(cfg["data"]["pdfs"])
        pdfs = sorted(pdf_dir.glob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {pdf_dir}")
            sys.exit(1)

        results = []
        for pdf in pdfs:
            scoring = extract_and_score(str(pdf))
            print_report(scoring)
            results.append(scoring)

        # Summary table
        print(f"\n{'=' * 60}")
        print(f"  RANKING")
        print(f"{'=' * 60}")
        results.sort(key=lambda x: x["total_score"], reverse=True)
        for i, r in enumerate(results, 1):
            print(f"  {i}. {r['candidate']:<30} {r['total_score']}/{r['max_score']}")
        print()
    else:
        scoring = extract_and_score(sys.argv[1])
        print_report(scoring)
