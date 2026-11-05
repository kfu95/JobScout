import json
import os
import re
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


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
    role_families: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    salary_floor: float | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)


def storage_dir():
    path = Path(os.getenv("ONBOARDING_DIR", "../config/onboarding"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


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


def load_facts(profile):
    path = storage_dir() / "facts.json"
    if not path.exists():
        facts = [Fact(**{**fact, "id": str(fact.get("id") or uuid.uuid4()), "verified": fact.get("verified", True)}).model_dump() for fact in profile.get("verified_experience", [])]
        write_private(path, json.dumps(facts).encode())
    return json.loads(path.read_text())


def save_facts(facts):
    write_private(storage_dir() / "facts.json", json.dumps(facts, ensure_ascii=False, indent=2).encode())


def load_preferences(profile):
    path = storage_dir() / "preferences.json"
    if path.exists():
        return Preferences.model_validate_json(path.read_text()).model_dump()
    return Preferences(target_titles=profile.get("target_titles", []), locations=profile.get("preferred_locations", [])).model_dump()


def save_preferences(preferences):
    write_private(storage_dir() / "preferences.json", preferences.model_dump_json(indent=2).encode())


def extract_text(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    if suffix == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
    raise ValueError("Upload a PDF or DOCX resume")


def parse_resume(text):
    facts = []
    current = None
    section = ""
    date_pattern = re.compile(r"\b(?:19|20)\d{2}\s*(?:-|–|—|to)\s*(?:(?:19|20)\d{2}|present|current)\b", re.I)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"(?:experience|employment|work experience|skills|technical skills|education)\s*:?\s*", line, re.I):
            section = line.rstrip(": ").lower()
            current = None
            continue
        if "skills" in section:
            skills = [s.strip() for s in re.split(r"[,;|]", line) if s.strip()]
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
    verified = [fact for fact in load_facts(profile) if fact.get("verified") is True]
    save_facts(verified + facts)
    return facts
