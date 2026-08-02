"""Audit log helper — records admin actions for compliance and forensics."""
import json
from flask import request, has_request_context
from flask_login import current_user
from app.extensions import db
from app.models import AuditLog


def record(action, target_type=None, target_id=None, details=None):
    """Record an audit log entry. Safe to call even if outside a request."""
    actor_id = None
    actor_username = None
    ip = None

    if has_request_context():
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr) or '').split(',')[0].strip()
        try:
            if current_user.is_authenticated:
                actor_id = current_user.id
                actor_username = current_user.username
        except Exception:
            pass

    if details is not None and not isinstance(details, str):
        try:
            details = json.dumps(details, default=str)
        except Exception:
            details = str(details)

    entry = AuditLog(
        actor_id=actor_id,
        actor_username=actor_username,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details,
        ip=ip,
    )
    db.session.add(entry)
    # caller commits — audit always flushed in same txn as the action
