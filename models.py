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
    Column, String, Integer, Numeric, DateTime, ForeignKey, Boolean, Text
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
