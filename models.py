"""
Database models for the Revive Capital client portal.

Think of this file as designing the folders inside a shared filing cabinet:
- Loan        = one manila folder per loan, the master record
- Condition   = a sticky note inside that folder ("still need: pay stubs")
- ActivityEvent = the log taped to the folder cover ("Jun 1: moved to Conditions")
- Borrower    = the key that unlocks ONE folder — never the whole cabinet

Pulse CRM (used by loan officers) and the client portal (used by borrowers)
both read/write through this same cabinet, so there's only ever one source
of truth.
"""
from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, ForeignKey, Boolean, Text, LargeBinary, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
import uuid


def gen_uuid():
    return str(uuid.uuid4())


class Loan(Base):
    __tablename__ = "loans"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_number = Column(String, unique=True, index=True, nullable=False)  # e.g. "LN-2038"
    borrower_name = Column(String, nullable=False)
    borrower_email = Column(String, index=True, nullable=False)  # used to verify self-registration
    property_address = Column(String, nullable=True)

    loan_type = Column(String, nullable=True)       # "FHA 30yr"
    loan_amount = Column(Numeric(12, 2), nullable=True)
    rate = Column(Numeric(6, 3), nullable=True)      # 6.875
    apr = Column(Numeric(6, 3), nullable=True)
    ltv = Column(String, nullable=True)

    stage = Column(String, nullable=False, default="Application")
    # Application, Processing, Underwriting, Conditions, Clear to Close, Funded, On Hold, Withdrawn
    stage_date = Column(DateTime(timezone=True), nullable=True)  # when the loan entered its current stage

    date_submitted = Column(DateTime(timezone=True), nullable=True)
    est_closing_date = Column(String, nullable=True)   # kept as free text like Pulse's "Jun 5"
    rate_lock_expires = Column(DateTime(timezone=True), nullable=True)

    loan_officer_name = Column(String, nullable=True)
    loan_officer_email = Column(String, nullable=True)
    loan_officer_phone = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    conditions = relationship("Condition", back_populates="loan", cascade="all, delete-orphan")
    activity = relationship("ActivityEvent", back_populates="loan", cascade="all, delete-orphan")
    borrower_account = relationship("Borrower", back_populates="loan", uselist=False)


class Condition(Base):
    __tablename__ = "conditions"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=False)
    title = Column(String, nullable=False)
    detail = Column(String, nullable=True)
    done = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    loan = relationship("Loan", back_populates="conditions")


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=False)
    text = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    loan = relationship("Loan", back_populates="activity")


class Borrower(Base):
    """
    A borrower's login. One borrower account maps to exactly one loan —
    this is the "key" that only opens one folder in the cabinet.
    """
    __tablename__ = "borrowers"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_id = Column(String, ForeignKey("loans.id"), unique=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    loan = relationship("Loan", back_populates="borrower_account")


class Document(Base):
    """
    A single uploaded file — borrower-uploaded (via the portal) or
    staff-uploaded (via Pulse CRM, LO/LOA/Processor). The raw bytes are
    stored directly in Postgres (bytea) for simplicity — fine for typical
    mortgage PDFs (a few MB each). If document volume grows large, moving
    to S3/Railway volume storage later is a clean upgrade path — this
    schema doesn't need to change, only where `data` physically lives.
    """
    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=False)
    filename = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    data = Column(LargeBinary, nullable=False)
    uploaded_by_role = Column(String, nullable=False)   # "borrower" | "lo" | "processor" | "admin"
    uploaded_by_name = Column(String, nullable=True)
    doc_type = Column(String, nullable=True)             # e.g. "Conditional Loan Approval", "Pay Stub" — borrower picks from a simple list, staff can leave blank
    status = Column(String, nullable=False, default="pending")  # pending | analyzed
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    loan = relationship("Loan")


class PulseSnapshot(Base):
    """
    A full, durable backup of Pulse's own loan pipeline + contacts — the
    complete in-app data structure (steps, contacts, disclosure tracking,
    conditions, everything), not just the subset the client portal needs.

    This exists because Pulse's pipeline previously lived ONLY in browser
    localStorage — one lost/cleared browser and it was gone, with no
    server-side copy anywhere. This table is that server-side copy.

    Single-row-per-key design: one row holds the entire org's pipeline as
    one JSON blob. Simple, and matches how Pulse already treats LOANS as
    one in-memory object. If per-loan-officer or per-branch snapshots are
    ever needed, the `key` column already supports that (e.g. "pipeline",
    "pipeline_branch_irvine") without a schema change.
    """
    __tablename__ = "pulse_snapshots"

    id = Column(String, primary_key=True, default=gen_uuid)
    key = Column(String, unique=True, index=True, nullable=False, default="pipeline")
    data = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Form1003(Base):
    """
    Streamlined borrower application intake — NOT a compliance-grade
    replacement for the final signed 1003/URLA. Covers the fields a loan
    officer actually needs to start processing a file: borrower/contact
    info, current address, employment, income, assets, liabilities, and
    a handful of declarations. Stored as one flexible JSON blob (`data`)
    rather than dozens of individual columns, since the full URLA has 100+
    fields and most loans only ever touch a subset of them.
    """
    __tablename__ = "form_1003"

    id = Column(String, primary_key=True, default=gen_uuid)
    loan_id = Column(String, ForeignKey("loans.id"), unique=True, nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    status = Column(String, nullable=False, default="draft")  # draft | submitted
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    loan = relationship("Loan")
