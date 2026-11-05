from io import BytesIO
from html import escape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter


def packet_pdf(packet):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=50, rightMargin=50, topMargin=42, bottomMargin=42)
    styles = getSampleStyleSheet()
    story = [Paragraph(escape(packet.get("candidate_name") or "Candidate"), styles["Title"]), Spacer(1, 6)]
    if packet.get("candidate_email"):
        story.append(Paragraph(escape(packet["candidate_email"]), styles["Normal"]))
    if packet.get("summary"):
        story.extend([Spacer(1, 12), Paragraph(escape(packet["summary"]), styles["Normal"])])
    if packet.get("skills"):
        story.extend([Spacer(1, 12), Paragraph("Skills", styles["Heading2"]), Paragraph(escape(", ".join(packet["skills"])), styles["Normal"])])
    story.extend([Spacer(1, 12), Paragraph("Relevant Experience", styles["Heading2"])])
    for fact in packet.get("selected_facts", []):
        heading = " — ".join(str(fact.get(k, "")) for k in ("role", "company", "dates") if fact.get(k))
        story.append(Paragraph(escape(heading or "Verified Experience"), styles["Heading3"]))
        for bullet in fact.get("bullets", []):
            story.append(Paragraph("• " + escape(str(bullet)), styles["BodyText"]))
            story.append(Spacer(1, 4))
    doc.build(story)
    return buffer.getvalue()
