"""Print-lock password helpers — bcrypt-hashed at rest."""
import bcrypt
from app.models import get_setting, set_setting


def is_enabled():
    return get_setting('print_lock_enabled', 'false') == 'true'


def set_password(plaintext):
    """Hash and store the kiosk lock password."""
    if not plaintext:
        raise ValueError('Empty password')
    h = bcrypt.hashpw(plaintext.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    set_setting('print_lock_password_hash', h)
    # Wipe any legacy plaintext copy
    set_setting('print_lock_password', '')


def check_password(plaintext):
    if not plaintext:
        return False
    h = get_setting('print_lock_password_hash', '')
    if h:
        try:
            return bcrypt.checkpw(plaintext.encode('utf-8'), h.encode('utf-8'))
        except Exception:
            return False
    # Backwards compatibility: pre-hash deployments stored plaintext.
    legacy = get_setting('print_lock_password', '')
    if legacy and plaintext == legacy:
        # Migrate on first successful check
        set_password(plaintext)
        return True
    return False


def enable(plaintext):
    set_password(plaintext)
    set_setting('print_lock_enabled', 'true')


def disable():
    set_setting('print_lock_enabled', 'false')
