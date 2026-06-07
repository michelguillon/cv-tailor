"""api/security.py — the Full Mode Unlock Gate's capability token (SPEC §12.7, D-38).

Full (Sonnet) mode is expensive, so on a public deployment it's gated behind a one-time
unlock: the human submits `FULL_MODE_KEY` once, and the backend issues a signed capability
cookie that proves "unlocked until <exp>". Full runs are then gated on the cookie — the raw
key never lives in the browser and isn't re-sent per run.

The token is signed with stdlib HMAC-SHA256 (no dependency) using `FULL_MODE_KEY` itself as
the secret: the cookie carries only a signature, never the key, and rotating the key
invalidates every outstanding cookie. Everything **fails closed** — no key configured, or a
missing/tampered/expired token, means "not unlocked". The backend is the source of truth
(`api/routers/runs.py` enforces it); UI hiding is convenience only.

The CLI is unaffected: it has no browser/cookie and keeps `--key` (config.resolve_run_config).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

__all__ = [
    "FULL_COOKIE", "FULL_MODE_TTL", "cookie_secure", "full_mode_configured",
    "key_matches", "issue_token", "verify_token",
]

FULL_COOKIE = "cv_full_mode"
FULL_MODE_TTL = 7 * 24 * 3600          # 7 days — owner convenience vs. exposure window
COOKIE_PATH = "/api"


def _key() -> str:
    """The full-mode key from the environment (the signing secret), or '' if unset."""
    return os.environ.get("FULL_MODE_KEY", "")


def full_mode_configured() -> bool:
    """True when the server has a FULL_MODE_KEY — i.e. full mode can be unlocked at all."""
    return bool(_key())


def cookie_secure() -> bool:
    """Whether to mark the capability cookie Secure. Off by default (localhost http);
    set COOKIE_SECURE=true in prod (the browser↔Cloudflare leg is HTTPS)."""
    return os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


def key_matches(candidate: str | None) -> bool:
    """Constant-time check of a submitted unlock key against FULL_MODE_KEY."""
    key = _key()
    if not key or not candidate:
        return False
    return hmac.compare_digest(candidate, key)


def _sign(exp: int, secret: str) -> str:
    sig = hmac.new(secret.encode(), str(exp).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def issue_token(*, now: int | None = None) -> str:
    """Mint a capability token `"<exp>.<b64sig>"`. Caller must have verified the key first
    (full mode configured); raises if it isn't, so a token is never minted unsigned."""
    secret = _key()
    if not secret:
        raise RuntimeError("cannot issue a full-mode token: FULL_MODE_KEY is not set")
    exp = (int(time.time()) if now is None else now) + FULL_MODE_TTL
    return f"{exp}.{_sign(exp, secret)}"


def verify_token(token: str | None, *, now: int | None = None) -> bool:
    """True iff `token` is a well-formed, unexpired capability token signed with the current
    FULL_MODE_KEY. Fails closed on anything off (no key, malformed, expired, bad signature)."""
    secret = _key()
    if not secret or not token or "." not in token:
        return False
    exp_str, _, sig = token.partition(".")
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < (int(time.time()) if now is None else now):
        return False
    return hmac.compare_digest(sig, _sign(exp, secret))
