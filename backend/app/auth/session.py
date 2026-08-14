"""Fake local session auth for the demo (spec §7 Screen 1 — 'do not spend
significant time here').

A single seeded clinician (DEMO_USER_* env) gets an HMAC-signed session cookie.
This is demo-local auth, not a production identity system.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.schemas.core import Clinician

SESSION_COOKIE = "careloop_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # demo-day length

DEMO_CLINICIAN_ID = "clin_maya_patel"
DEMO_CLINICIAN_NAME = "Dr. Maya Patel"
DEMO_CLINICIAN_ROLE = "Primary care / internal medicine"


def demo_clinician() -> Clinician:
    return Clinician(
        id=DEMO_CLINICIAN_ID,
        email=get_settings().demo_user_email,
        name=DEMO_CLINICIAN_NAME,
        role=DEMO_CLINICIAN_ROLE,
    )


def _sign(payload: str) -> str:
    secret = get_settings().app_secret.encode()
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def issue_session_cookie(email: str) -> str:
    payload = f"{base64.urlsafe_b64encode(email.encode()).decode()}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_session_cookie(value: str | None) -> str | None:
    """Return the session email if the cookie is valid and unexpired, else None."""
    if not value:
        return None
    parts = value.rsplit(".", 1)
    if len(parts) != 2:
        return None
    payload, signature = parts
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        email_b64, issued_at = payload.split(".", 1)
        if time.time() - int(issued_at) > SESSION_TTL_SECONDS:
            return None
        return base64.urlsafe_b64decode(email_b64.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def require_clinician(request: Request) -> Clinician:
    """FastAPI dependency: resolve the current clinician or raise 401."""
    email = verify_session_cookie(request.cookies.get(SESSION_COOKIE))
    if email is None or email != get_settings().demo_user_email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return demo_clinician()
