import html
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from urllib.parse import urlsplit

from sqlalchemy import select

from .models import Board, DigestJob, Job

log = logging.getLogger(__name__)


def send_digest(db, test=False, transport=None):
    host = os.getenv("SMTP_HOST", "")
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    recipient = os.getenv("DIGEST_TO", "")
    sender = os.getenv("SMTP_FROM", "") or username
    if not all((host, username, password, recipient, sender)):
        log.info("Digest skipped: SMTP settings are incomplete")
        return {"status": "unconfigured", "count": 0}
    query = (select(Job, Board).join(Board, Job.board_id == Board.id)
             .where(Job.is_open.is_(True), Job.score > 0)
             .order_by(Job.score.desc(), Job.first_seen.desc()))
    if not test:
        query = query.where(~select(DigestJob.job_id).where(DigestJob.job_id == Job.id).exists())
    jobs = db.execute(query).all()
    if not jobs and not test:
        return {"status": "empty", "count": 0}
    base = os.getenv("DASHBOARD_URL", "http://localhost:3000").rstrip("/")
    lines = ["Your matched jobs:", ""] if jobs else ["Your test digest is working. There are no matched jobs yet."]
    html_lines = ["Your matched jobs:"] if jobs else ["Your test digest is working. There are no matched jobs yet."]
    for job, board in jobs:
        salary = re.findall(r"(?im)^Annual USD salary(?: maximum)?:[^\n]+", job.description or "")
        entry = [f"{job.title} at {board.company} (match: {job.score})",
                 f"Location: {job.location or 'Unspecified'}",
                 *salary, f"Source: {board.provider}", f"{base}/?job={job.id}"]
        source_url = None
        if board.provider == "adzuna":
            source_url = "https://www.adzuna.com/"
        elif board.provider == "remoteok":
            source_url = job.apply_url
            parsed = urlsplit(source_url)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ("remoteok.com", "www.remoteok.com"):
                raise ValueError("Invalid Remote OK listing URL")
        lines.extend(entry + ([source_url] if source_url else []) + [""])
        html_lines.append("<p>" + "<br>".join(html.escape(line) for line in entry[:-1]))
        html_lines.append(f'<br><a href="{html.escape(entry[-1], quote=True)}">Open in JobScout</a>')
        if source_url:
            label = "Adzuna" if board.provider == "adzuna" else "Remote OK"
            html_lines.append(f'<br><a href="{html.escape(source_url, quote=True)}" style="display:inline-block;min-width:116px;min-height:23px">{label}</a>')
        html_lines.append("</p>")
    message = EmailMessage()
    message["Subject"] = "JobScout test digest" if test else "Your JobScout daily digest"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(lines))
    message.add_alternative("".join(html_lines), subtype="html")
    factory = transport or smtplib.SMTP
    with factory(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError("SMTP refused digest recipients")
    if not test:
        db.add_all(DigestJob(job_id=job.id) for job, _ in jobs)
        db.commit()
    return {"status": "sent", "count": len(jobs)}
