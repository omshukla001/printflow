"""Consumable stock: paper sheets and toner pages.

Two things happen here. Stock **levels** go down as jobs print and back up when
the admin restocks, with every change written to `StockMovement` so the history
survives. And because each item carries a `unit_cost`, the same numbers give the
**cost of goods** for a job — what a print actually cost the shop, next to what
the customer was charged.

Units are the ones things are bought and consumed in:

* paper — **sheets**. A duplex job puts two pages on one sheet, so it uses half
  the paper of the same job printed one-sided.
* toner — **pages of yield**, the figure printed on a cartridge box. Both sides
  of a duplex sheet consume toner, so toner is counted in *impressions*, not
  sheets. This is an estimate: real coverage varies with what is on the page.
"""
import logging

from flask import current_app

from app.extensions import db
from app.models import Alert, StockItem, StockMovement, utcnow

log = logging.getLogger(__name__)

DEFAULT_LOW_THRESHOLD = {'paper': 100, 'toner': 200}


# --- items ----------------------------------------------------------------

def get_item(kind, key, printer_id=None, create=False):
    """Find a stock row, optionally creating it on first use."""
    item = StockItem.query.filter_by(kind=kind, key=key, printer_id=printer_id).first()
    if item is None and printer_id is not None:
        # Fall back to the shop-wide row when a printer has no stock of its own.
        item = StockItem.query.filter_by(kind=kind, key=key, printer_id=None).first()
    if item is None and create:
        item = StockItem(kind=kind, key=key, printer_id=printer_id, quantity=0,
                         low_threshold=DEFAULT_LOW_THRESHOLD.get(kind, 100),
                         unit_cost=0.0)
        db.session.add(item)
        db.session.commit()
    return item


def all_items():
    return StockItem.query.order_by(StockItem.kind.asc(), StockItem.key.asc()).all()


def seed_defaults():
    """Create empty rows for the sizes and modes on offer. Idempotent."""
    created = 0
    try:
        sizes = current_app.config.get('PAPER_SIZES') or ['A4']
    except RuntimeError:
        sizes = ['A4']
    for size in sizes:
        if get_item('paper', size) is None:
            get_item('paper', size, create=True)
            created += 1
    for mode in ('bw', 'color'):
        if get_item('toner', mode) is None:
            get_item('toner', mode, create=True)
            created += 1
    return created


# --- movements ------------------------------------------------------------

def record(item, change, reason, actor_id=None, job_id=None, note=None, commit=True):
    """Apply a change to a stock level and log it. Returns the movement."""
    item.quantity = (item.quantity or 0) + change
    item.updated_at = utcnow()
    movement = StockMovement(
        stock_item_id=item.id,
        change=change,
        balance_after=item.quantity,
        reason=reason,
        job_id=job_id,
        actor_id=actor_id,
        note=(note or None),
        unit_cost=item.unit_cost or 0.0,
    )
    db.session.add(movement)
    if commit:
        db.session.commit()
    return movement


def restock(item, amount, actor_id=None, note=None):
    """Add stock. Amount must be positive."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError('Restock amount must be greater than zero.')
    movement = record(item, amount, 'restock', actor_id=actor_id, note=note)
    check_item(item)
    return movement


def adjust(item, new_quantity, actor_id=None, note=None):
    """Set the level to a counted figure — a stocktake, or correcting a mistake."""
    new_quantity = max(0, int(new_quantity))
    delta = new_quantity - (item.quantity or 0)
    if delta == 0:
        return None
    movement = record(item, delta, 'adjust', actor_id=actor_id, note=note)
    check_item(item)
    return movement


# --- consumption ----------------------------------------------------------

def usage_for_job(job):
    """(sheets of paper, toner impressions) a job consumes."""
    from app.services.print_options import effective_pages

    pages = effective_pages(job) * (job.copies or 1)
    nup = job.pages_per_sheet or 1
    impressions = (pages + nup - 1) // nup   # one imaged side per N-up group
    sheets = impressions
    if (job.sides or 'one-sided') == 'two-sided':
        sheets = (impressions + 1) // 2      # two imaged sides share a sheet
    return sheets, impressions


def estimate_cost(job, printer_id=None):
    """What this job costs the shop in consumables, without changing anything."""
    sheets, impressions = usage_for_job(job)
    paper = get_item('paper', job.paper_size or 'A4', printer_id)
    toner = get_item('toner', job.color_mode or 'bw', printer_id)
    paper_cost = round(sheets * (paper.unit_cost if paper else 0.0), 4)
    ink_cost = round(impressions * (toner.unit_cost if toner else 0.0), 4)
    return {
        'sheets': sheets,
        'impressions': impressions,
        'paper_cost': round(paper_cost, 2),
        'ink_cost': round(ink_cost, 2),
        'total': round(paper_cost + ink_cost, 2),
        'paper_item': paper,
        'toner_item': toner,
    }


def consume_for_job(job, printer_id=None, commit=True):
    """Deduct what a finished job used and stamp its cost onto the job.

    Deliberately allows a level to go negative: the paper physically went
    through the printer whether or not the count was up to date, and a negative
    balance is a far louder signal to the admin than silently clamping at zero.
    """
    if (job.sheets_used or 0) > 0:
        return None  # already accounted for; complete_job is idempotent

    printer_id = printer_id or job.printer_id or job.claimed_by_agent
    estimate = estimate_cost(job, printer_id)

    paper = estimate['paper_item'] or get_item('paper', job.paper_size or 'A4', create=True)
    toner = estimate['toner_item'] or get_item('toner', job.color_mode or 'bw', create=True)

    if estimate['sheets']:
        record(paper, -estimate['sheets'], 'print', job_id=job.id, commit=False)
    if estimate['impressions']:
        record(toner, -estimate['impressions'], 'print', job_id=job.id, commit=False)

    job.sheets_used = estimate['sheets']
    job.impressions_used = estimate['impressions']
    job.paper_cost = estimate['paper_cost']
    job.ink_cost = estimate['ink_cost']

    if commit:
        db.session.commit()

    check_item(paper)
    check_item(toner)
    return estimate


# --- alerts ---------------------------------------------------------------

def raise_alert(kind, subject, title, message=None, severity='warning'):
    """Open an alert, or leave the existing one alone if it is already open."""
    existing = Alert.query.filter_by(subject=subject, is_active=True).first()
    if existing is not None:
        if existing.kind != kind or existing.severity != severity:
            # Condition got worse (low → out) or better; keep one row, update it.
            existing.kind = kind
            existing.severity = severity
            existing.title = title
            existing.message = message
            db.session.commit()
        return existing

    alert = Alert(kind=kind, subject=subject, title=title, message=message,
                  severity=severity, is_active=True)
    db.session.add(alert)
    db.session.commit()
    log.warning('Alert raised: %s — %s', subject, title)
    return alert


def resolve_alert(subject, note=None):
    """Close any open alert for this subject. Returns how many were closed."""
    open_alerts = Alert.query.filter_by(subject=subject, is_active=True).all()
    for alert in open_alerts:
        alert.is_active = False
        alert.resolved_at = utcnow()
        alert.resolved_note = note
    if open_alerts:
        db.session.commit()
        log.info('Alert cleared: %s', subject)
    return len(open_alerts)


def item_subject(item):
    """Stable identifier for an item's alerts."""
    scope = item.printer_id or 'shop'
    return f'stock:{item.kind}:{item.key}:{scope}'


def check_item(item):
    """Raise, escalate or clear the low-stock alert for one item."""
    subject = item_subject(item)
    if item.is_out:
        return raise_alert(
            f'{item.kind}_out', subject,
            f'Out of {item.label}',
            f'{item.label} is at {item.quantity} {item.unit_name}. Printing will '
            f'fail or the count is wrong — restock and check.',
            severity='critical')
    if item.is_low:
        return raise_alert(
            f'{item.kind}_low', subject,
            f'Low on {item.label}',
            f'{item.quantity} {item.unit_name} left (warning below '
            f'{item.low_threshold}).',
            severity='warning')
    resolve_alert(subject, note=f'Back to {item.quantity} {item.unit_name}')
    return None


def check_all():
    """Re-evaluate every item. Returns the number of active stock alerts."""
    for item in all_items():
        check_item(item)
    return Alert.query.filter(Alert.is_active.is_(True),
                              Alert.kind.notin_(('kiosk_offline',))).count()


def active_alerts():
    return Alert.query.filter_by(is_active=True).order_by(
        Alert.severity.desc(), Alert.created_at.desc()).all()


# --- reporting ------------------------------------------------------------

def cost_summary(since=None):
    """Revenue, cost of goods and margin over completed jobs."""
    from app.models import PrintJob

    q = PrintJob.query.filter(PrintJob.status == 'completed')
    if since is not None:
        q = q.filter(PrintJob.printed_at >= since)

    revenue = paper = ink = sheets = impressions = 0.0
    jobs = 0
    for job in q.all():
        jobs += 1
        revenue += job.cost or 0.0
        paper += job.paper_cost or 0.0
        ink += job.ink_cost or 0.0
        sheets += job.sheets_used or 0
        impressions += job.impressions_used or 0

    cost = paper + ink
    return {
        'jobs': jobs,
        'revenue': round(revenue, 2),
        'paper_cost': round(paper, 2),
        'ink_cost': round(ink, 2),
        'cost': round(cost, 2),
        'margin': round(revenue - cost, 2),
        'sheets': int(sheets),
        'impressions': int(impressions),
        'cost_per_sheet': round(cost / sheets, 3) if sheets else 0.0,
        'revenue_per_sheet': round(revenue / sheets, 3) if sheets else 0.0,
        'margin_per_sheet': round((revenue - cost) / sheets, 3) if sheets else 0.0,
    }
