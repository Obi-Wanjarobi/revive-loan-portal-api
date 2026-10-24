"""
Pulse Broker Portal — third-party brokers who send deals to Revive Capital /
Revive Capital Holdings.

A separate door into the same filing cabinet, with its own key type:
  /broker/*        — a logged-in, APPROVED broker: their own linked files,
                     scenario submissions, uploads on their linked files
  /admin/brokers*  — the owner approves brokers, links them to loans, and
                     reviews submissions (admin token required)

Rules baked in (Revive operating rules):
  - Brokers never see compensation, lender incentives or processing fees.
    Nothing here reads app_settings / comp data.
  - Brokers see only loans an admin explicitly linked to them.
  - Borrower data shown to brokers is minimized: first name + last initial,
    no email, no property address, no rate lock details.
  - Scenario submissions take first name + last initial only and REQUIRE a
    recorded borrower-contact consent attestation.
  - Broker accounts start "pending"; only an admin can activate them.

Additive only: new tables (brokers, broker_loans, broker_submissions) are
created by Base.metadata.create_all; no existing table or route changes.
"""
import datetime
import html
import re
import time
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

import auth
import models
from database import Base, get_db

router = APIRouter()
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
BROKER_TOKEN_HOURS = 12
NOTIFY_TO = "jturner@myrevivecapital.com"
_broker_scheme = OAuth2PasswordBearer(tokenUrl="broker/login")


# ============================================================
# MODELS (new tables only)
# ============================================================

class Broker(Base):
    __tablename__ = "brokers"
    id = Column(String, primary_key=True, default=models.gen_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    company = Column(String, nullable=False)
    nmls_id = Column(String, nullable=False)
    company_nmls_id = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    states = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending | active | suspended
    terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BrokerLoan(Base):
    __tablename__ = "broker_loans"
    __table_args__ = (UniqueConstraint("broker_id", "loan_id", name="uq_broker_loan"),)
    id = Column(String, primary_key=True, default=models.gen_uuid)
    broker_id = Column(String, ForeignKey("brokers.id"), nullable=False, index=True)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BrokerSubmission(Base):
    __tablename__ = "broker_submissions"
    id = Column(String, primary_key=True, default=models.gen_uuid)
    ref = Column(String, unique=True, index=True, nullable=False)  # RB-1001
    broker_id = Column(String, ForeignKey("brokers.id"), nullable=False, index=True)
    entity = Column(String, nullable=True)
    timeline = Column(String, nullable=True)
    borrower_first = Column(String, nullable=False)
    borrower_last_initial = Column(String(1), nullable=False)
    fico_band = Column(String, nullable=True)
    purpose = Column(String, nullable=True)
    product = Column(String, nullable=True)
    occupancy = Column(String, nullable=True)
    property_type = Column(String, nullable=True)
    property_state = Column(String(2), nullable=True)
    target_close = Column(String, nullable=True)
    value = Column(Integer, nullable=True)
    loan_amount = Column(Integer, nullable=True)
    monthly_rent = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    consent_attested = Column(Boolean, nullable=False, default=False)
    consent_attested_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, nullable=False, default="new")  # new | reviewing | accepted | declined
    status_note = Column(String, nullable=True)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ============================================================
# SCHEMAS
# ============================================================

class AccessRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=2, max_length=160)
    nmls_id: str = Field(pattern=r"^\d{3,10}$")
    company_nmls_id: Optional[str] = Field(default=None, pattern=r"^\d{3,10}$")
    email: EmailStr
    phone: str = Field(min_length=7, max_length=30)
    states: Optional[str] = Field(default=None, max_length=200)
    password: str = Field(min_length=10, max_length=128)
    terms_accepted: bool


class BrokerLogin(BaseModel):
    email: EmailStr
    password: str


class SubmissionIn(BaseModel):
    entity: Optional[str] = Field(default=None, max_length=80)
    timeline: Optional[str] = Field(default=None, max_length=80)
    borrower_first: str = Field(min_length=1, max_length=60)
    borrower_last_initial: str = Field(pattern=r"^[A-Za-z]$")
    fico_band: Optional[str] = Field(default=None, max_length=40)
    purpose: Optional[str] = Field(default=None, max_length=60)
    product: Optional[str] = Field(default=None, max_length=60)
    occupancy: Optional[str] = Field(default=None, max_length=40)
    property_type: Optional[str] = Field(default=None, max_length=60)
    property_state: str = Field(pattern=r"^[A-Za-z]{2}$")
    target_close: Optional[str] = Field(default=None, max_length=20)
    value: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    loan_amount: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    monthly_rent: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    notes: Optional[str] = Field(default=None, max_length=2000)
    consent_attested: bool


class StatusIn(BaseModel):
    status: str
    note: Optional[str] = Field(default=None, max_length=300)


# ============================================================
# HELPERS
# ============================================================

_SSN = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_LONGNUM = re.compile(r"\b\d{9,17}\b")


def _reject_sensitive(text: Optional[str]):
    """Scenario notes must not carry SSNs or account numbers."""
    if text and (_SSN.search(text) or _LONGNUM.search(text)):
        raise HTTPException(status_code=422, detail="Notes look like they contain an SSN or account number. Remove it and resubmit. Revive collects documents through the secure Client Portal.")


def _name_short(full: Optional[str]) -> str:
    parts = (full or "").split()
    if not parts:
        return "Borrower"
    return parts[0] + (" " + parts[-1][0].upper() + "." if len(parts) > 1 else "")


def _notify(subject: str, body: str):
    """Internal alert to Jay only. Never includes borrower details; failure never blocks the request."""
    try:
        import campaign
        campaign.send_one(NOTIFY_TO, subject, body)
    except Exception:
        pass


_fails = {}


def _throttle(key: str):
    now = time.time()
    hits = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = hits
    if len(hits) >= 8:
        raise HTTPException(status_code=429, detail="Too many attempts. Wait 15 minutes and try again.")


def create_broker_token(broker_id: str) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(hours=BROKER_TOKEN_HOURS)
    return jwt.encode({"sub": broker_id, "role": "broker", "exp": exp}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


def current_broker(token: str = Depends(_broker_scheme), db: Session = Depends(get_db)) -> Broker:
    bad = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Log in again.")
    try:
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    except JWTError:
        raise bad
    if payload.get("role") != "broker":
        raise bad
    b = db.query(Broker).filter(Broker.id == payload.get("sub")).first()
    if not b or b.status != "active":
        raise bad
    return b


def _linked_loan(db: Session, broker: Broker, loan_number: str) -> models.Loan:
    loan = (db.query(models.Loan)
            .join(BrokerLoan, BrokerLoan.loan_id == models.Loan.id)
            .filter(BrokerLoan.broker_id == broker.id, models.Loan.loan_number == loan_number)
            .first())
    if not loan:
        raise HTTPException(status_code=404, detail="File not found.")
    return loan


def _loan_view(loan: models.Loan) -> dict:
    """Broker-safe view: no borrower email, address, rate lock, or anything financial beyond amount/type."""
    return {
        "loan_number": loan.loan_number,
        "borrower": _name_short(loan.borrower_name),
        "loan_type": loan.loan_type,
        "loan_amount": float(loan.loan_amount) if loan.loan_amount is not None else None,
        "stage": loan.stage,
        "stage_date": loan.stage_date,
        "est_closing_date": loan.est_closing_date,
        "updated_at": loan.updated_at,
        "loan_officer_name": loan.loan_officer_name,
        "conditions": [{"title": c.title, "done": bool(c.done)} for c in sorted(loan.conditions, key=lambda c: (c.done, str(c.created_at)))],
        "activity": [{"text": a.text, "at": a.created_at} for a in sorted(loan.activity, key=lambda a: str(a.created_at), reverse=True)[:10]],
    }


def _sub_view(s: BrokerSubmission) -> dict:
    return {k: getattr(s, k) for k in ("ref", "entity", "timeline", "borrower_first", "borrower_last_initial", "fico_band", "purpose",
                                         "product", "occupancy", "property_type", "property_state", "target_close", "value",
                                         "loan_amount", "monthly_rent", "notes", "status", "status_note", "created_at", "updated_at")}


def _sniff(content: bytes) -> Optional[str]:
    if content[:4] == b"%PDF":
        return "application/pdf"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


# ============================================================
# BROKER ROUTES
# ============================================================

@router.post("/broker/request-access")
def request_access(p: AccessRequest, db: Session = Depends(get_db)):
    if not p.terms_accepted:
        raise HTTPException(status_code=400, detail="Accept the partner terms to continue.")
    email = p.email.lower()
    if db.query(Broker).filter(Broker.email == email).first():
        # Same answer either way so the form can't be used to probe who has an account.
        return {"status": "received"}
    b = Broker(email=email, password_hash=auth.hash_password(p.password), full_name=p.full_name.strip(),
               company=p.company.strip(), nmls_id=p.nmls_id, company_nmls_id=p.company_nmls_id,
               phone=p.phone.strip(), states=(p.states or "").strip() or None, status="pending",
               terms_accepted_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(b)
    db.commit()
    e = html.escape
    _notify(f"Broker access request: {p.company}",
            f"<p><b>{e(p.full_name)}</b> ({e(p.company)}) requested Broker Portal access.</p>"
            f"<p>Individual NMLS: {e(p.nmls_id)}<br>Company NMLS: {e(p.company_nmls_id or '-')}<br>States: {e(p.states or '-')}</p>"
            f"<p>Verify the NMLS IDs on NMLS Consumer Access, then approve in the Broker Portal Admin tab.</p>")
    return {"status": "received"}


@router.post("/broker/login")
def broker_login(p: BrokerLogin, db: Session = Depends(get_db)):
    email = p.email.lower()
    _throttle(email)
    b = db.query(Broker).filter(Broker.email == email).first()
    if not b or not auth.verify_password(p.password, b.password_hash):
        _fails.setdefault(email, []).append(time.time())
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if b.status == "pending":
        raise HTTPException(status_code=403, detail="Your access request is still being reviewed. Revive will email you once it's approved.")
    if b.status != "active":
        raise HTTPException(status_code=403, detail="This account is not active. Contact Revive.")
    b.last_login_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    return {"access_token": create_broker_token(b.id), "token_type": "bearer"}


@router.get("/broker/me")
def broker_me(b: Broker = Depends(current_broker)):
    return {"full_name": b.full_name, "company": b.company, "email": b.email, "nmls_id": b.nmls_id}


@router.get("/broker/pipeline")
def broker_pipeline(b: Broker = Depends(current_broker), db: Session = Depends(get_db)):
    loans = (db.query(models.Loan).join(BrokerLoan, BrokerLoan.loan_id == models.Loan.id)
             .filter(BrokerLoan.broker_id == b.id).order_by(models.Loan.updated_at.desc()).all())
    subs = (db.query(BrokerSubmission).filter(BrokerSubmission.broker_id == b.id, BrokerSubmission.loan_id.is_(None))
            .order_by(BrokerSubmission.created_at.desc()).all())
    return {"loans": [_loan_view(l) for l in loans], "submissions": [_sub_view(s) for s in subs]}


@router.post("/broker/submissions")
def broker_submit(p: SubmissionIn, b: Broker = Depends(current_broker), db: Session = Depends(get_db)):
    if not p.consent_attested:
        raise HTTPException(status_code=400, detail="Confirm the borrower agreed to be contacted before submitting.")
    _reject_sensitive(p.notes)
    _reject_sensitive(p.borrower_first)
    n = db.query(BrokerSubmission).count()
    ref = f"RB-{1001 + n}"
    while db.query(BrokerSubmission).filter(BrokerSubmission.ref == ref).first():
        n += 1
        ref = f"RB-{1001 + n}"
    data = p.dict()
    data["borrower_last_initial"] = data["borrower_last_initial"].upper()
    data["property_state"] = data["property_state"].upper()
    s = BrokerSubmission(ref=ref, broker_id=b.id, consent_attested_at=datetime.datetime.now(datetime.timezone.utc), **data)
    db.add(s)
    db.commit()
    db.refresh(s)
    _notify(f"New broker deal {ref}: {b.company}",
            f"<p><b>{html.escape(b.company)}</b> submitted scenario <b>{ref}</b> ({html.escape(p.product or '-')}, {html.escape(p.purpose or '-')}).</p>"
            f"<p>Open the Broker Portal Admin tab to review.</p>")
    return _sub_view(s)


@router.post("/broker/loans/{loan_number}/documents")
async def broker_upload(loan_number: str, file: UploadFile = File(...), doc_type: str = Form(None),
                        b: Broker = Depends(current_broker), db: Session = Depends(get_db)):
    loan = _linked_loan(db, b, loan_number)
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large. Keep uploads under 15MB.")
    ctype = _sniff(content)
    if not ctype:
        raise HTTPException(status_code=415, detail="Upload a PDF, JPG, or PNG.")
    doc = models.Document(loan_id=loan.id, filename=(file.filename or "upload")[:200], content_type=ctype, data=content,
                          uploaded_by_role="broker", uploaded_by_name=f"{b.full_name} ({b.company})"[:200],
                          doc_type=(doc_type or None) and doc_type[:120], status="pending")
    db.add(doc)
    db.add(models.ActivityEvent(loan_id=loan.id, text=f"Broker uploaded {doc.filename}"))
    db.commit()
    db.refresh(doc)
    try:
        import portal_docs
        portal_docs.export_now(doc.id)
    except Exception:
        pass
    return {"filename": doc.filename, "doc_type": doc.doc_type, "created_at": doc.created_at}


@router.get("/broker/loans/{loan_number}/documents")
def broker_docs(loan_number: str, b: Broker = Depends(current_broker), db: Session = Depends(get_db)):
    loan = _linked_loan(db, b, loan_number)
    docs = (db.query(models.Document).filter(models.Document.loan_id == loan.id, models.Document.uploaded_by_role == "broker")
            .order_by(models.Document.created_at.desc()).all())
    return [{"filename": d.filename, "doc_type": d.doc_type, "created_at": d.created_at} for d in docs]


# ============================================================
# ADMIN ROUTES (owner only)
# ============================================================

ADMIN = [Depends(auth.get_current_admin)]


@router.get("/admin/brokers", dependencies=ADMIN)
def admin_brokers(db: Session = Depends(get_db)):
    out = []
    for b in db.query(Broker).order_by(Broker.created_at.desc()).all():
        links = (db.query(models.Loan.loan_number).join(BrokerLoan, BrokerLoan.loan_id == models.Loan.id)
                 .filter(BrokerLoan.broker_id == b.id).all())
        out.append({"id": b.id, "full_name": b.full_name, "company": b.company, "email": b.email, "phone": b.phone,
                    "nmls_id": b.nmls_id, "company_nmls_id": b.company_nmls_id, "states": b.states, "status": b.status,
                    "created_at": b.created_at, "approved_at": b.approved_at, "last_login_at": b.last_login_at,
                    "loans": [l[0] for l in links]})
    return out


@router.post("/admin/brokers/{broker_id}/status", dependencies=ADMIN)
def admin_broker_status(broker_id: str, p: StatusIn, db: Session = Depends(get_db)):
    if p.status not in ("pending", "active", "suspended"):
        raise HTTPException(status_code=400, detail="Status must be pending, active, or suspended.")
    b = db.query(Broker).filter(Broker.id == broker_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Broker not found.")
    b.status = p.status
    if p.status == "active" and not b.approved_at:
        b.approved_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    return {"id": b.id, "status": b.status}


@router.post("/admin/brokers/{broker_id}/loans/{loan_number}", dependencies=ADMIN)
def admin_link(broker_id: str, loan_number: str, db: Session = Depends(get_db)):
    b = db.query(Broker).filter(Broker.id == broker_id).first()
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not b or not loan:
        raise HTTPException(status_code=404, detail="Broker or loan not found.")
    if not db.query(BrokerLoan).filter(BrokerLoan.broker_id == b.id, BrokerLoan.loan_id == loan.id).first():
        db.add(BrokerLoan(broker_id=b.id, loan_id=loan.id))
        db.commit()
    return {"linked": loan_number}


@router.delete("/admin/brokers/{broker_id}/loans/{loan_number}", dependencies=ADMIN)
def admin_unlink(broker_id: str, loan_number: str, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if loan:
        db.query(BrokerLoan).filter(BrokerLoan.broker_id == broker_id, BrokerLoan.loan_id == loan.id).delete()
        db.commit()
    return {"unlinked": loan_number}


@router.get("/admin/broker-submissions", dependencies=ADMIN)
def admin_submissions(db: Session = Depends(get_db)):
    rows = db.query(BrokerSubmission, Broker).join(Broker, Broker.id == BrokerSubmission.broker_id) \
             .order_by(BrokerSubmission.created_at.desc()).all()
    out = []
    for s, b in rows:
        v = _sub_view(s)
        v.update({"id": s.id, "broker": b.full_name, "company": b.company, "broker_email": b.email,
                  "consent_attested_at": s.consent_attested_at})
        out.append(v)
    return out


@router.post("/admin/broker-submissions/{sub_id}/status", dependencies=ADMIN)
def admin_submission_status(sub_id: str, p: StatusIn, loan_number: Optional[str] = None, db: Session = Depends(get_db)):
    if p.status not in ("new", "reviewing", "accepted", "declined"):
        raise HTTPException(status_code=400, detail="Status must be new, reviewing, accepted, or declined.")
    s = db.query(BrokerSubmission).filter(BrokerSubmission.id == sub_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Submission not found.")
    s.status = p.status
    s.status_note = p.note
    if loan_number:
        loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found.")
        s.loan_id = loan.id
        if not db.query(BrokerLoan).filter(BrokerLoan.broker_id == s.broker_id, BrokerLoan.loan_id == loan.id).first():
            db.add(BrokerLoan(broker_id=s.broker_id, loan_id=loan.id))
    db.commit()
    return _sub_view(s)
