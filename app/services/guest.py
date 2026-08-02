"""Walk-in guest user creation."""
import secrets
import string
from datetime import timedelta
import bcrypt
from app.extensions import db
from app.models import User, utcnow


def generate_guest_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    # Avoid confusable characters
    alphabet = alphabet.replace('O', '').replace('0', '').replace('I', '').replace('1', '')
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def create_guest(label=None, hours=4):
    """Create a temporary guest user. Returns (user, plaintext_password)."""
    suffix = secrets.token_hex(3)
    username = f'guest_{suffix}'
    password = generate_guest_code(10)
    pw_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    user = User(
        username=username,
        email=f'{username}@guest.local',
        password_hash=pw_hash,
        full_name=label or f'Guest {suffix.upper()}',
        is_admin=False,
        is_active_user=True,
        is_guest=True,
        guest_expires_at=utcnow() + timedelta(hours=hours),
    )
    db.session.add(user)
    db.session.commit()
    return user, password
