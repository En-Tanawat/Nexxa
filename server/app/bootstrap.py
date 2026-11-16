"""Create the first admin from env vars at startup (for hosts without a shell)."""
import logging
import os
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.create_admin import MIN_PASSWORD_LEN, create_or_update
from app.models import AdminUser

log = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,64}")


def bootstrap_first_admin(session: Session) -> str | None:
    """Return the created username, or None. Never raises on bad input; never logs the password."""
    username = os.getenv("NEXXA_ADMIN_USER", "").strip()
    password = os.getenv("NEXXA_ADMIN_PASSWORD", "")
    if not username or not password:
        return None
    if session.scalar(select(AdminUser.id).limit(1)) is not None:
        log.info("admin user already exists; remove NEXXA_ADMIN_PASSWORD from the environment")
        return None
    if not USERNAME_RE.fullmatch(username):
        log.warning("NEXXA_ADMIN_USER invalid: use 3-64 characters from letters, digits, '.', '_', '-'")
        return None
    if len(password) < MIN_PASSWORD_LEN:
        log.warning("NEXXA_ADMIN_PASSWORD invalid: password too short (min %d)", MIN_PASSWORD_LEN)
        return None
    try:
        create_or_update(session, username, password)
    except IntegrityError:  # another worker created it first
        session.rollback()
        return None
    log.info("created first admin user %s", username)
    return username
