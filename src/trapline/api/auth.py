"""Zabezpečení GUI jedním sdíleným heslem.

Kuchařka drží hash v DB; Trapline zatím žádnou nepoužívá, takže heslo přijde
z prostředí (``APP_PASSWORD``). Po přihlášení dostane klient podepsaný token
(HMAC + expirace) v HttpOnly cookie.

Podpisový klíč se **odvozuje z hesla**, ne generuje náhodně. Náhodný klíč by
se při každém startu měnil — a self-update restartuje proces, takže by tě
každá aktualizace odhlásila. Takhle relace restart přežije a změna hesla
naopak všechny staré tokeny zneplatní.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import settings

log = logging.getLogger("trapline.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "trapline_auth"
TOKEN_DAYS = 90  # domácí appka, „zůstat přihlášen" je záměr

#: Po kolika neúspěších se z jedné IP na chvíli přestane brát heslo.
MAX_FAILS = 10
LOCKOUT_S = 300.0

#: ip → (počet neúspěchů, čas do kdy je zámek)
_fails: dict[str, tuple[int, float]] = {}


class LoginRequest(BaseModel):
    password: str


def _secret() -> str:
    if settings.auth_secret:
        return settings.auth_secret
    return hmac.new(
        b"trapline-auth-v1", settings.app_password.encode(), hashlib.sha256
    ).hexdigest()


def verify_password(password: str) -> bool:
    if not settings.auth_enabled:
        return True
    return hmac.compare_digest(password, settings.app_password)


def make_token(days: int = TOKEN_DAYS) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + days * 86400}).encode()
    ).decode()
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        payload, sig = token.split(".")
        expect = hmac.new(
            _secret().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0)) > time.time()
    except Exception:  # noqa: BLE001
        return False


def token_from_request(request: Request) -> str | None:
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def is_authenticated(request: Request) -> bool:
    return not settings.auth_enabled or valid_token(token_from_request(request))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _locked(ip: str) -> bool:
    fails, until = _fails.get(ip, (0, 0.0))
    return fails >= MAX_FAILS and time.time() < until


def _record_fail(ip: str) -> None:
    fails, _ = _fails.get(ip, (0, 0.0))
    _fails[ip] = (fails + 1, time.time() + LOCKOUT_S)


@router.get("/status")
def status(request: Request):
    """GUI se podle tohohle rozhodne, jestli ukázat přihlášení, nebo obsah."""
    return {
        "required": settings.auth_enabled,
        "authenticated": is_authenticated(request),
    }


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    if not settings.auth_enabled:
        return {"ok": True, "required": False}

    ip = _client_ip(request)
    if _locked(ip):
        raise HTTPException(429, "Příliš mnoho pokusů. Zkus to za pár minut.")

    if not verify_password(req.password):
        _record_fail(ip)
        log.warning("Neúspěšné přihlášení z %s", ip)
        raise HTTPException(401, "Špatné heslo.")

    _fails.pop(ip, None)
    response.set_cookie(
        COOKIE_NAME,
        make_token(),
        max_age=TOKEN_DAYS * 86400,
        httponly=True,  # JS na token nevidí, XSS nemá co ukrást
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )
    return {"ok": True, "required": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
