"""
Revive Capital Client Portal — backend API.

Three doors into the same filing cabinet:
  /auth/*      — borrower self-registration + login
  /me/*        — a logged-in borrower reading their OWN loan folder
  /internal/*  — Pulse CRM pushing loan updates in (staff-only, API-key locked)
"""
import os
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_

import models, schemas, auth
from database import engine, get_db, Base

Base.metadata.create_all(bind=engine)  # MVP: auto-create tables. Move to Alembic migrations once this is live.

app = FastAPI(title="Revive Capital Client Portal API")

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
