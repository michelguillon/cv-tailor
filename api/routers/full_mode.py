"""api/routers/full_mode.py — Full Mode Unlock Gate endpoints (SPEC §12.7, D-38).

Capabilities + a one-time unlock that issues a signed HttpOnly capability cookie, so the
Web UI can run full (Sonnet) mode without re-sending the raw key per run. Enforcement of
the cookie on the actual run lives in `runs.py`; this router only mints/clears it and
reports state. All checks fail closed (see `api/security.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from api.security import (
    COOKIE_PATH,
    FULL_COOKIE,
    FULL_MODE_TTL,
    cookie_secure,
    full_mode_configured,
    issue_token,
    key_matches,
    verify_token,
)

router = APIRouter(prefix="/api", tags=["full_mode"])


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    """What the UI needs to render the mode picker: is demo available, is full configured
    server-side, and is THIS browser session unlocked for full (valid capability cookie)."""
    configured = full_mode_configured()
    return {
        "demo_available": True,
        "full_configured": configured,
        "full_unlocked": configured and verify_token(request.cookies.get(FULL_COOKIE)),
    }


class UnlockRequest(BaseModel):
    key: str


@router.post("/full-mode/unlock")
def unlock(body: UnlockRequest, response: Response) -> dict:
    """Validate the full-mode key once; on success set the signed capability cookie.

    403 if full mode isn't configured on this server (fail closed); 401 on a wrong key
    (no cookie set, the user stays in demo)."""
    if not full_mode_configured():
        raise HTTPException(status_code=403, detail="full mode is not available on this server")
    if not key_matches(body.key):
        raise HTTPException(status_code=401, detail="incorrect full-mode key")
    response.set_cookie(
        FULL_COOKIE, issue_token(), max_age=FULL_MODE_TTL, httponly=True,
        samesite="lax", secure=cookie_secure(), path=COOKIE_PATH,
    )
    return {"unlocked": True}


@router.post("/full-mode/lock")
def lock(response: Response) -> dict:
    """Clear the capability cookie — re-lock full mode for this browser."""
    response.delete_cookie(FULL_COOKIE, path=COOKIE_PATH)
    return {"unlocked": False}
