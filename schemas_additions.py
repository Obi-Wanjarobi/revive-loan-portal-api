# ============================================================
# ADD THESE to schemas.py — append at the end of the file.
# Written in Pydantic v1 style to match your existing schemas
# (BaseModel + Optional, same as your other classes).
# ============================================================

from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


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
        orm_mode = True


class Form1003In(BaseModel):
    data: dict
    submit: bool = False  # False = save as draft, True = mark submitted


class Form1003Out(BaseModel):
    data: dict
    status: str
    submitted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True
