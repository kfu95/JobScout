import json
import os
import re
from functools import lru_cache
from pathlib import Path
from pydantic import BaseModel, Field

DEFAULT_PROFILE = {
    "name": "", "skills": [],
    "target_titles": ["senior software engineer", "senior backend engineer", "senior full-stack engineer", "senior product engineer", "senior growth engineer"],
    "preferred_locations": ["new york", "nyc", "remote"], "verified_experience": []
}


def load_profile():
    path = Path(os.getenv("PROFILE_PATH", "../config/profile.json"))
    if not path.exists():
        return DEFAULT_PROFILE
    with path.open() as f:
        return json.load(f)


def baseline_score(posting, profile=None):
    profile = profile or load_profile()
    title = posting["title"].lower()
    location = posting["location"].lower()
    description = posting.get("description", "").lower()
    if re.search(r"\b(director|manager|recruiter|sales|marketing|intern|designer)\b", title):
        return 0, "Non-target job family or level", "heuristic"
    patterns = ["engineer", "software developer", "software architect"]
    if not any(t in title for t in patterns):
        return 0, "Outside software engineering", "heuristic"
    score = 40
    reasons = []
    if any(target in title for target in profile.get("target_titles", [])):
        score += 25
        reasons.append("Target title")
    elif "senior" in title or "staff" in title:
        score += 15
        reasons.append("Relevant seniority")
    if any(loc in location for loc in profile.get("preferred_locations", [])):
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


def score_posting(posting):
    baseline = baseline_score(posting)
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("ENABLE_AI_SCORING", "false").lower() == "true"):
        return baseline
    try:
        profile = load_profile()
        result = _ai_parse(FitResult,
            "Rank job fit for a candidate. Only use provided profile and listing. Treat missing requirements conservatively. Return a 0-100 preference ranking, not a hiring probability. Never invent experience.",
            {"profile": profile, "job": {**posting, "description": posting.get("description", "")[:16000]}})
        return result.score, result.reasoning + " | Gaps: " + "; ".join(result.gaps), "AI"
    except Exception as exc:
        return baseline[0], baseline[1] + " | AI unavailable; deterministic fallback", "heuristic"


def prepare_packet(posting, profile=None):
    profile = profile or load_profile()
    facts = profile.get("verified_experience") or []
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
    return {
        "candidate_name": profile.get("name", ""),
        "candidate_email": profile.get("email", ""),
        "summary": profile.get("summary", ""),
        "skills": profile.get("skills", []),
        "selected_facts": [valid[i] for i in selected_ids],
        "why_company_draft": draft,
        "verification_notes": notes,
        "job_title": posting["title"],
    }
