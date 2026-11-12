import json
import os
from pathlib import Path
from pydantic import BaseModel, Field
from .onboarding import load_facts
from .preferences import candidate_profile, eligibility, load_preferences, preference_bonuses

DEFAULT_PROFILE = {
    "name": "", "skills": [], "target_titles": [],
    "preferred_locations": [], "verified_experience": []
}

def load_profile():
    path = Path(os.getenv("PROFILE_PATH", "../config/profile.json"))
    if not path.exists():
        return DEFAULT_PROFILE
    with path.open() as f:
        return json.load(f)

def baseline_score(posting, profile=None, preferences=None):
    profile = profile if profile is not None else load_profile()
    preferences = preferences if preferences is not None else load_preferences(profile)
    allowed, salary_note = eligibility(posting, preferences)
    if not allowed:
        return 0, salary_note, "heuristic"
    title = posting["title"].lower()
    location = posting.get("location", "").lower()
    description = posting.get("description", "").lower()
    target_titles = [term.strip() for term in preferences.get("target_titles", []) if term.strip()]
    score = 40
    reasons = [salary_note]
    if target_titles:
        score += 25
        reasons.append("Target title")
    selected_seniority_bonus, selected_location_bonus, selected_work_mode_bonus = preference_bonuses(
        title, location, preferences
    )
    if selected_seniority_bonus:
        score += selected_seniority_bonus
        reasons.append("Relevant seniority")
    if selected_location_bonus:
        score += selected_location_bonus
        reasons.append("Preferred location")
    elif not location:
        reasons.append("Location not specified")
    else:
        reasons.append("Verify location manually")
    if selected_work_mode_bonus:
        score += selected_work_mode_bonus
        reasons.append("Preferred work mode")
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
        model_profile = candidate_profile(profile)
        result = _ai_parse(FitResult,
            "Rank job fit for a candidate. Only use provided profile and listing. Treat missing requirements conservatively. Return a 0-100 preference ranking, not a hiring probability. Never invent experience.",
            {"profile": model_profile, "preferences": preferences, "job": {**posting, "description": posting.get("description", "")[:16000]}})
        boost = sum(preference_bonuses(posting["title"].lower(), posting.get("location", "").lower(), preferences))
        reasoning = result.reasoning + " | Gaps: " + "; ".join(result.gaps)
        if boost:
            reasoning += " | Saved preference boost"
        return max(1, min(100, result.score + boost)), reasoning, "AI"
    except Exception:
        return baseline[0], baseline[1] + " | AI unavailable; deterministic fallback", "heuristic"


def prepare_packet(posting, profile=None, facts=None):
    profile = profile or load_profile()
    facts = facts if facts is not None else load_facts(profile)
    facts = [f for f in facts if f.get("verified") is True]
    if not facts:
        raise ValueError("Upload a resume and verify at least one career fact in onboarding before generating applications")
    valid = {str(f["id"]): f for f in facts if f.get("id")}
    if not valid:
        raise ValueError("Each verified onboarding fact needs an id")
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
