"""Auth routes — POST /api/auth/login, /logout, GET /api/auth/me."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.auth.session import (
    SESSION_COOKIE,
    demo_clinician,
    issue_session_cookie,
    require_clinician,
)
from app.config import get_settings
from app.schemas.core import Clinician

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response) -> dict:
    settings = get_settings()
    email_ok = hmac.compare_digest(body.email, settings.demo_user_email)
    password_ok = hmac.compare_digest(body.password, settings.demo_user_password)
    if not (email_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_cookie(body.email),
        httponly=True,
        samesite="lax",
        secure=False,  # localhost demo only
        path="/",
    )
    return {"ok": True, "clinician": demo_clinician().model_dump()}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(clinician: Clinician = Depends(require_clinician)) -> dict:
    return {"clinician": clinician.model_dump()}
