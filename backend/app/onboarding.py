import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Fact(BaseModel):
    id: str
    role: str = ""
    company: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    verified: bool = False


class Preferences(BaseModel):
    target_titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    years_experience: float | None = Field(default=None, ge=0)
    locations: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    salary_floor: float | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"

    @field_validator("target_titles", "seniority", "departments", "locations", "work_modes")
    @classmethod
    def clean_terms(cls, terms):
        return [term.strip() for term in terms if term.strip()]


def storage_dir():
    path = Path(os.getenv("ONBOARDING_DIR", "../config/onboarding"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def preference_score_lock():
    path = storage_dir() / ".preferences.lock"
    with path.open("a") as lock:
        path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def fact_store_lock():
    path = storage_dir() / ".facts.lock"
    with path.open("a") as lock:
        path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_private(path, data):
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


def _load_facts(profile):
    path = storage_dir() / "facts.json"
    if not path.exists():
        facts = [Fact(**{**fact, "id": str(fact.get("id") or uuid.uuid4()), "verified": fact.get("verified", True)}).model_dump() for fact in profile.get("verified_experience", [])]
        write_private(path, json.dumps(facts).encode())
    return json.loads(path.read_text())


def _save_facts(facts):
    write_private(storage_dir() / "facts.json", json.dumps(facts, ensure_ascii=False, indent=2).encode())


@contextmanager
def locked_facts(profile):
    with fact_store_lock():
        yield _load_facts(profile)


def load_facts(profile):
    with fact_store_lock():
        return _load_facts(profile)


def save_facts(facts):
    with fact_store_lock():
        _save_facts(facts)


def update_stored_fact(profile, fact_id, updated):
    with fact_store_lock():
        facts = _load_facts(profile)
        for index, existing in enumerate(facts):
            if existing["id"] == fact_id:
                if existing != updated:
                    facts[index] = updated
                    _save_facts(facts)
                return updated
    return None


def save_preferences(preferences):
    write_private(storage_dir() / "preferences.json", preferences.model_dump_json(indent=2).encode())


def extract_text(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    if suffix == ".docx":
        from docx import Document
        lines = []
        for paragraph in Document(BytesIO(content)).paragraphs:
            text = paragraph.text
            style = paragraph.style.name if paragraph.style else ""
            if text and style.casefold().startswith("list") and not re.match(r"^[•●▪*\-]", text):
                text = "• " + text
            lines.append(text)
        return "\n".join(lines)
    raise ValueError("Upload a PDF or DOCX resume")


def parse_resume(text):
    facts = []
    current = None
    section = ""
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    month_year = rf"(?:{month}\s+)?(?:19|20)\d{{2}}"
    date_pattern = re.compile(rf"\b{month_year}\s*(?:-|–|—|to)\s*(?:{month_year}|present|current)\b", re.I)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"(?:experience|employment|work experience|skills|technical skills|education)\s*:?\s*", line, re.I):
            section = line.rstrip(": ").lower()
            current = None
            continue
        if "skills" in section:
            skill_line = re.sub(r"^[•●▪*\-]\s*", "", line)
            skills = [s.strip() for s in re.split(r"[,;|]", skill_line) if s.strip()]
            if skills:
                facts.append(Fact(id=str(uuid.uuid4()), skills=skills).model_dump())
            continue
        date = date_pattern.search(line)
        if date:
            heading = (line[:date.start()] + line[date.end():]).strip(" ,|–—-")
            parts = [p.strip() for p in re.split(r"\s*[|–—]\s*|\s+ at \s+", heading, maxsplit=1, flags=re.I)]
            current = Fact(id=str(uuid.uuid4()), role=parts[0] if parts else "", company=parts[1] if len(parts) > 1 else "", dates=date.group()).model_dump()
            facts.append(current)
        elif current and re.match(r"^[•●▪*\-]", line):
            current["bullets"].append(re.sub(r"^[•●▪*\-]\s*", "", line))
    return facts


def upload_resume(filename, content, profile):
    text = extract_text(filename, content)
    if not text.strip():
        raise ValueError("No text found in resume")
    facts = parse_resume(text)
    if not facts:
        raise ValueError("No career facts recognized; check the resume format")
    with fact_store_lock():
        verified = [fact for fact in _load_facts(profile) if fact.get("verified") is True]
        _save_facts(verified + facts)
    return facts
