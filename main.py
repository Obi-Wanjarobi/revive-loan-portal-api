"""
Revive Capital Client Portal — backend API.

Three doors into the same filing cabinet:
  /auth/*      — borrower self-registration + login
  /me/*        — a logged-in borrower reading their OWN loan folder
  /internal/*  — Pulse CRM pushing loan updates in (staff-only, API-key locked)
"""
import os
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_

import models, schemas, auth
from database import engine, get_db, Base

Base.metadata.create_all(bind=engine)  # MVP: auto-create tables. Move to Alembic migrations once this is live.

app = FastAPI(title="Revive Capital Client Portal API")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15MB per file — plenty for a scanned PDF, keeps Postgres storage sane

cors_origins = os.environ.get("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ============================================================
# AUTH — borrower self-registration & login
# ============================================================

@app.post("/auth/register", response_model=schemas.TokenResponse)
def register(payload: schemas.RegisterRequest, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == payload.loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="We couldn't find that loan number. Double-check it and try again.")

    if loan.borrower_email.lower() != payload.email.lower():
        raise HTTPException(status_code=400, detail="That email doesn't match the one on file for this loan.")

    existing = db.query(models.Borrower).filter(models.Borrower.loan_id == loan.id).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account already exists for this loan. Try logging in instead.")

    borrower = models.Borrower(
        loan_id=loan.id,
        email=payload.email.lower(),
        password_hash=auth.hash_password(payload.password),
    )
    db.add(borrower)
    db.commit()
    db.refresh(borrower)

    token = auth.create_access_token(borrower.id)
    return schemas.TokenResponse(access_token=token)


@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    borrower = db.query(models.Borrower).filter(models.Borrower.email == payload.email.lower()).first()
    if not borrower or not auth.verify_password(payload.password, borrower.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    token = auth.create_access_token(borrower.id)
    return schemas.TokenResponse(access_token=token)


# ============================================================
# ME — a logged-in borrower viewing their own loan
# ============================================================

@app.get("/me/loan", response_model=schemas.LoanOut)
def my_loan(current_borrower: models.Borrower = Depends(auth.get_current_borrower), db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.id == current_borrower.loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found.")
    return loan


@app.post("/me/documents", response_model=schemas.DocumentOut)
async def upload_my_document(
    file: UploadFile = File(...),
    doc_type: str = Form(None),
    current_borrower: models.Borrower = Depends(auth.get_current_borrower),
    db: Session = Depends(get_db),
):
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large — please keep uploads under 15MB.")
    doc = models.Document(
        loan_id=current_borrower.loan_id,
        filename=file.filename,
        content_type=file.content_type,
        data=content,
        uploaded_by_role="borrower",
        uploaded_by_name=None,
        doc_type=doc_type,
        status="pending",
    )
    db.add(doc)
    db.add(models.ActivityEvent(loan_id=current_borrower.loan_id, text=f"You uploaded {file.filename}"))
    db.commit()
    db.refresh(doc)
    return doc


@app.get("/me/documents", response_model=list[schemas.DocumentOut])
def list_my_documents(current_borrower: models.Borrower = Depends(auth.get_current_borrower), db: Session = Depends(get_db)):
    return (
        db.query(models.Document)
        .filter(models.Document.loan_id == current_borrower.loan_id)
        .order_by(models.Document.created_at.desc())
        .all()
    )


@app.post("/me/form1003", response_model=schemas.Form1003Out)
def save_my_1003(payload: schemas.Form1003In, current_borrower: models.Borrower = Depends(auth.get_current_borrower), db: Session = Depends(get_db)):
    form = db.query(models.Form1003).filter(models.Form1003.loan_id == current_borrower.loan_id).first()
    if not form:
        form = models.Form1003(loan_id=current_borrower.loan_id, data={})
        db.add(form)
    form.data = payload.data
    if payload.submit:
        form.status = "submitted"
        form.submitted_at = datetime.utcnow()
        db.add(models.ActivityEvent(loan_id=current_borrower.loan_id, text="You submitted your loan application (1003)"))
    db.commit()
    db.refresh(form)
    return form


@app.get("/me/form1003", response_model=schemas.Form1003Out)
def get_my_1003(current_borrower: models.Borrower = Depends(auth.get_current_borrower), db: Session = Depends(get_db)):
    form = db.query(models.Form1003).filter(models.Form1003.loan_id == current_borrower.loan_id).first()
    if not form:
        return schemas.Form1003Out(data={}, status="draft", submitted_at=None, updated_at=None)
    return form


# ============================================================
# ADMIN — you (the owner), can log in and view ANY loan by number
# ============================================================

@app.post("/admin/login", response_model=schemas.TokenResponse)
def admin_login(payload: schemas.AdminLoginRequest):
    if not auth.ADMIN_EMAIL or not auth.ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="Admin login isn't configured yet.")
    if payload.email.lower() != auth.ADMIN_EMAIL.lower():
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if not auth.verify_password(payload.password, auth.ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    token = auth.create_admin_token()
    return schemas.TokenResponse(access_token=token)


@app.get("/admin/loans", dependencies=[Depends(auth.get_current_admin)])
def admin_list_loans(db: Session = Depends(get_db)):
    loans = db.query(models.Loan).all()
    return [{"loan_number": l.loan_number, "borrower_name": l.borrower_name, "stage": l.stage} for l in loans]


@app.get("/admin/loans/{loan_number}", response_model=schemas.LoanOut, dependencies=[Depends(auth.get_current_admin)])
def admin_get_loan(loan_number: str, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="No loan found with that number.")
    return loan


# ============================================================
# INTERNAL — Pulse CRM pushes updates here (API-key locked, never exposed to borrowers)
# ============================================================

@app.post("/internal/loans/upsert", response_model=schemas.LoanOut, dependencies=[Depends(auth.verify_internal_key)])
def upsert_loan(payload: schemas.LoanUpsert, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == payload.loan_number).first()
    if not loan:
        loan = models.Loan(loan_number=payload.loan_number)
        db.add(loan)

    for field, value in payload.dict(exclude_unset=True).items():
        setattr(loan, field, value)

    db.commit()
    db.refresh(loan)
    return loan


@app.post("/internal/loans/{loan_number}/conditions", dependencies=[Depends(auth.verify_internal_key)])
def sync_conditions(loan_number: str, conditions: list[schemas.ConditionUpsert], db: Session = Depends(get_db)):
    """Replaces the full condition list for a loan — simplest way for Pulse to stay authoritative."""
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Unknown loan number.")

    db.query(models.Condition).filter(models.Condition.loan_id == loan.id).delete()
    for c in conditions:
        db.add(models.Condition(loan_id=loan.id, title=c.title, detail=c.detail, done=c.done))
    db.commit()
    return {"status": "ok", "count": len(conditions)}


@app.post("/internal/loans/{loan_number}/activity", dependencies=[Depends(auth.verify_internal_key)])
def add_activity(loan_number: str, payload: schemas.ActivityCreate, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Unknown loan number.")
    db.add(models.ActivityEvent(loan_id=loan.id, text=payload.text))
    db.commit()
    return {"status": "ok"}


@app.post("/internal/loans/{loan_number}/documents", response_model=schemas.DocumentOut, dependencies=[Depends(auth.verify_internal_key)])
async def internal_upload_document(
    loan_number: str,
    file: UploadFile = File(...),
    uploaded_by_role: str = Form(...),   # "lo" | "processor" | "admin"
    uploaded_by_name: str = Form(None),
    doc_type: str = Form(None),
    db: Session = Depends(get_db),
):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Unknown loan number.")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large — please keep uploads under 15MB.")
    doc = models.Document(
        loan_id=loan.id,
        filename=file.filename,
        content_type=file.content_type,
        data=content,
        uploaded_by_role=uploaded_by_role,
        uploaded_by_name=uploaded_by_name,
        doc_type=doc_type,
        status="pending",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@app.get("/internal/loans/{loan_number}/documents", response_model=list[schemas.DocumentOut], dependencies=[Depends(auth.verify_internal_key)])
def internal_list_documents(loan_number: str, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Unknown loan number.")
    return (
        db.query(models.Document)
        .filter(models.Document.loan_id == loan.id)
        .order_by(models.Document.created_at.desc())
        .all()
    )


@app.get("/internal/documents/{document_id}/download", dependencies=[Depends(auth.verify_internal_key)])
def internal_download_document(document_id: str, db: Session = Depends(get_db)):
    """Pulse fetches the raw file bytes here to feed into Doc Intelligence for AI review."""
    doc = db.query(models.Document).filter(models.Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return Response(content=doc.data, media_type=doc.content_type or "application/octet-stream",
                     headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})


@app.patch("/internal/documents/{document_id}/status", dependencies=[Depends(auth.verify_internal_key)])
def internal_mark_document_status(document_id: str, status_value: str = Form(...), db: Session = Depends(get_db)):
    """Pulse calls this after running a document through Doc Intelligence, so the portal/CRM both know it's been reviewed."""
    doc = db.query(models.Document).filter(models.Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    doc.status = status_value
    db.commit()
    return {"status": "ok"}


@app.get("/internal/loans/{loan_number}/form1003", response_model=schemas.Form1003Out, dependencies=[Depends(auth.verify_internal_key)])
def internal_get_1003(loan_number: str, db: Session = Depends(get_db)):
    loan = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Unknown loan number.")
    form = db.query(models.Form1003).filter(models.Form1003.loan_id == loan.id).first()
    if not form:
        return schemas.Form1003Out(data={}, status="draft", submitted_at=None, updated_at=None)
    return form


# ============================================================
# PULSE PIPELINE SNAPSHOT — durable server-side backup of Pulse's
# entire in-browser LOANS + CONTACTS object. Pulse's own UI previously
# had NO server-side copy of this data at all — only localStorage.
# This makes every autosave durable, regardless of what happens to any
# one browser/device. Guarded by the same INTERNAL_API_KEY Pulse
# already uses for portal sync — no new credential to manage.
# ============================================================

@app.post("/internal/pulse/snapshot", dependencies=[Depends(auth.verify_internal_key)])
def save_pulse_snapshot(payload: dict, db: Session = Depends(get_db)):
    key = payload.get("key", "pipeline")
    data = payload.get("data")
    if data is None:
        raise HTTPException(status_code=422, detail="Missing 'data' field.")
    snap = db.query(models.PulseSnapshot).filter(models.PulseSnapshot.key == key).first()
    if not snap:
        snap = models.PulseSnapshot(key=key, data=data)
        db.add(snap)
    else:
        snap.data = data
    db.commit()
    db.refresh(snap)
    return {"status": "ok", "key": key, "updated_at": snap.updated_at}


@app.get("/internal/pulse/snapshot", dependencies=[Depends(auth.verify_internal_key)])
def get_pulse_snapshot(key: str = "pipeline", db: Session = Depends(get_db)):
    snap = db.query(models.PulseSnapshot).filter(models.PulseSnapshot.key == key).first()
    if not snap:
        return {"key": key, "data": None, "updated_at": None}
    return {"key": key, "data": snap.data, "updated_at": snap.updated_at}
