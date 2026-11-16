from sqlalchemy.orm import Session

from app.models import AuditLog


def record(session: Session, actor: str, action: str, target: str, detail: str | None = None) -> None:
    """Queue an audit row in the caller's transaction (commit together with the change). Never put secrets in detail."""
    session.add(AuditLog(actor=actor, action=action, target=target, detail=detail))
