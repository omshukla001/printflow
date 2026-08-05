import os
import secrets
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))

# Root for per-instance data (uploads, previews, receipts). On a container host
# point this at a mounted volume — a container filesystem is wiped on every
# restart and redeploy, which would drop a job's file before the agent polls
# for it. Defaults to the repo dir so local/RPi runs are unchanged.
DATA_DIR = os.environ.get('DATA_DIR', basedir)


def _database_url():
    """DATABASE_URL, with the scheme normalized.

    Managed Postgres (Render, Heroku, some Railway setups) still hands out the
    legacy `postgres://` scheme, which SQLAlchemy 1.4+ refuses to load a dialect
    for. Falls back to the local SQLite file when unset.
    """
    url = os.environ.get('DATABASE_URL')
    if not url:
        return 'sqlite:///' + os.path.join(basedir, 'instance', 'printflow.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    ENV = os.environ.get('FLASK_ENV', 'development')
    IS_PRODUCTION = ENV == 'production'

    # SECRET_KEY must be set in production; refuse to start with default
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'printflow-dev-secret-key-change-in-production'

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }
    # Pool sizing only applies to Postgres. SQLite uses a pool class that does
    # not accept pool_size/max_overflow, so passing them would break local runs.
    if SQLALCHEMY_DATABASE_URI.startswith('postgresql'):
        SQLALCHEMY_ENGINE_OPTIONS.update({
            'pool_recycle': 280,
            'pool_size': _env_int('DB_POOL_SIZE', 10),
            'max_overflow': _env_int('DB_MAX_OVERFLOW', 20),
        })

    UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads', 'originals')
    PREVIEW_FOLDER = os.path.join(DATA_DIR, 'uploads', 'previews')
    RECEIPT_FOLDER = os.path.join(DATA_DIR, 'uploads', 'receipts')
    MAX_CONTENT_LENGTH = _env_int('MAX_UPLOAD_BYTES', 50 * 1024 * 1024)  # 50MB max upload

    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'doc', 'docx', 'txt'}
    ALLOWED_MIME_TYPES = {
        'application/pdf',
        'image/png',
        'image/jpeg',
        'image/jpg',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
    }

    # Preview settings
    PREVIEW_DPI = 150
    PREVIEW_MAX_WIDTH = 800
    PREVIEW_PAGE_LIMIT = _env_int('PREVIEW_PAGE_LIMIT', 50)  # cap preview pages for very large PDFs

    # CUPS settings (only used when running locally on RPi)
    CUPS_SERVER = 'localhost'
    # The shop's printer. CUPS accumulates a queue per printer ever plugged in,
    # and submitting to one with no hardware behind it is accepted silently and
    # never prints — so this names the attached device rather than trusting the
    # CUPS default. Override per machine with DEFAULT_PRINTER.
    DEFAULT_PRINTER = os.environ.get('DEFAULT_PRINTER', 'Brother_HL_L2400D')

    # Queue polling interval (seconds)
    CUPS_POLL_INTERVAL = _env_int('CUPS_POLL_INTERVAL', 5)

    # Cloud mode: when True, printing goes through the agent API instead of local CUPS
    CLOUD_MODE = _env_bool('CLOUD_MODE', False)

    # Colour printing. Off while the shop runs a mono-only laser (Brother
    # HL-L2400D). Flip to true (or set COLOR_PRINTING_ENABLED=true) once a
    # colour printer is attached — the colour option reappears in the UI and
    # colour pricing rows become selectable again. Server-side validation
    # forces every job to 'bw' while this is false, so a crafted POST can't
    # bypass the hidden field.
    COLOR_PRINTING_ENABLED = _env_bool('COLOR_PRINTING_ENABLED', False)

    # Paper sizes offered to customers, in display order. The shop stocks A4
    # only, so A3/Letter are off — set PAPER_SIZES=A4,A3,Letter to restore them.
    # Anything not listed here is rejected server-side and clamped to the first
    # entry, so a crafted POST can't order a size the shop can't load.
    PAPER_SIZES = [s.strip() for s in
                   os.environ.get('PAPER_SIZES', 'A4').split(',') if s.strip()] or ['A4']

    # Site URL for QR codes (set to your domain in production)
    SITE_URL = os.environ.get('SITE_URL', '')

    # Shared agent key. Still honoured for a hand-configured Pi, but new kiosks
    # should enroll instead and get a key of their own — see /admin/kiosks.
    AGENT_API_KEY = os.environ.get('AGENT_API_KEY', '')

    # Enrollment hands a brand-new key over the wire, so it refuses plain HTTP
    # unless this is set. Only turn it on for a trusted local network.
    ALLOW_INSECURE_ENROLL = _env_bool('ALLOW_INSECURE_ENROLL', False)
    ENROLL_CODE_MINUTES = _env_int('ENROLL_CODE_MINUTES', 15)

    # --- Security ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', IS_PRODUCTION)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', IS_PRODUCTION)
    REMEMBER_COOKIE_DURATION = timedelta(hours=_env_int('REMEMBER_HOURS', 12))
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int('SESSION_HOURS', 12))

    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # tie to session
    WTF_CSRF_SSL_STRICT = False  # the agent / proxy may terminate TLS

    # Account lockout
    LOCKOUT_THRESHOLD = _env_int('LOCKOUT_THRESHOLD', 5)
    LOCKOUT_MINUTES = _env_int('LOCKOUT_MINUTES', 15)

    # Per-user concurrent active jobs cap
    USER_MAX_ACTIVE_JOBS = _env_int('USER_MAX_ACTIVE_JOBS', 10)

    # Kiosk token TTL (seconds)
    KIOSK_TOKEN_TTL = _env_int('KIOSK_TOKEN_TTL', 90)

    # Rate limits (Flask-Limiter strings)
    RATE_LIMIT_DEFAULT = os.environ.get('RATE_LIMIT_DEFAULT', '200 per minute')
    RATE_LIMIT_LOGIN = os.environ.get('RATE_LIMIT_LOGIN', '10 per minute; 50 per hour')
    RATE_LIMIT_REGISTER = os.environ.get('RATE_LIMIT_REGISTER', '5 per minute; 20 per hour')
    RATE_LIMIT_CHECKIN = os.environ.get('RATE_LIMIT_CHECKIN', '20 per minute; 200 per hour')
    RATE_LIMIT_AGENT = os.environ.get('RATE_LIMIT_AGENT', '600 per minute')
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')

    # Watchdog timeouts (minutes)
    STUCK_READY_MINUTES = _env_int('STUCK_READY_MINUTES', 10)
    STUCK_PRINTING_MINUTES = _env_int('STUCK_PRINTING_MINUTES', 15)
    AGENT_OFFLINE_SECONDS = _env_int('AGENT_OFFLINE_SECONDS', 60)
    ORPHAN_FILE_DAYS = _env_int('ORPHAN_FILE_DAYS', 7)

    # --- File retention ---
    # The server is a staging post, not an archive: it holds a document only
    # long enough for the agent to fetch and print it. Once the job reaches a
    # terminal state the stored file and its previews are deleted; the DB row
    # (billing, history) is kept.
    PURGE_AFTER_PRINT = _env_bool('PURGE_AFTER_PRINT', True)
    # Grace period after a job completes before its file is deleted. 0 = delete
    # as soon as the agent confirms the print. Raise it (e.g. 60) to keep
    # one-click reprint working for a while after printing.
    FILE_RETENTION_MINUTES = _env_int('FILE_RETENTION_MINUTES', 0)
    # Failed jobs keep their file longer so an admin can retry the print.
    FAILED_FILE_RETENTION_HOURS = _env_int('FAILED_FILE_RETENTION_HOURS', 24)
    # Uploads that were queued and then never printed or cancelled. After this
    # many hours they are cancelled (refunding any charge) and their file is
    # deleted. Set to 0 to leave abandoned uploads alone.
    ABANDONED_FILE_HOURS = _env_int('ABANDONED_FILE_HOURS', 72)

    @classmethod
    def validate(cls):
        """Fail fast on insecure production config."""
        if cls.IS_PRODUCTION:
            problems = []
            if cls.SECRET_KEY == 'printflow-dev-secret-key-change-in-production':
                problems.append('SECRET_KEY is set to the default. Set a random SECRET_KEY env var.')
            # AGENT_API_KEY being empty is now the normal, preferred state:
            # kiosks enroll and get per-device keys, so there is no shared
            # secret to configure. Only the insecure-enrollment escape hatch is
            # worth refusing to boot over.
            if cls.CLOUD_MODE and cls.ALLOW_INSECURE_ENROLL:
                problems.append('ALLOW_INSECURE_ENROLL is on in production — kiosk keys '
                                'would be handed out over plain HTTP.')
            if problems:
                raise RuntimeError('Production config errors:\n  - ' + '\n  - '.join(problems))
