"""Kiosk enrollment — turning a blank Raspberry Pi into a registered kiosk.

The admin generates a short code, someone types it on the new Pi, and the Pi
exchanges it for a key that belongs to that device alone. Nothing secret has to
be copied by hand, and one kiosk can be revoked without disturbing the others.

Only the SHA-256 of a key is stored. The key itself is returned once, at
enrollment, and lives thereafter only in `/etc/printflow-agent.env` on the Pi.
A plain hash is the right choice here rather than bcrypt: the key is 48 hex
characters of `secrets` output, so there is no guessable password to slow an
attacker down, and this runs on every poll from every kiosk.
"""
import hashlib
import hmac
import logging
import secrets
import string
from datetime import timedelta

from app.extensions import db
from app.models import AgentStatus, EnrollmentCode, utcnow

log = logging.getLogger(__name__)

# No I/O/0/1 — these codes get read aloud and typed by hand.
_ALPHABET = ''.join(c for c in (string.ascii_uppercase + string.digits)
                    if c not in 'IO01')

CODE_TTL_MINUTES = 15
CODE_GROUPS = 3
CODE_GROUP_LEN = 3


def hash_key(key):
    """Stable hash of an agent key. Empty input hashes to None."""
    if not key:
        return None
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def generate_code():
    """A fresh, unused code in XXX-XXX-XXX form."""
    for _ in range(20):
        groups = [''.join(secrets.choice(_ALPHABET) for _ in range(CODE_GROUP_LEN))
                  for _ in range(CODE_GROUPS)]
        code = '-'.join(groups)
        if not EnrollmentCode.query.filter_by(code=code).first():
            return code
    return '-'.join(''.join(secrets.choice(_ALPHABET) for _ in range(4))
                    for _ in range(CODE_GROUPS))


def create_code(created_by=None, label=None, ttl_minutes=CODE_TTL_MINUTES):
    """Open an enrollment window. Returns the EnrollmentCode."""
    entry = EnrollmentCode(
        code=generate_code(),
        label=(label or None),
        created_by=created_by,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes),
    )
    db.session.add(entry)
    db.session.commit()
    log.info('Enrollment code issued (expires in %s min)', ttl_minutes)
    return entry


def normalize_code(raw):
    """Accept what a human actually types: spaces, lowercase, missing dashes."""
    if not raw:
        return ''
    cleaned = ''.join(ch for ch in str(raw).upper() if ch in _ALPHABET)
    if len(cleaned) == CODE_GROUPS * CODE_GROUP_LEN:
        return '-'.join(cleaned[i:i + CODE_GROUP_LEN]
                        for i in range(0, len(cleaned), CODE_GROUP_LEN))
    return str(raw).strip().upper()


def find_usable_code(raw):
    """Look up a code, returning (code, problem). Only one of the two is set."""
    entry = EnrollmentCode.query.filter_by(code=normalize_code(raw)).first()
    if entry is None:
        return None, 'That enrollment code was not recognised.'
    if entry.is_spent:
        return None, 'That enrollment code has already been used.'
    if entry.is_expired:
        return None, 'That enrollment code has expired — generate a new one.'
    return entry, None


def _slug(value):
    """A printer id safe to put in an HTTP header, an env file and a URL."""
    cleaned = []
    for ch in (value or '').strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in ' _-.':
            cleaned.append('-')
    slug = ''.join(cleaned).strip('-')
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug[:48] or 'kiosk'


def _unique_printer_id(preferred):
    """Keep ids distinct when two Pis ship with the same hostname or label."""
    base = _slug(preferred)
    candidate = base
    suffix = 2
    while AgentStatus.query.filter_by(printer_id=candidate).first() is not None:
        candidate = f'{base}-{suffix}'
        suffix += 1
    return candidate


def enroll(raw_code, device=None, remote_ip=None):
    """Spend a code and issue a key to the device presenting it.

    Returns (payload, problem). `payload` carries the one-and-only copy of the
    new key. A device re-enrolling with the same MAC reclaims its existing row
    rather than creating a duplicate kiosk.
    """
    device = device or {}
    entry, problem = find_usable_code(raw_code)
    if entry is None:
        return None, problem

    mac = (device.get('mac_address') or '').strip().upper() or None
    hostname = (device.get('hostname') or '').strip() or None

    agent = None
    if mac:
        agent = AgentStatus.query.filter_by(mac_address=mac).first()
    if agent is None:
        agent = AgentStatus(printer_id=_unique_printer_id(entry.label or hostname))
        db.session.add(agent)
        db.session.flush()

    key = secrets.token_hex(24)
    agent.key_hash = hash_key(key)
    agent.key_prefix = key[:8]
    agent.enrolled_at = utcnow()
    agent.revoked_at = None
    agent.mac_address = mac or agent.mac_address
    agent.hostname = hostname or agent.hostname
    agent.ip_address = (device.get('ip_address') or agent.ip_address)
    agent.platform = (device.get('platform') or agent.platform)
    if entry.label:
        agent.printer_name = agent.printer_name or entry.label

    entry.used_at = utcnow()
    entry.used_by_agent_id = agent.id
    entry.used_from_ip = (remote_ip or '')[:64] or None
    db.session.commit()

    log.info('Kiosk enrolled: %s (%s)', agent.printer_id, mac or 'no MAC')
    return {
        'agent_key': key,
        'printer_id': agent.printer_id,
        'agent_id': agent.printer_id,
        'kiosk_name': agent.printer_name or agent.printer_id,
    }, None


def authenticate(presented_key, master_key=None):
    """Resolve a presented key to a kiosk.

    Returns (agent, is_master). `agent` is None for the master key, which is
    kept working so an existing hand-configured Pi keeps printing through the
    upgrade. Comparisons are constant-time.
    """
    if not presented_key:
        return None, False

    if master_key and hmac.compare_digest(str(presented_key), str(master_key)):
        return None, True

    digest = hash_key(presented_key)
    if not digest:
        return None, False

    for agent in AgentStatus.query.filter(AgentStatus.key_hash.isnot(None)).all():
        if agent.revoked_at is not None:
            continue
        if hmac.compare_digest(agent.key_hash, digest):
            return agent, False
    return None, False


def revoke(agent, reason=None):
    """Kill a kiosk's key. It stops working on its next request."""
    if agent.key_hash is None:
        return False
    agent.key_hash = None
    agent.revoked_at = utcnow()
    db.session.commit()
    log.warning('Kiosk key revoked: %s (%s)', agent.printer_id, reason or 'no reason given')
    return True


def recent_codes(limit=10):
    return EnrollmentCode.query.order_by(EnrollmentCode.created_at.desc()).limit(limit).all()
