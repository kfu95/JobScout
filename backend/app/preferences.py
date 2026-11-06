from .scoring import load_profile


def discovery_preferences():
    profile = load_profile()
    return {
        "locations": profile.get("preferred_locations") or [],
        "salary_floor": profile.get("salary_floor") or 0,
    }


def matches(posting, preferences):
    region = posting.get("remote_region", "").lower().strip()
    if region and not any(term in region for term in ("anywhere", "worldwide", "global", "united states", "usa", "u.s.", "north america", "new york", "nyc")):
        return False
    location = posting.get("location", "").lower()
    locations = [s.lower() for s in preferences["locations"] if s]
    if location and locations and not any(s in location for s in locations):
        return False
    maximum = posting.get("salary_max")
    if maximum is not None and preferences["salary_floor"] and maximum < float(preferences["salary_floor"]):
        return False
    return True
