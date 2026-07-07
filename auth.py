"""
Two separate locks in this system, on purpose:

1. Borrower login (JWT)   -> opens exactly one loan folder, expires, tied to
                             a password only the borrower knows.
2. Internal sync key (INTERNAL_API_KEY) -> a single shared secret that only
                             Pulse CRM uses, from your office network / app,
                             never given to a borrower. This is the "staff
                             door" into the cabinet.
"""
import os
import datetime
import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status, Header
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from database import get_db
import models

SECRET_KEY = os.environ.get("JWT_SECRET", "CHANGE_ME_IN_RAILWAY_VARIABLES")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24 * 14  # borrowers stay logged in for 2 weeks

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "CHANGE_ME_IN_RAILWAY_VARIABLES")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))


def create_access_token(borrower_id: str) -> str:
    expire = datetime.datetime.utcnow() + datetime.timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": borrower_id, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_borrower(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.Borrower:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials — please log in again.",
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        borrower_id = payload.get("sub")
        if borrower_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    borrower = db.query(models.Borrower).filter(models.Borrower.id == borrower_id).first()
    if borrower is None:
        raise credentials_exception
    return borrower


def verify_internal_key(x_internal_key: str = Header(...)):
    """Guards the endpoints Pulse CRM calls to sync loan data."""
    if x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal API key.")
    return True
