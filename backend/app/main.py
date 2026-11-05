import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .db import get_db, init_db
from .models import Board, Job, JobVersion, Application, ApplicationEvent, Lead, now
from .collectors import ALLOWED_PROVIDERS, SLUG_PATTERN, collect_all
from .scoring import prepare_packet, load_profile, score_posting
from .onboarding import Fact, Preferences, load_facts, save_facts, load_preferences, save_preferences, upload_resume, preference_score_lock
from .pdf import packet_pdf
from .gmail_api import callback_router as gmail_callback_router, router as gmail_router

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="Job Agent API", version="0.1.0", lifespan=lifespan)
app.include_router(gmail_callback_router)
app.include_router(gmail_router)
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])

class BoardIn(BaseModel):
    company: str = Field(min_length=2, max_length=200)
    provider: str
    slug: str

class LeadIn(BaseModel):
    source: str = Field(min_length=2, max_length=60)
    title: str = Field(min_length=2, max_length=250)
    company: str = Field(default="", max_length=200)
    url: str

class DecisionIn(BaseModel):
    decision: str

class StatusIn(BaseModel):
    status: str
    evidence: str = ""

def job_or_404(db, job_id):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job

def serialize_job(job, board, application=None, include_description=False):
    data = {"id": job.id, "company": board.company, "provider": board.provider,
            "title": job.title, "location": job.location, "apply_url": job.apply_url,
            "is_open": job.is_open, "score": job.score, "score_reason": job.score_reason,
            "score_method": job.score_method, "first_seen": job.first_seen,
            "last_seen": job.last_seen, "application_status": application.status if application else "not_started"}
    if include_description:
        data["description"] = job.description
        data["packet"] = application.packet if application else None
        data["application_id"] = application.id if application else None
    return jsonable_encoder(data)

def record_event(db, app_row, event_type, details=""):
    db.add(ApplicationEvent(application_id=app_row.id, event_type=event_type, details=details[:1500]))


def application_for_update(db, job_id):
    return db.scalar(select(Application).where(Application.job_id == job_id).with_for_update())

@app.get("/api/onboarding")
def onboarding():
    profile = load_profile()
    return {"facts": load_facts(profile), "preferences": load_preferences(profile)}

@app.post("/api/onboarding/resume")
async def resume_upload(file: UploadFile = File(...)):
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Resume exceeds 10 MB")
    try:
        return {"facts": upload_resume(file.filename or "", content, load_profile())}
    except Exception as e:
        raise HTTPException(400, f"Could not extract resume: {e}") from e

@app.put("/api/onboarding/facts/{fact_id}")
def update_fact(fact_id: str, fact: Fact, db: Session = Depends(get_db)):
    if fact.id != fact_id:
        raise HTTPException(400, "Fact ID cannot change")
    facts = load_facts(load_profile())
    for index, existing in enumerate(facts):
        if existing["id"] == fact_id:
            updated = fact.model_dump()
            changed = existing != updated
            if not changed:
                return existing
            applications = db.scalars(select(Application).order_by(Application.id).with_for_update()).all()
            facts[index] = updated
            save_facts(facts)
            for application in applications:
                selected = application.packet.get("selected_facts", []) if application.packet else []
                if application.status == "approved" and any(selected_fact.get("id") == fact_id for selected_fact in selected):
                    application.status = "prepared"
                    application.packet = {}
                    application.approved_at = None
                    record_event(db, application, "materials_invalidated", f"Fact {fact_id} changed")
            db.commit()
            return facts[index]
    raise HTTPException(404, "Fact not found")

@app.put("/api/onboarding/preferences")
def update_preferences(preferences: Preferences, db: Session = Depends(get_db)):
    with preference_score_lock():
        profile = load_profile()
        preference_data = preferences.model_dump()
        for job, board in db.execute(select(Job, Board).join(Board, Job.board_id == Board.id)):
            posting = {"title": job.title, "company": board.company, "location": job.location, "description": job.description}
            job.score, job.score_reason, job.score_method = score_posting(posting, profile, preference_data)
        save_preferences(preferences)
        db.commit()
    return preferences

@app.get("/api/health")
def health():
    return {"ok": True, "review_before_submit": True}

@app.get("/api/boards")
def list_boards(limit: int | None = Query(None, ge=1, le=500), db: Session = Depends(get_db)):
    query = select(Board).order_by(Board.company)
    if limit is not None:
        query = query.limit(limit)
    return jsonable_encoder(db.scalars(query).all())

@app.post("/api/boards")
def add_board(payload: BoardIn, db: Session = Depends(get_db)):
    if payload.provider not in ALLOWED_PROVIDERS or not SLUG_PATTERN.fullmatch(payload.slug):
        raise HTTPException(400, "Provider must be greenhouse, lever, or ashby, with a valid board slug")
    existing = db.scalar(select(Board).where(Board.provider == payload.provider, Board.slug == payload.slug))
    if existing:
        raise HTTPException(409, "Board already exists")
    row = Board(company=payload.company.strip(), provider=payload.provider, slug=payload.slug)
    db.add(row)
    db.commit()
    db.refresh(row)
    return jsonable_encoder(row)

@app.post("/api/collect")
async def collect_now(db: Session = Depends(get_db)):
    # Explicit manual trigger; the separate worker also polls periodically.
    return {"results": await collect_all(db)}

@app.get("/api/jobs")
def list_jobs(q: str = "", min_score: int = 0, open_only: bool = True, limit: int = 200, db: Session = Depends(get_db)):
    stmt = (select(Job, Board, Application).join(Board, Job.board_id == Board.id)
            .outerjoin(Application, Application.job_id == Job.id))
    if open_only:
        stmt = stmt.where(Job.is_open.is_(True))
    if q:
        pat = f"%{q[:100]}%"
        stmt = stmt.where((Job.title.ilike(pat)) | (Board.company.ilike(pat)) | (Job.location.ilike(pat)))
    stmt = (stmt.where(Job.score >= max(1, min_score))
            .order_by(Job.score.desc(), Job.first_seen.desc())
            .limit(max(1, min(limit, 500))))
    return [serialize_job(job, board, application) for job, board, application in db.execute(stmt)]

@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)):
    job = job_or_404(db, job_id)
    return serialize_job(job, db.get(Board, job.board_id), db.scalar(select(Application).where(Application.job_id == job_id)), True)

@app.get("/api/jobs/{job_id}/versions")
def job_versions(job_id: int, db: Session = Depends(get_db)):
    job_or_404(db, job_id)
    return jsonable_encoder(db.scalars(select(JobVersion).where(JobVersion.job_id == job_id).order_by(JobVersion.id.desc())).all())

@app.post("/api/jobs/{job_id}/prepare")
def prepare(job_id: int, db: Session = Depends(get_db)):
    job = job_or_404(db, job_id)
    if not job.is_open:
        raise HTTPException(409, "Job is no longer listed")
    existing = application_for_update(db, job.id)
    if existing and existing.status not in ("prepared", "skipped"):
        raise HTTPException(409, "Application has advanced beyond preparation")
    try:
        packet = prepare_packet({"title": job.title, "company": db.get(Board, job.board_id).company,
                                 "location": job.location, "description": job.description, "apply_url": job.apply_url})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    row = existing or Application(job_id=job.id)
    row.packet = packet
    row.status = "prepared"
    db.add(row)
    db.flush()
    record_event(db, row, "materials_prepared")
    db.commit()
    return {"status": row.status, "packet": packet}

@app.post("/api/jobs/{job_id}/decision")
def decide(job_id: int, payload: DecisionIn, db: Session = Depends(get_db)):
    job_or_404(db, job_id)
    if payload.decision not in ("approve", "skip"):
        raise HTTPException(400, "Decision must be approve or skip")
    row = application_for_update(db, job_id)
    if not row:
        if payload.decision == "approve":
            raise HTTPException(409, "Prepare an application packet first")
        row = Application(job_id=job_id, status="skipped", packet={})
    elif row.status not in ("prepared", "skipped"):
        raise HTTPException(409, "Decision cannot change after advancing")
    if payload.decision == "approve":
        if not row.packet or not packet_is_current(row.packet):
            raise HTTPException(409, "Prepare a packet with currently verified facts first")
        row.status, row.approved_at = "approved", now()
    else:
        row.status = "skipped"
    db.add(row)
    db.flush()
    record_event(db, row, "materials_" + row.status)
    db.commit()
    return {"status": row.status}

@app.post("/api/jobs/{job_id}/reported")
def reported(job_id: int, db: Session = Depends(get_db)):
    job_or_404(db, job_id)
    row = application_for_update(db, job_id)
    if not row or row.status != "approved" or not row.packet or not packet_is_current(row.packet):
        raise HTTPException(409, "Approve current materials before reporting submission")
    row.status, row.reported_at = "submission_reported", now()
    record_event(db, row, "submission_reported", "Manually reported; confirmation not verified")
    db.commit()
    return {"status": row.status}

@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: int, payload: StatusIn, db: Session = Depends(get_db)):
    job_or_404(db, job_id)
    row = application_for_update(db, job_id)
    if not row or row.status not in ("submission_reported", "confirmed", "interview", "rejected"):
        raise HTTPException(409, "Report a submission first")
    if payload.status not in ("confirmed", "interview", "rejected", "withdrawn"):
        raise HTTPException(400, "Invalid application status")
    if payload.status == "confirmed" and not payload.evidence.strip():
        raise HTTPException(400, "Confirmation requires a receipt or evidence note")
    row.status = payload.status
    record_event(db, row, "status_" + row.status, payload.evidence)
    db.commit()
    return {"status": row.status}

def packet_is_current(packet):
    verified = {f["id"]: f for f in load_facts(load_profile()) if f.get("verified") is True}
    return bool(packet.get("selected_facts")) and all(verified.get(f.get("id")) == f for f in packet["selected_facts"])

@app.get("/api/jobs/{job_id}/resume.pdf")
def resume_pdf(job_id: int, db: Session = Depends(get_db)):
    job_or_404(db, job_id)
    row = application_for_update(db, job_id)
    if not row or not row.packet:
        raise HTTPException(404, "Prepare packet first")
    if not packet_is_current(row.packet):
        raise HTTPException(409, "Prepare a packet with currently verified facts first")
    return Response(content=packet_pdf(row.packet), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="job-{job_id}-resume.pdf"'})

@app.get("/api/leads")
def list_leads(db: Session = Depends(get_db)):
    return jsonable_encoder(db.scalars(select(Lead).order_by(Lead.id.desc()).limit(200)).all())

@app.post("/api/leads")
def add_lead(payload: LeadIn, db: Session = Depends(get_db)):
    parsed = urlparse(payload.url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(400, "Only HTTPS job listing URLs are allowed")
    existing = db.scalar(select(Lead).where(Lead.url == payload.url))
    if existing:
        return jsonable_encoder(existing)
    lead = Lead(**payload.model_dump())
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return jsonable_encoder(lead)

@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    return {"boards": db.scalar(select(func.count(Board.id))) or 0,
            "jobs": db.scalar(select(func.count(Job.id)).where(Job.is_open.is_(True))) or 0,
            "strong_matches": db.scalar(select(func.count(Job.id)).where(Job.is_open.is_(True), Job.score >= 80)) or 0,
            "prepared": db.scalar(select(func.count(Application.id)).where(Application.status == "prepared")) or 0,
            "approved": db.scalar(select(func.count(Application.id)).where(Application.status == "approved")) or 0,
            "reported": db.scalar(select(func.count(Application.id)).where(Application.status == "submission_reported")) or 0}
