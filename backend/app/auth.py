"""Who is asking. Local use has one profile; a public deployment gives each browser its own space.

Guest spaces: the browser holds a random token in an httpOnly cookie; the database stores only
its SHA-256. There are no passwords and no email addresses. Reading never creates a space (a
visitor without a cookie just sees empty views); the first write does. All data is reached
through the profile id, and every route checks ownership, so one visitor cannot see another's.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, db
from .limits import client_ip, windows
from .models import Profile, utcnow

NO_PROFILE = 0  # a visitor who has not created a space yet: owns nothing
COOKIE_DAYS = 90


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def current_profile(request: Request, response: Response, s: Annotated[Session, Depends(db.get_session)]) -> int:
    if config.AUTH_MODE != "guest":
        return db.DEFAULT_PROFILE_ID
    token = request.cookies.get(config.COOKIE_NAME)
    if token and 20 <= len(token) <= 100:
        p = s.scalar(select(Profile).where(Profile.token_hash == hash_token(token)))
        if p is not None:
            now = utcnow()
            if p.last_seen is None or now - p.last_seen > timedelta(minutes=5):
                p.last_seen = now
                s.commit()
            return p.id
    if request.method == "GET":
        return NO_PROFILE
    ok, retry = windows.allow(("guest", client_ip(request)), config.MAX_GUEST_PROFILES_PER_IP_PER_HOUR, 3600)
    if not ok:
        raise HTTPException(429, "Too many new spaces were started from this address. Try again later.")
    token = secrets.token_urlsafe(32)
    p = Profile(name="Guest", kind="guest", token_hash=hash_token(token), last_seen=utcnow())
    s.add(p)
    s.commit()
    response.set_cookie(config.COOKIE_NAME, token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")
    return p.id


def purge_idle_guests(s: Session) -> int:
    """Delete guest spaces nobody has opened for GUEST_RETENTION_DAYS, with everything in them."""
    cutoff = utcnow() - timedelta(days=config.GUEST_RETENTION_DAYS)
    ids = list(s.scalars(select(Profile.id).where(Profile.kind == "guest",
                                                  (Profile.last_seen < cutoff) | (Profile.last_seen.is_(None)))))
    for pid in ids:
        erase_profile(s, pid, drop_profile=True)
    return len(ids)


def erase_profile(s: Session, profile_id: int, drop_profile: bool) -> dict:
    """Delete everything a profile owns. Database foreign keys cascade to runs, evidence and notes."""
    from sqlalchemy import delete, func
    from . import models as m
    counts = {"games": s.scalar(select(func.count(m.Game.id)).where(m.Game.profile_id == profile_id)) or 0,
              "studies": s.scalar(select(func.count(m.Study.id)).where(m.Study.profile_id == profile_id)) or 0}
    for model in (m.Game, m.ImportRun, m.AnalysisBatch, m.SourceIdentity, m.Study, m.CoachReview, m.AnalysisJob,
                  m.AnalysisRun):
        s.execute(delete(model).where(model.profile_id == profile_id))
    if drop_profile:
        s.execute(delete(Profile).where(Profile.id == profile_id))
    s.commit()
    return counts
