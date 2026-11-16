"""Create or update a dashboard admin: python -m app.create_admin --username U"""
import argparse
import getpass
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdminUser
from app.web.security import hash_password

MIN_PASSWORD_LEN = 10


def create_or_update(session: Session, username: str, password: str, role: str = "admin") -> AdminUser:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    user = session.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None:
        user = AdminUser(username=username, password_hash=hash_password(password), role=role)
        session.add(user)
    else:
        user.password_hash = hash_password(password)
        user.role = role
    session.commit()
    return user


def main() -> int:
    from app.db import engine, init_db  # lazy: importing this module must not touch the DB

    ap = argparse.ArgumentParser(description="Create or update a NEXXA admin user")
    ap.add_argument("--username", required=True)
    ap.add_argument("--role", default="admin", choices=["admin", "agent", "viewer"], help="Role: admin, agent, or viewer")
    args = ap.parse_args()
    password = os.getenv("NEXXA_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    init_db()
    with Session(engine) as s:
        try:
            user = create_or_update(s, args.username, password, role=args.role)
        except ValueError as e:
            print(f"error: {e}")
            return 1
        print(f"user '{user.username}' saved (id {user.id}, role {user.role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
