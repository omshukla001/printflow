"""Kiosk QR system — one-time tokens displayed on monitor, scanned by users.

Tokens have a TTL (configurable, default 90s) and are consumed atomically so
two phones racing on the same QR can't both succeed. The state lives in a DB
singleton row so multiple Gunicorn workers see the same view.
"""
import uuid
from datetime import datetime, timezone, timedelta
from flask import current_app
from sqlalchemy import and_

from app.extensions import db
from app.models import KioskState


def _now():
    return datetime.now(timezone.utc)


def _ttl():
    try:
        return current_app.config.get('KIOSK_TOKEN_TTL', 90)
    except RuntimeError:
        return 90


def _ensure_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _get_state():
    """Get or create the singleton KioskState row."""
    state = db.session.get(KioskState, 1)
    if state is None:
        now = _now()
        state = KioskState(
            id=1,
            current_token=str(uuid.uuid4()),
            created_at=now,
            token_expires_at=now + timedelta(seconds=_ttl()),
        )
        db.session.add(state)
        db.session.commit()
    return state


def generate_new_token():
    """Generate a fresh kiosk token and reset scan state."""
    state = _get_state()
    now = _now()
    state.current_token = str(uuid.uuid4())
    state.created_at = now
    state.token_expires_at = now + timedelta(seconds=_ttl())
    state.scanned_by = None
    state.scanned_username = None
    state.jobs_count = 0
    state.scanned_at = None
    state.next_token = None
    db.session.commit()
    return state.current_token


def get_current_token():
    """Get the current kiosk token, regenerating if expired."""
    state = _get_state()
    now = _now()
    expires = _ensure_aware(state.token_expires_at)
    if state.current_token is None or (expires and expires < now and state.scanned_by is None):
        state.current_token = str(uuid.uuid4())
        state.created_at = now
        state.token_expires_at = now + timedelta(seconds=_ttl())
        state.scanned_by = None
        state.scanned_username = None
        state.jobs_count = 0
        state.scanned_at = None
        state.next_token = None
        db.session.commit()
    return state.current_token


def _is_valid(state, token):
    expires = _ensure_aware(state.token_expires_at)
    return bool(
        state.current_token
        and token == state.current_token
        and state.scanned_by is None
        and (expires is None or expires > _now())
    )


def peek_token(token):
    """Check if a token matches current and hasn't been consumed, without consuming it."""
    state = _get_state()
    return _is_valid(state, token)


def validate_and_consume(token):
    """Atomically check if token matches current and mark it consumed.

    Uses a conditional UPDATE so two workers racing can't both win.
    Returns True if valid (first scan wins), False otherwise.
    """
    state = _get_state()
    if not _is_valid(state, token):
        return False

    now = _now()
    # Atomic: only consume if still unscanned + token unchanged + not expired
    result = db.session.query(KioskState).filter(
        and_(
            KioskState.id == 1,
            KioskState.current_token == token,
            KioskState.scanned_by.is_(None),
        )
    ).update({
        'scanned_by': '__consumed__',
        'scanned_at': now,
    }, synchronize_session=False)
    db.session.commit()
    return result == 1


def mark_scanned(user_full_name, username, jobs_count):
    """Mark the current token as scanned AND pre-generate the next token."""
    state = _get_state()
    now = _now()
    state.scanned_by = user_full_name
    state.scanned_username = username
    state.jobs_count = jobs_count
    state.scanned_at = now
    state.next_token = str(uuid.uuid4())
    db.session.commit()


def get_status():
    """Get the current kiosk state for polling."""
    state = _get_state()
    expires = _ensure_aware(state.token_expires_at)
    result = {
        'token': state.current_token,
        'scanned_by': state.scanned_by,
        'scanned_username': state.scanned_username,
        'jobs_prioritized': state.jobs_count or 0,
        'is_scanned': (state.scanned_by is not None
                       and state.scanned_by != '__consumed__'),
        'token_expires_at': expires.isoformat() if expires else None,
    }
    if state.next_token:
        result['new_token'] = state.next_token
    return result


def recent_completions(window_seconds=120, limit=5):
    """Jobs finished recently, for the kiosk's "collect your papers" screen.

    `ready_in_seconds` is how long until the paper is physically out, which is
    not the same as the job being marked completed. The agent reports
    completion from the CUPS job state, and CUPS closes a job once the data
    reaches the printer's buffer — on a USB laser the sheets keep coming for
    some time afterwards. Telling someone to collect their papers at that
    moment sends them to a printer that is still running, so the display waits
    for the estimate to run out instead.
    """
    from app.models import PrintJob, User
    from app.services import print_options, print_timing

    cutoff = _now() - timedelta(seconds=window_seconds)
    jobs = (PrintJob.query
            .filter(PrintJob.status == 'completed', PrintJob.printed_at >= cutoff)
            .order_by(PrintJob.printed_at.desc())
            .limit(limit)
            .all())
    if not jobs:
        return []

    names = {u.id: u.full_name for u in
             User.query.filter(User.id.in_({j.user_id for j in jobs})).all()}
    return [{
        'id': j.id,
        'user': names.get(j.user_id) or 'Customer',
        'filename': j.filename,
        'sheets': print_options.effective_sheets(j),
        'ready_in_seconds': round(print_timing.remaining_seconds(j)),
    } for j in jobs]


def activate_next_token():
    """Promote the pre-generated next token to current, reset scan state."""
    state = _get_state()
    now = _now()
    if state.next_token:
        state.current_token = state.next_token
        state.created_at = now
        state.token_expires_at = now + timedelta(seconds=_ttl())
        state.scanned_by = None
        state.scanned_username = None
        state.jobs_count = 0
        state.scanned_at = None
        state.next_token = None
        db.session.commit()
        return state.current_token
    # No prepared next token — just generate one
    return generate_new_token()


def reset_state():
    """Admin: clear scan state and rotate token."""
    return generate_new_token()
