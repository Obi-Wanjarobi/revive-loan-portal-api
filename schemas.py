from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    loan_number: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Borrower-facing loan view ----------
class ConditionOut(BaseModel):
    id: str
    title: str
    detail: Optional[str] = None
    done: bool
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ActivityOut(BaseModel):
    id: str
    text: str
    created_at: datetime

    class Config:
        from_attributes = True


class LoanOut(BaseModel):
    loan_number: str
    borrower_name: str
    property_address: Optional[str] = None
    loan_type: Optional[str] = None
    loan_amount: Optional[float] = None
    rate: Optional[float] = None
    apr: Optional[float] = None
    ltv: Optional[str] = None
    stage: Optional[str] = None
    stage_date: Optional[datetime] = None
    date_submitted: Optional[datetime] = None
    est_closing_date: Optional[str] = None
    rate_lock_expires: Optional[datetime] = None
    loan_officer_name: Optional[str] = None
    loan_officer_email: Optional[str] = None
    loan_officer_phone: Optional[str] = None
    conditions: List[ConditionOut] = []
    activity: List[ActivityOut] = []

    class Config:
        from_attributes = True


# ---------- Internal sync (Pulse -> backend) ----------
class LoanUpsert(BaseModel):
    loan_number: str
    borrower_name: str
    borrower_email: EmailStr
    property_address: Optional[str] = None
    loan_type: Optional[str] = None
    loan_amount: Optional[float] = None
    rate: Optional[float] = None
    apr: Optional[float] = None
    ltv: Optional[str] = None
    stage: Optional[str] = None
    stage_date: Optional[datetime] = None
    date_submitted: Optional[datetime] = None
    est_closing_date: Optional[str] = None
    rate_lock_expires: Optional[datetime] = None
    loan_officer_name: Optional[str] = None
    loan_officer_email: Optional[str] = None
    loan_officer_phone: Optional[str] = None


class ConditionUpsert(BaseModel):
    title: str
    detail: Optional[str] = None
    done: bool = False


class ActivityCreate(BaseModel):
    text: str
class DocumentOut(BaseModel):
    id: str
    filename: str
    content_type: Optional[str] = None
    uploaded_by_role: str
    uploaded_by_name: Optional[str] = None
    doc_type: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class Form1003In(BaseModel):
    data: dict
    submit: bool = False


class Form1003Out(BaseModel):
    data: dict
    status: str
    submitted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
