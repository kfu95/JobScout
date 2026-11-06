import re


USD_RANGE_PATTERN = re.compile(
    r"(?:\$\s*|USD\s+)(\d[\d,]*(?:\.\d+)?)\s*(k)?"
    r"(?:\s*(?:-|–|—|to)\s*(?:\$\s*|USD\s+)?(\d[\d,]*(?:\.\d+)?)\s*(k)?)?",
    re.I,
)
SALARY_CONTEXT_PATTERN = re.compile(r"\b(?:salary|base[\s-]+pay|pay[\s-]+range|compensation[\s-]+range)\b", re.I)
NON_SALARY_PATTERN = re.compile(r"\b(?:bonus|stipend|equity|stock|learning|benefit)\b", re.I)
NON_SALARY_SUFFIX_PATTERN = re.compile(r"^\s*(?:signing\s+)?(?:bonus|stipend|equity|stock|learning|benefit)\b", re.I)
SALARY_SUFFIX_PATTERN = re.compile(r"\s*(?:per year|annually)?\s*(?:annual\s+)?(?:base\s+salary|salary\s+range|base[\s-]+pay|pay[\s-]+range)\b", re.I)
FOREIGN_DOLLAR_PREFIX_PATTERN = re.compile(r"\b(?:CA|AU|NZ)\s*\$", re.I)
CURRENCY_CODE_BEFORE_PATTERN = re.compile(r"\b([A-Za-z]{3})\s*(?::\s*)?$")
CURRENCY_CODE_AFTER_PATTERN = re.compile(r"^\s*[([]?\s*([A-Za-z]{3})\b")
CURRENCY_CODE_LABEL_PATTERN = re.compile(r"\b(?:currency|in)\s*:?\s*([A-Za-z]{3})\b", re.I)
CURRENCY_CODES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL
""".split())
NON_ANNUAL_PERIOD_PATTERN = re.compile(r"(?:/|\b(?:per|a|an)\s+)(?:hour|hr|day|week|month)\b|\b(?:hourly|daily|weekly|monthly)\b", re.I)
PROFILE_PREFERENCE_FIELDS = {
    "target_titles", "preferred_locations", "locations", "seniority",
    "work_modes", "salary_floor", "currency",
}


def load_preferences(profile):
    from .onboarding import Preferences, storage_dir

    path = storage_dir() / "preferences.json"
    if path.exists():
        return Preferences.model_validate_json(path.read_text()).model_dump()
    return Preferences(
        target_titles=profile.get("target_titles", []),
        locations=profile.get("preferred_locations", []),
        salary_floor=profile.get("salary_floor"),
    ).model_dump()


def candidate_profile(profile):
    return {key: value for key, value in profile.items() if key not in PROFILE_PREFERENCE_FIELDS}


def matches_phrase(value, terms):
    return any(
        re.search(rf"(?<!\w){re.escape(term.strip().lower())}(?!\w)", value)
        for term in terms
        if term.strip()
    )


def stated_salaries(description):
    matches = []
    for context in SALARY_CONTEXT_PATTERN.finditer(description):
        tail = description[context.end():]
        boundary = re.search(r"\n\s*\n|(?<!\d)[.!?](?!\d)", tail)
        segment = tail[:boundary.start()] if boundary else tail
        amount = USD_RANGE_PATTERN.search(segment[:120])
        if amount and not NON_SALARY_PATTERN.search(segment[:amount.start()]) and annual_usd_amount(
            description, context.end() + amount.start(), context.end() + amount.end()
        ):
            matches.append(amount)
    for amount in USD_RANGE_PATTERN.finditer(description):
        if SALARY_SUFFIX_PATTERN.match(description[amount.end():amount.end() + 60]) and annual_usd_amount(
            description, amount.start(), amount.end()
        ):
            matches.append(amount)
    return [
        float(number.replace(",", "")) * (1000 if suffix else 1)
        for match in matches
        for number, suffix in ((match.group(1), match.group(2)), (match.group(3), match.group(4)))
        if number
    ]


def annual_usd_amount(description, start, end):
    context = description[max(0, start - 40):end + 40]
    before = description[max(0, start - 40):start]
    after = description[end:end + 40]
    currency_matches = (
        CURRENCY_CODE_BEFORE_PATTERN.search(before),
        CURRENCY_CODE_AFTER_PATTERN.search(after),
        CURRENCY_CODE_LABEL_PATTERN.search(before),
    )
    has_foreign_code = any(
        match and match.group(1).upper() in CURRENCY_CODES and match.group(1).upper() != "USD"
        for match in currency_matches
    )
    return not (
        FOREIGN_DOLLAR_PREFIX_PATTERN.search(context)
        or has_foreign_code
        or NON_ANNUAL_PERIOD_PATTERN.search(context)
        or NON_SALARY_SUFFIX_PATTERN.search(after)
    )


def salary_filter(posting, preferences):
    floor = preferences.get("salary_floor")
    maximum = posting.get("salary_max")
    if maximum is not None:
        salaries = [float(maximum)]
    else:
        description = str(posting.get("description", ""))
        salaries = [amount for amount in stated_salaries(description) if amount >= 10000]
    if floor and salaries and max(salaries) < floor:
        return False, "Below salary floor"
    return True, "Salary unknown" if not salaries else "Salary stated"


def eligibility(posting, preferences):
    location = str(posting.get("location", "")).lower()
    region = str(posting.get("remote_region", "")).lower().strip()
    if not region and location.startswith("remote "):
        region = location.removeprefix("remote ").strip()
    if region and not any(
        term in region
        for term in ("anywhere", "worldwide", "global", "united states", "usa", "u.s.", "north america", "new york", "nyc")
    ):
        return False, "Outside eligible remote region"

    title = str(posting.get("title", "")).lower()
    target_titles = [term.strip() for term in preferences.get("target_titles", []) if term.strip()]
    if target_titles:
        if not matches_phrase(title, target_titles):
            return False, "Outside target titles"
    else:
        if re.search(r"\b(director|manager|recruiter|sales|marketing|intern|designer)\b", title):
            return False, "Non-target job family or level"
        if not any(pattern in title for pattern in ("engineer", "software developer", "software architect")):
            return False, "Outside software engineering"

    locations = preferences.get("locations", [])
    remote_rescue = matches_phrase(location, ["remote"]) and matches_phrase(
        "remote", preferences.get("work_modes", [])
    )
    if locations and not matches_phrase(location, locations) and not remote_rescue:
        return False, "Outside preferred locations"

    return salary_filter(posting, preferences)


def matches(posting, preferences):
    return eligibility(posting, preferences)[0]


def preference_bonuses(title, location, preferences):
    return (
        15 if matches_phrase(title, preferences.get("seniority", [])) else 0,
        15 if matches_phrase(location, preferences.get("locations", [])) else 0,
        15 if matches_phrase(location, preferences.get("work_modes", [])) else 0,
    )
