import uuid
from datetime import datetime, timezone
from flask_login import UserMixin
from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


def as_aware(dt):
    """Treat a stored datetime as UTC.

    SQLite hands back naive datetimes, so anything read from the DB has to be
    stamped before it can be compared against `utcnow()`.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active_user = db.Column(db.Boolean, default=True)
    qr_token = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()))
    balance_owed = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=utcnow)

    # Lockout / security
    failed_login_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    password_changed_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)

    # Guest / walk-in
    is_guest = db.Column(db.Boolean, default=False)
    guest_expires_at = db.Column(db.DateTime, nullable=True)

    # Referral programme — the code this user shares with friends.
    referral_code = db.Column(db.String(16), unique=True, nullable=True, index=True)
    referred_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    jobs = db.relationship('PrintJob', backref='user', lazy='dynamic',
                           foreign_keys='PrintJob.user_id')
    ledger_entries = db.relationship('PaymentLedger', backref='user', lazy='dynamic',
                                    foreign_keys='PaymentLedger.user_id')

    @property
    def is_active(self):
        if not self.is_active_user:
            return False
        expires = as_aware(self.guest_expires_at)
        if self.is_guest and expires and expires < utcnow():
            return False
        return True

    @property
    def is_locked(self):
        locked_until = as_aware(self.locked_until)
        return bool(locked_until and locked_until > utcnow())


class PrintJob(db.Model):
    __tablename__ = 'print_jobs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    page_count = db.Column(db.Integer, default=1)
    copies = db.Column(db.Integer, default=1)
    color_mode = db.Column(db.String(10), default='bw')  # bw or color
    paper_size = db.Column(db.String(10), default='A4')  # A4, A3, Letter
    sides = db.Column(db.String(20), default='one-sided')  # one-sided or two-sided
    status = db.Column(db.String(20), default='queued', index=True)
    # statuses: queued, prioritized, ready_to_print, printing, completed, failed, cancelled
    queue_position = db.Column(db.Integer, nullable=True)  # manual admin override
    priority_score = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)          # what the user actually pays
    cost_locked = db.Column(db.Boolean, default=False)

    # Offers. `cost` is always the discounted figure so billing needs no
    # special-casing; these columns exist to show the customer what they saved.
    base_cost = db.Column(db.Float, default=0.0)     # before any discount
    discount_amount = db.Column(db.Float, default=0.0)
    discount_label = db.Column(db.String(200), nullable=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey('discount_vouchers.id'), nullable=True)

    # What the job cost the shop in consumables, stamped when it completes.
    # `cost` is what the customer pays; these are what it cost to serve them.
    paper_cost = db.Column(db.Float, default=0.0)
    ink_cost = db.Column(db.Float, default=0.0)
    sheets_used = db.Column(db.Integer, default=0)
    impressions_used = db.Column(db.Integer, default=0)
    cups_job_id = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    scanned_at = db.Column(db.DateTime, nullable=True)
    submitted_at = db.Column(db.DateTime, default=utcnow, index=True)
    # When the sheet actually started moving, as distinct from printed_at.
    # last_status_at cannot serve here — completion overwrites it, which would
    # erase the only record of how long the print took.
    printing_started_at = db.Column(db.DateTime, nullable=True)
    printed_at = db.Column(db.DateTime, nullable=True)
    last_status_at = db.Column(db.DateTime, default=utcnow)
    retry_count = db.Column(db.Integer, default=0)

    # Multi-printer routing
    printer_id = db.Column(db.String(64), nullable=True, index=True)
    claimed_by_agent = db.Column(db.String(64), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)

    # Async preview state
    preview_status = db.Column(db.String(20), default='pending')  # pending, ready, failed
    preview_pages = db.Column(db.Integer, default=0)

    # Failure tracking (separate from error_message so we keep raw)
    failed_reason = db.Column(db.String(500), nullable=True)

    # Receipt
    receipt_filename = db.Column(db.String(255), nullable=True)

    # Set once the stored document + previews have been deleted from the
    # server. The row stays for history/billing; the bytes do not.
    files_purged_at = db.Column(db.DateTime, nullable=True)

    # Advanced print options
    page_ranges = db.Column(db.String(120), nullable=True)         # e.g. "1-3,5,7-9"; NULL = all
    pages_per_sheet = db.Column(db.Integer, default=1)             # 1, 2, 4, 6, 9
    page_set = db.Column(db.String(10), default='all')             # all | odd | even
    output_order = db.Column(db.String(10), default='normal')      # normal | reverse
    orientation = db.Column(db.String(20), default='auto')         # auto | portrait | landscape
    fit_to_page = db.Column(db.Boolean, default=False)
    print_quality = db.Column(db.String(10), default='normal')     # draft | normal | high
    collate = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.Index('ix_print_jobs_status_submitted', 'status', 'submitted_at'),
        db.Index('ix_print_jobs_user_status', 'user_id', 'status'),
    )

    @property
    def consumable_cost(self):
        """Total cost of goods for this job."""
        return round((self.paper_cost or 0.0) + (self.ink_cost or 0.0), 2)

    @property
    def margin(self):
        """What the shop actually made on this job."""
        return round((self.cost or 0.0) - self.consumable_cost, 2)

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'page_count': self.page_count,
            'copies': self.copies,
            'color_mode': self.color_mode,
            'paper_size': self.paper_size,
            'sides': self.sides,
            'status': self.status,
            'cost': self.cost,
            'queue_position': self.queue_position,
            'preview_status': self.preview_status,
            'preview_pages': self.preview_pages,
            'files_purged': self.files_purged_at is not None,
            'base_cost': self.base_cost,
            'discount_amount': self.discount_amount,
            'discount_label': self.discount_label,
            'page_ranges': self.page_ranges,
            'pages_per_sheet': self.pages_per_sheet,
            'page_set': self.page_set,
            'output_order': self.output_order,
            'orientation': self.orientation,
            'fit_to_page': self.fit_to_page,
            'print_quality': self.print_quality,
            'collate': self.collate,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'printed_at': self.printed_at.isoformat() if self.printed_at else None,
            'last_status_at': self.last_status_at.isoformat() if self.last_status_at else None,
            'failed_reason': self.failed_reason,
            'user': self.user.full_name if self.user else None,
            'username': self.user.username if self.user else None,
        }


class Pricing(db.Model):
    __tablename__ = 'pricing'

    id = db.Column(db.Integer, primary_key=True)
    paper_size = db.Column(db.String(10), nullable=False)
    color_mode = db.Column(db.String(10), nullable=False)
    # Price for one sheet of paper printed on ONE side.
    price_per_page = db.Column(db.Float, nullable=False, default=0.0)
    # Price for one sheet printed on BOTH sides. A duplex sheet uses the same
    # paper but twice the toner, so it costs more than a simplex sheet but less
    # than two of them. NULL falls back to price_per_page (old behaviour).
    duplex_price_per_page = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.UniqueConstraint('paper_size', 'color_mode', name='uq_pricing_size_color'),
    )

    def price_for_sides(self, sides):
        """Price of one sheet for the given sides setting."""
        if sides == 'two-sided' and self.duplex_price_per_page is not None:
            return self.duplex_price_per_page
        return self.price_per_page


class PricingHistory(db.Model):
    """Append-only log of pricing changes."""
    __tablename__ = 'pricing_history'

    id = db.Column(db.Integer, primary_key=True)
    paper_size = db.Column(db.String(10), nullable=False)
    color_mode = db.Column(db.String(10), nullable=False)
    old_price = db.Column(db.Float, nullable=True)
    new_price = db.Column(db.Float, nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    changed_at = db.Column(db.DateTime, default=utcnow, index=True)


class QRScan(db.Model):
    __tablename__ = 'qr_scans'

    id = db.Column(db.Integer, primary_key=True)
    qr_token = db.Column(db.String(36), nullable=False)
    scan_type = db.Column(db.String(20), default='priority')  # priority, kiosk
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    job_id = db.Column(db.Integer, db.ForeignKey('print_jobs.id'), nullable=True)
    scanned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    scanned_at = db.Column(db.DateTime, default=utcnow)


class AppSettings(db.Model):
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    value = db.Column(db.String(500), nullable=False, default='')


def get_setting(key, default=''):
    """Get an app setting value by key."""
    row = AppSettings.query.filter_by(key=key).first()
    return row.value if row else default


def set_setting(key, value):
    """Set an app setting value (upsert)."""
    row = AppSettings.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = AppSettings(key=key, value=str(value))
        db.session.add(row)
    db.session.commit()


class Advertisement(db.Model):
    """A slide on the kiosk screen.

    `media_type` decides how the kiosk renders it. `image_filename` predates the
    other types and is still read as a fallback so ads created before rich media
    keep working untouched.
    """
    __tablename__ = 'advertisements'

    MEDIA_TYPES = ('text', 'image', 'video', 'pdf', 'html', 'url')

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    media_type = db.Column(db.String(16), default='text', index=True)
    media_filename = db.Column(db.String(255), nullable=True)
    media_mime = db.Column(db.String(100), nullable=True)
    # PDFs are pre-rendered to PNG pages at upload; kiosk browsers cannot be
    # relied on to display an embedded PDF.
    media_pages = db.Column(db.Integer, default=0)
    html_content = db.Column(db.Text, nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    duration_seconds = db.Column(db.Integer, default=6)
    display_order = db.Column(db.Integer, default=0, index=True)

    @property
    def stored_file(self):
        """The file backing this ad, whichever column it landed in."""
        return self.media_filename or self.image_filename

    @property
    def effective_type(self):
        kind = self.media_type or 'text'
        if kind == 'text' and self.image_filename and not self.media_filename:
            return 'image'  # pre-rich-media ad
        return kind

    def to_kiosk_dict(self):
        """What the kiosk display needs to render this slide."""
        kind = self.effective_type
        data = {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'type': kind,
            'duration': max(2, self.duration_seconds or 6),
            'media_url': f'/api/ad-media/{self.id}' if self.stored_file else None,
            'link_url': self.link_url,
            'html': self.html_content if kind == 'html' else None,
            'pages': [f'/api/ad-media/{self.id}/page/{i + 1}'
                      for i in range(self.media_pages or 0)] if kind == 'pdf' else [],
            # Kept so older kiosk builds keep rendering image ads.
            'has_image': bool(self.stored_file) and kind == 'image',
            'image_url': f'/api/ad-media/{self.id}' if (self.stored_file and kind == 'image') else None,
        }
        return data


class PaymentLedger(db.Model):
    __tablename__ = 'payment_ledger'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)  # positive=charge, negative=payment/refund
    description = db.Column(db.String(255), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('print_jobs.id'), nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    entry_type = db.Column(db.String(20), default='charge')  # charge, payment, refund, adjustment


class KioskState(db.Model):
    """Singleton table (row id=1) storing the kiosk QR token state."""
    __tablename__ = 'kiosk_state'

    id = db.Column(db.Integer, primary_key=True)
    current_token = db.Column(db.String(36))
    next_token = db.Column(db.String(36), nullable=True)
    scanned_by = db.Column(db.String(120), nullable=True)
    scanned_username = db.Column(db.String(80), nullable=True)
    jobs_count = db.Column(db.Integer, default=0)
    scanned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    token_expires_at = db.Column(db.DateTime, nullable=True)


class AgentStatus(db.Model):
    """One row per registered print agent / printer — a kiosk, in practice."""
    __tablename__ = 'agent_status'

    id = db.Column(db.Integer, primary_key=True)
    printer_id = db.Column(db.String(64), unique=True, nullable=True, index=True)
    is_online = db.Column(db.Boolean, default=False)
    printer_name = db.Column(db.String(120), nullable=True)
    printer_status = db.Column(db.String(50), nullable=True)
    last_heartbeat = db.Column(db.DateTime, nullable=True)
    agent_version = db.Column(db.String(40), nullable=True)

    # Device identity, reported by the agent each heartbeat.
    mac_address = db.Column(db.String(32), nullable=True)
    hostname = db.Column(db.String(120), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    platform = db.Column(db.String(160), nullable=True)

    # What the kiosk is doing right now.
    activity = db.Column(db.String(20), nullable=True)  # idle, printing, error
    active_job_count = db.Column(db.Integer, default=0)
    last_error = db.Column(db.String(300), nullable=True)
    agent_started_at = db.Column(db.DateTime, nullable=True)

    # The kiosk display (browser) checks in separately from the print agent —
    # the screen can be dead while printing still works, and vice versa.
    kiosk_last_seen = db.Column(db.DateTime, nullable=True)

    # Per-device credential, issued at enrollment. Only the hash is kept: the
    # key itself is shown once, on the Pi, and never stored server-side.
    key_hash = db.Column(db.String(64), nullable=True, index=True)
    key_prefix = db.Column(db.String(12), nullable=True)   # for "which key is this"
    enrolled_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_enrolled(self):
        return bool(self.key_hash) and self.revoked_at is None

    def seconds_since_heartbeat(self, now=None):
        beat = as_aware(self.last_heartbeat)
        if beat is None:
            return None
        return ((now or utcnow()) - beat).total_seconds()

    def is_kiosk_display_up(self, offline_seconds=90, now=None):
        seen = as_aware(self.kiosk_last_seen)
        if seen is None:
            return False
        return ((now or utcnow()) - seen).total_seconds() < offline_seconds


class PrintPreset(db.Model):
    """User-saved print configuration profile."""
    __tablename__ = 'print_presets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    # Stored as individual columns so we can apply directly to a job
    copies = db.Column(db.Integer, default=1)
    color_mode = db.Column(db.String(10), default='bw')
    paper_size = db.Column(db.String(10), default='A4')
    sides = db.Column(db.String(20), default='one-sided')
    pages_per_sheet = db.Column(db.Integer, default=1)
    page_set = db.Column(db.String(10), default='all')
    output_order = db.Column(db.String(10), default='normal')
    orientation = db.Column(db.String(20), default='auto')
    fit_to_page = db.Column(db.Boolean, default=False)
    print_quality = db.Column(db.String(10), default='normal')
    collate = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'name', name='uq_preset_user_name'),
    )

    def apply_to(self, job):
        """Copy this preset's settings onto a PrintJob (does not commit)."""
        job.copies = self.copies
        job.color_mode = self.color_mode
        job.paper_size = self.paper_size
        job.sides = self.sides
        job.pages_per_sheet = self.pages_per_sheet
        job.page_set = self.page_set
        job.output_order = self.output_order
        job.orientation = self.orientation
        job.fit_to_page = self.fit_to_page
        job.print_quality = self.print_quality
        job.collate = self.collate


class EnrollmentCode(db.Model):
    """A short-lived, single-use code that lets a new Pi claim a kiosk key.

    This is what replaces copying a shared secret onto every device: the code is
    typed once, exchanged for a per-device key, and immediately spent.
    """
    __tablename__ = 'enrollment_codes'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    label = db.Column(db.String(120), nullable=True)   # e.g. "Counter 2"
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    used_by_agent_id = db.Column(db.Integer, nullable=True)
    used_from_ip = db.Column(db.String(64), nullable=True)

    creator = db.relationship('User', foreign_keys=[created_by])

    @property
    def is_spent(self):
        return self.used_at is not None

    @property
    def is_expired(self):
        expires = as_aware(self.expires_at)
        return expires is not None and expires < utcnow()

    @property
    def is_usable(self):
        return not self.is_spent and not self.is_expired

    @property
    def seconds_left(self):
        expires = as_aware(self.expires_at)
        if expires is None:
            return 0
        return max(0, int((expires - utcnow()).total_seconds()))


class StockItem(db.Model):
    """A consumable the shop runs out of: paper, or toner.

    Both are counted in the unit they are actually bought and consumed in —
    paper in sheets, toner in pages of yield — so `unit_cost` multiplied by
    usage is the true cost of goods for a job. One row per (kind, key, printer).
    """
    __tablename__ = 'stock_items'

    KINDS = ('paper', 'toner')

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False, index=True)
    # paper → paper size ('A4'); toner → colour mode ('bw', 'color')
    key = db.Column(db.String(32), nullable=False)
    printer_id = db.Column(db.String(64), nullable=True, index=True)

    quantity = db.Column(db.Integer, default=0)        # sheets, or pages of toner left
    # Off until somebody actually counts the stock. A seeded row sits at
    # quantity 0, which is indistinguishable from genuinely empty — without
    # this the shop is told it is out of paper and toner from the day it opens.
    # Set automatically by the first restock or adjustment.
    tracking_enabled = db.Column(db.Boolean, default=False)
    low_threshold = db.Column(db.Integer, default=100)
    unit_cost = db.Column(db.Float, default=0.0)       # ₹ per sheet / per page
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint('kind', 'key', 'printer_id', name='uq_stock_kind_key_printer'),
    )

    @property
    def unit_name(self):
        return 'sheets' if self.kind == 'paper' else 'pages'

    @property
    def label(self):
        if self.kind == 'paper':
            return f'{self.key} paper'
        return f'{"Colour" if self.key == "color" else "Black"} toner'

    @property
    def is_low(self):
        return (self.quantity or 0) <= (self.low_threshold or 0)

    @property
    def is_out(self):
        return (self.quantity or 0) <= 0


class StockMovement(db.Model):
    """Append-only record of every change to a stock level.

    `balance_after` is stored rather than recomputed so the history stays
    readable even after thresholds or costs are edited.
    """
    __tablename__ = 'stock_movements'

    id = db.Column(db.Integer, primary_key=True)
    stock_item_id = db.Column(db.Integer, db.ForeignKey('stock_items.id'), nullable=False, index=True)
    change = db.Column(db.Integer, nullable=False)      # + restock, − consumption
    balance_after = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(24), nullable=False)   # restock, print, adjust, spoilage
    job_id = db.Column(db.Integer, nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    note = db.Column(db.String(200), nullable=True)
    unit_cost = db.Column(db.Float, default=0.0)        # cost at the time of the movement
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    item = db.relationship('StockItem', backref=db.backref('movements', lazy='dynamic'))
    actor = db.relationship('User', foreign_keys=[actor_id])

    @property
    def value(self):
        """₹ this movement was worth — stock bought, or stock consumed."""
        return round(abs(self.change) * (self.unit_cost or 0.0), 2)


class Alert(db.Model):
    """Something the admin needs to know about, with a history.

    An alert stays `active` until the condition clears, so the dashboard shows
    what is wrong *now* while the table keeps what went wrong before.
    """
    __tablename__ = 'alerts'

    KINDS = ('paper_low', 'paper_out', 'toner_low', 'toner_out', 'kiosk_offline')

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(32), nullable=False, index=True)
    severity = db.Column(db.String(16), default='warning')   # warning, critical
    # Identifies the thing the alert is about, so a repeat does not pile up.
    subject = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_note = db.Column(db.String(200), nullable=True)

    @property
    def duration_seconds(self):
        start = as_aware(self.created_at)
        if start is None:
            return None
        end = as_aware(self.resolved_at) or utcnow()
        return (end - start).total_seconds()


class BulkDiscountTier(db.Model):
    """Admin-managed "print more, pay less" tier.

    A tier fires when the job's page impressions (pages x copies, after range
    and odd/even filtering) reach `min_pages`. The highest matching tier wins —
    they do not stack with each other.
    """
    __tablename__ = 'bulk_discount_tiers'

    id = db.Column(db.Integer, primary_key=True)
    min_pages = db.Column(db.Integer, nullable=False)
    discount_percent = db.Column(db.Float, nullable=False, default=0.0)
    max_discount = db.Column(db.Float, nullable=True)  # ₹ cap; NULL = uncapped
    label = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def display_label(self):
        return self.label or f'{self.min_pages}+ pages — {self.discount_percent:g}% off'


class DiscountVoucher(db.Model):
    """A single-use percentage discount sitting in a user's account.

    Vouchers are how referral rewards are delivered: granting one is separate
    from spending one, so a reward survives until the user actually prints.
    """
    __tablename__ = 'discount_vouchers'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    source = db.Column(db.String(32), nullable=False, default='manual')
    # sources: referral_friend (welcome bonus), referral_referrer (reward), manual
    discount_percent = db.Column(db.Float, nullable=False, default=0.0)
    max_discount = db.Column(db.Float, nullable=True)
    description = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(16), nullable=False, default='available', index=True)
    # statuses: available, used, expired, revoked
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    used_job_id = db.Column(db.Integer, nullable=True)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('vouchers', lazy='dynamic'))

    def is_usable(self, now=None):
        if self.status != 'available':
            return False
        expires = as_aware(self.expires_at)
        if expires and expires < (now or utcnow()):
            return False
        return True


class Referral(db.Model):
    """One friend invited by one user.

    Created 'pending' at registration and flipped to 'qualified' the first time
    the invited user completes a print — that is when the referrer's reward is
    granted, and when the invite counts against the campaign limit.
    """
    __tablename__ = 'referrals'

    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    referred_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    code_used = db.Column(db.String(16), nullable=True)
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    # statuses: pending, qualified, expired
    created_at = db.Column(db.DateTime, default=utcnow)
    qualified_at = db.Column(db.DateTime, nullable=True)
    reward_voucher_id = db.Column(db.Integer, nullable=True)

    referrer = db.relationship('User', foreign_keys=[referrer_id],
                               backref=db.backref('referrals_made', lazy='dynamic'))
    referred = db.relationship('User', foreign_keys=[referred_id])


class AuditLog(db.Model):
    """Append-only log of admin actions."""
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    actor_username = db.Column(db.String(80), nullable=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_type = db.Column(db.String(40), nullable=True)  # user, job, pricing, etc.
    target_id = db.Column(db.String(40), nullable=True)
    details = db.Column(db.Text, nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PasswordReset(db.Model):
    """A short-lived, single-use code that lets one account set a new password.

    The code is stored only as a SHA-256 hash, the same way kiosk keys are: it
    is high-entropy and generated by us, so a plain hash is enough and bcrypt's
    work factor would only slow down the counter.

    Two ways in, one table. If SMTP is configured the code is emailed; if not,
    the row sits `pending` until a member of staff issues a fresh code at the
    counter. Either way it expires, is spent on first use, and is invalidated
    the moment a newer one is issued for the same account.
    """
    __tablename__ = 'password_resets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=True, index=True)
    # emailed | counter — how the customer was meant to receive the code.
    delivery = db.Column(db.String(16), nullable=False, default='counter')
    # pending (waiting on staff) | issued (code is live) | used | cancelled
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    requested_ip = db.Column(db.String(64), nullable=True)
    issued_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    issued_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    used_ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    user = db.relationship('User', foreign_keys=[user_id])
    issuer = db.relationship('User', foreign_keys=[issued_by])

    @property
    def is_expired(self):
        expires = as_aware(self.expires_at)
        return bool(expires and expires < utcnow())

    def is_usable(self):
        return (self.status == 'issued'
                and self.code_hash is not None
                and not self.is_expired)
