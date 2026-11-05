import json
import os
import re
from pathlib import Path
from pydantic import BaseModel, Field
from .onboarding import load_facts, load_preferences

DEFAULT_PROFILE = {
    "name": "", "skills": [],
    "target_titles": ["senior software engineer", "senior backend engineer", "senior full-stack engineer", "senior product engineer", "senior growth engineer"],
    "preferred_locations": ["new york", "nyc", "remote"], "verified_experience": []
}

USD_RANGE_PATTERN = re.compile(
    r"(?:\$\s*|USD\s+)(\d[\d,]*(?:\.\d+)?)\s*(k)?"
    r"(?:\s*(?:-|–|—|to)\s*(?:\$\s*|USD\s+)?(\d[\d,]*(?:\.\d+)?)\s*(k)?)?",
    re.I,
)
SALARY_CONTEXT_PATTERN = re.compile(r"\b(?:salary|base[\s-]+pay|pay[\s-]+range|compensation[\s-]+range)\b", re.I)
NON_SALARY_PATTERN = re.compile(r"\b(?:bonus|stipend|equity|stock|learning|benefit)\b", re.I)
SALARY_SUFFIX_PATTERN = re.compile(r"\s*(?:per year|annually)?\s*(?:annual\s+)?(?:base\s+salary|salary\s+range|base[\s-]+pay|pay[\s-]+range)\b", re.I)


def load_profile():
    path = Path(os.getenv("PROFILE_PATH", "../config/profile.json"))
    if not path.exists():
        return DEFAULT_PROFILE
    with path.open() as f:
        return json.load(f)


def stated_salaries(description):
    matches = []
    for context in SALARY_CONTEXT_PATTERN.finditer(description):
        tail = description[context.end():]
        boundary = re.search(r"(?<!\d)[\n.!?](?!\d)", tail)
        segment = tail[:boundary.start()] if boundary else tail
        amount = USD_RANGE_PATTERN.search(segment[:120])
        if amount and not NON_SALARY_PATTERN.search(segment[:amount.start()]):
            matches.append(amount)
    for amount in USD_RANGE_PATTERN.finditer(description):
        if SALARY_SUFFIX_PATTERN.match(description[amount.end():amount.end() + 60]):
            matches.append(amount)
    return [
        float(number.replace(",", "")) * (1000 if suffix else 1)
        for match in matches
        for number, suffix in ((match.group(1), match.group(2)), (match.group(3), match.group(4)))
        if number
    ]


def preference_filter(posting, preferences):
    text = " ".join(str(posting.get(k, "")) for k in ("title", "description", "industry")).lower()
    company = str(posting.get("company", "")).lower()
    for key, value in (("excluded_companies", company), ("excluded_keywords", text)):
        if any(term.strip().lower() in value for term in preferences.get(key, []) if term.strip()):
            return False, "Excluded by preferences"
    floor = preferences.get("salary_floor")
    description = str(posting.get("description", ""))
    salaries = [amount for amount in stated_salaries(description) if amount >= 10000]
    if floor and salaries and max(salaries) < floor:
        return False, "Below salary floor"
    return True, "Salary unknown" if not salaries else "Salary stated"


def matches_phrase(value, terms):
    return any(
        re.search(rf"(?<!\w){re.escape(term.strip().lower())}(?!\w)", value)
        for term in terms
        if term.strip()
    )


def baseline_score(posting, profile=None, preferences=None):
    profile = profile if profile is not None else load_profile()
    preferences = preferences if preferences is not None else load_preferences(profile)
    allowed, salary_note = preference_filter(posting, preferences)
    if not allowed:
        return 0, salary_note, "heuristic"
    title = posting["title"].lower()
    location = posting.get("location", "").lower()
    description = posting.get("description", "").lower()
    target_titles = [term.strip() for term in preferences.get("target_titles", []) if term.strip()]
    if target_titles:
        if not matches_phrase(title, target_titles):
            return 0, "Outside target titles", "heuristic"
    else:
        if re.search(r"\b(director|manager|recruiter|sales|marketing|intern|designer)\b", title):
            return 0, "Non-target job family or level", "heuristic"
        patterns = ["engineer", "software developer", "software architect"]
        if not any(pattern in title for pattern in patterns):
            return 0, "Outside software engineering", "heuristic"
    score = 40
    reasons = [salary_note]
    if target_titles:
        score += 25
        reasons.append("Target title")
    if matches_phrase(title, preferences.get("seniority", [])):
        score += 15
        reasons.append("Relevant seniority")
    if matches_phrase(location, preferences.get("locations", [])) or matches_phrase(location, preferences.get("work_modes", [])):
        score += 15
        reasons.append("Preferred location")
    elif not location:
        reasons.append("Location not specified")
    else:
        reasons.append("Verify location manually")
    skills = [s for s in profile.get("skills", []) if len(s) >= 2]
    matches = [s for s in skills if s.lower() in description]
    score += min(20, 4 * len(matches))
    if matches:
        reasons.append("Matched skills: " + ", ".join(matches[:5]))
    return min(100, score), "; ".join(reasons) or "Engineering role", "heuristic"


class FitResult(BaseModel):
    score: int = Field(ge=0, le=100)
    reasoning: str
    strengths: list[str]
    gaps: list[str]


class PacketDraft(BaseModel):
    selected_fact_ids: list[str]
    why_company_draft: str
    verification_notes: list[str]


def _ai_parse(model_type, system_prompt, user_payload):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=35, max_retries=1)
    response = client.responses.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        input=[{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        text_format=model_type,
        store=False,
    )
    if response.output_parsed is None:
        raise ValueError("No structured model response")
    return response.output_parsed


def score_posting(posting, profile=None, preferences=None):
    profile = profile if profile is not None else load_profile()
    preferences = preferences if preferences is not None else load_preferences(profile)
    baseline = baseline_score(posting, profile, preferences)
    if baseline[0] == 0:
        return baseline
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("ENABLE_AI_SCORING", "false").lower() == "true"):
        return baseline
    try:
        result = _ai_parse(FitResult,
            "Rank job fit for a candidate. Only use provided profile and listing. Treat missing requirements conservatively. Return a 0-100 preference ranking, not a hiring probability. Never invent experience.",
            {"profile": profile, "preferences": preferences, "job": {**posting, "description": posting.get("description", "")[:16000]}})
        return result.score, result.reasoning + " | Gaps: " + "; ".join(result.gaps), "AI"
    except Exception as exc:
        return baseline[0], baseline[1] + " | AI unavailable; deterministic fallback", "heuristic"


def prepare_packet(posting, profile=None):
    profile = profile or load_profile()
    facts = [f for f in load_facts(profile) if f.get("verified") is True]
    if not facts:
        raise ValueError("Fill config/profile.json verified_experience before generating applications")
    valid = {str(f["id"]): f for f in facts if f.get("id")}
    if not valid:
        raise ValueError("Each verified_experience item needs an id")
    selected_ids = list(valid.keys())[:8]
    draft = "Draft a company-specific answer here after reviewing the posting and verified achievements."
    notes = ["Confirm dates, metrics, eligibility and every application answer before submission."]
    if os.getenv("OPENAI_API_KEY"):
        try:
            result = _ai_parse(PacketDraft,
                "Select only IDs from supplied VERIFIED facts relevant to the job. Never invent work, numbers, dates or skills. Draft a short answer to 'Why this company?' based on the listing and facts; flag speculation and uncertain claims. Do not submit anything.",
                {"verified_facts": facts, "job": {**posting, "description": posting.get("description", "")[:16000]}})
            selected_ids = [i for i in result.selected_fact_ids if i in valid][:8] or selected_ids
            draft, notes = result.why_company_draft, result.verification_notes
        except Exception:
            notes.append("AI preparation failed. Deterministic packet created instead.")
    verified_skills = [skill for fact_id in selected_ids for skill in valid[fact_id].get("skills", [])]
    return {
        "candidate_name": profile.get("name", ""),
        "candidate_email": profile.get("email", ""),
        "summary": "",
        "skills": verified_skills,
        "selected_facts": [valid[i] for i in selected_ids],
        "why_company_draft": draft,
        "verification_notes": notes,
        "job_title": posting["title"],
    }
