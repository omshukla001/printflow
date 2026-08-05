"""Queue management and CUPS status monitoring."""
import logging
import threading
import time
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import or_, func

from app.extensions import db
from app.models import PrintJob, PaymentLedger
from app.services.retention import purge_job_files

log = logging.getLogger(__name__)


TERMINAL_STATES = ('completed', 'failed', 'cancelled')


def _release_voucher(job):
    """Give a discount voucher back when its job will never print."""
    try:
        from app.services import offers
        offers.release_voucher(job, commit=False)
    except Exception as e:
        log.warning('Could not release voucher for job #%s: %s', job.id, e)


def _maybe_purge(job, reason):
    """Drop the job's file now if retention is set to immediate.

    With FILE_RETENTION_MINUTES > 0 the file is left for the watchdog sweep so
    reprint keeps working during the grace period. Never let a cleanup problem
    surface as a failed status report — the print itself already succeeded.
    """
    try:
        if not current_app.config.get('PURGE_AFTER_PRINT', True):
            return
        if current_app.config.get('FILE_RETENTION_MINUTES', 0) > 0:
            return
        purge_job_files(job, reason=reason)
    except Exception as e:
        log.warning('Post-%s purge failed for job #%s: %s', reason, job.id, e)


def get_ordered_queue(printer_id=None):
    """Get print jobs in priority order.

    Order:
    1. Admin manually positioned (queue_position IS NOT NULL) — by queue_position ASC
    2. Currently printing
    3. Ready to print
    4. Prioritized (QR scanned) — by scanned_at ASC
    5. Regular queued — FIFO by submitted_at ASC
    """
    active_statuses = ['queued', 'prioritized', 'ready_to_print', 'printing']

    base_filter = [PrintJob.status.in_(active_statuses)]
    if printer_id is not None:
        base_filter.append(or_(PrintJob.printer_id == printer_id, PrintJob.printer_id.is_(None)))

    manual = PrintJob.query.filter(
        *base_filter,
        PrintJob.queue_position.isnot(None),
    ).order_by(PrintJob.queue_position.asc()).all()

    prioritized = PrintJob.query.filter(
        *base_filter,
        PrintJob.status == 'prioritized',
        PrintJob.queue_position.is_(None),
    ).order_by(PrintJob.scanned_at.asc(), PrintJob.id.asc()).all()

    regular = PrintJob.query.filter(
        *base_filter,
        PrintJob.status == 'queued',
        PrintJob.queue_position.is_(None),
    ).order_by(PrintJob.submitted_at.asc(), PrintJob.id.asc()).all()

    ready = PrintJob.query.filter(
        *base_filter,
        PrintJob.status == 'ready_to_print',
        PrintJob.queue_position.is_(None),
    ).order_by(PrintJob.submitted_at.asc()).all()

    printing = PrintJob.query.filter(
        *base_filter,
        PrintJob.status == 'printing',
        PrintJob.queue_position.is_(None),
    ).all()

    seen = set()
    ordered = []
    for job in manual + printing + ready + prioritized + regular:
        if job.id not in seen:
            seen.add(job.id)
            ordered.append(job)
    return ordered


def get_next_job():
    """Get the next job to print (first in priority order that is not already printing)."""
    queue = get_ordered_queue()
    for job in queue:
        if job.status in ('queued', 'prioritized', 'ready_to_print'):
            return job
    return None


def get_user_queue_position(user_id):
    """Get the queue position for a user's first pending job."""
    queue = get_ordered_queue()
    for i, job in enumerate(queue):
        if job.user_id == user_id and job.status in ('queued', 'prioritized'):
            return i + 1
    return None


def prioritize_user_jobs(user_id, scanned_at=None):
    """Bump all queued jobs for a user to prioritized status."""
    if scanned_at is None:
        scanned_at = datetime.now(timezone.utc)

    jobs = PrintJob.query.filter_by(user_id=user_id, status='queued').all()
    count = 0
    for job in jobs:
        job.status = 'prioritized'
        job.scanned_at = scanned_at
        job.last_status_at = scanned_at
        count += 1

    if count > 0:
        db.session.commit()
    return count


def claim_pending_jobs(printer_id, agent_id, limit=10):
    """Atomically claim ready_to_print jobs for an agent.

    Returns the list of claimed PrintJob rows. Two agents racing on the same
    queue won't both grab the same job because we re-check the status under a
    row-level lock.
    """
    now = datetime.now(timezone.utc)
    q = PrintJob.query.filter(
        PrintJob.status == 'ready_to_print',
    )
    if printer_id is not None:
        q = q.filter(or_(PrintJob.printer_id == printer_id, PrintJob.printer_id.is_(None)))
    q = q.order_by(PrintJob.submitted_at.asc()).limit(limit)

    candidates = q.all()
    claimed = []
    for job in candidates:
        # Atomic update: only claim if still ready_to_print and unclaimed
        result = db.session.query(PrintJob).filter(
            PrintJob.id == job.id,
            PrintJob.status == 'ready_to_print',
            PrintJob.claimed_by_agent.is_(None),
        ).update({
            'claimed_by_agent': agent_id,
            'claimed_at': now,
            'last_status_at': now,
        }, synchronize_session=False)
        if result == 1:
            db.session.commit()
            db.session.refresh(job)
            claimed.append(job)
        else:
            db.session.rollback()
    return claimed


def mark_printing(job, cups_job_id):
    """Idempotent transition ready_to_print → printing.

    Returns True if the transition happened, False if the job was already past
    that state (don't treat that as an error from the agent's perspective).
    """
    now = datetime.now(timezone.utc)
    result = db.session.query(PrintJob).filter(
        PrintJob.id == job.id,
        PrintJob.status.in_(('ready_to_print', 'printing')),
    ).update({
        'status': 'printing',
        'cups_job_id': cups_job_id,
        'last_status_at': now,
        # COALESCE, not `now`: this transition is idempotent and the agent may
        # report "printing" more than once. Overwriting would restart the
        # customer's countdown every time it did.
        'printing_started_at': func.coalesce(PrintJob.printing_started_at, now),
    }, synchronize_session=False)
    db.session.commit()
    return result == 1


def complete_job(job):
    """Mark a job as completed and charge the user. Idempotent."""
    if job.status == 'completed':
        return False
    now = datetime.now(timezone.utc)
    job.status = 'completed'
    job.printed_at = now
    job.last_status_at = now
    job.queue_position = None

    # Charge user — only if cost not already applied
    user = job.user
    if user and job.cost > 0:
        existing_charge = PaymentLedger.query.filter_by(
            job_id=job.id, entry_type='charge'
        ).first()
        if existing_charge is None:
            user.balance_owed = (user.balance_owed or 0) + job.cost
            ledger = PaymentLedger(
                user_id=user.id,
                amount=job.cost,
                description=f'Print: {job.filename} ({job.page_count}p x{job.copies})',
                job_id=job.id,
                entry_type='charge',
            )
            db.session.add(ledger)

    db.session.commit()

    # The paper and toner are physically gone — deduct them and stamp what the
    # job cost the shop. Never let an accounting problem undo a finished print.
    try:
        from app.services import stock
        stock.consume_for_job(job)
    except Exception as e:
        log.warning('Stock accounting failed for job #%s: %s', job.id, e)

    # A completed print is what makes a referral "successful" — pay the inviter.
    try:
        from app.services import offers
        offers.qualify_referral(job)
    except Exception as e:
        log.warning('Referral qualification failed for job #%s: %s', job.id, e)

    # The agent only reports 'completed' after CUPS finished with the file it
    # already downloaded, so the server copy has done its job.
    _maybe_purge(job, 'completed')
    return True


def fail_job(job, error_message='Print failed', refund=True):
    """Mark a job as failed. If the user was already charged, reverse the charge."""
    if job.status == 'failed':
        return False
    now = datetime.now(timezone.utc)
    job.status = 'failed'
    job.error_message = error_message
    job.failed_reason = (error_message or '')[:500]
    job.last_status_at = now
    job.queue_position = None

    if refund:
        existing_charge = PaymentLedger.query.filter_by(
            job_id=job.id, entry_type='charge'
        ).first()
        already_refunded = PaymentLedger.query.filter_by(
            job_id=job.id, entry_type='refund'
        ).first()
        if existing_charge and not already_refunded:
            user = job.user
            user.balance_owed = (user.balance_owed or 0) - existing_charge.amount
            db.session.add(PaymentLedger(
                user_id=user.id,
                amount=-existing_charge.amount,
                description=f'Refund (failed): {job.filename}',
                job_id=job.id,
                entry_type='refund',
            ))

    _release_voucher(job)
    db.session.commit()
    return True


def cancel_job(job, by_user_id=None, refund=True):
    """Cancel a queued or pre-print job."""
    if job.status in TERMINAL_STATES:
        return False
    job.status = 'cancelled'
    job.queue_position = None
    job.last_status_at = datetime.now(timezone.utc)

    if refund:
        existing_charge = PaymentLedger.query.filter_by(
            job_id=job.id, entry_type='charge'
        ).first()
        already_refunded = PaymentLedger.query.filter_by(
            job_id=job.id, entry_type='refund'
        ).first()
        if existing_charge and not already_refunded:
            user = job.user
            user.balance_owed = (user.balance_owed or 0) - existing_charge.amount
            db.session.add(PaymentLedger(
                user_id=user.id,
                amount=-existing_charge.amount,
                description=f'Refund (cancelled): {job.filename}',
                job_id=job.id,
                entry_type='refund',
            ))

    _release_voucher(job)
    db.session.commit()

    # A cancelled job will never be printed — nothing left to keep the file for.
    _maybe_purge(job, 'cancelled')
    return True


def get_user_active_count(user_id):
    """Number of jobs in non-terminal states for a user."""
    return PrintJob.query.filter(
        PrintJob.user_id == user_id,
        PrintJob.status.in_(('queued', 'prioritized', 'ready_to_print', 'printing')),
    ).count()


# --- CUPS monitor (local mode only) ---

def _monitor_cups_jobs(app):
    """Background thread to poll CUPS for job status updates."""
    with app.app_context():
        while True:
            try:
                time.sleep(app.config.get('CUPS_POLL_INTERVAL', 5))
                _check_printing_jobs(app)
            except Exception:
                pass


def _check_printing_jobs(app):
    """Check all 'printing' jobs against CUPS. Maps CUPS state to job state."""
    from app.services.printer import get_job_status

    with app.app_context():
        printing_jobs = PrintJob.query.filter_by(status='printing').all()
        for job in printing_jobs:
            if job.cups_job_id is None:
                continue
            try:
                cups_status = get_job_status(job.cups_job_id)
                if cups_status == 'completed':
                    complete_job(job)
                elif cups_status == 'cancelled':
                    fail_job(job, 'CUPS reports cancelled')
                elif cups_status in ('failed', 'aborted'):
                    fail_job(job, f'CUPS status: {cups_status}')
                # stopped / held / queued / printing: leave alone, watchdog handles timeouts
            except Exception:
                pass


def start_cups_monitor(app):
    """Start the background CUPS monitoring thread."""
    thread = threading.Thread(target=_monitor_cups_jobs, args=(app,), daemon=True)
    thread.start()
