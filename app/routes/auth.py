from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
import bcrypt
from app.extensions import db, limiter
from app.models import User, utcnow
from app.services import audit, mailer, offers, password_reset

auth_bp = Blueprint('auth', __name__)


def _login_limits():
    return current_app.config.get('RATE_LIMIT_LOGIN', '10 per minute; 50 per hour')


def _register_limits():
    return current_app.config.get('RATE_LIMIT_REGISTER', '5 per minute; 20 per hour')


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_LOGIN', '10 per minute; 50 per hour'),
               methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()
        now = datetime.now(timezone.utc)

        if user and user.is_locked:
            wait = int((user.locked_until - now).total_seconds() / 60) + 1
            flash(f'Account temporarily locked due to failed attempts. Try again in {wait} min.',
                  'error')
            return render_template('auth/login.html'), 429

        if user and bcrypt.checkpw(password.encode('utf-8'),
                                    user.password_hash.encode('utf-8')):
            if not user.is_active_user:
                flash('Your account has been deactivated.', 'error')
                return render_template('auth/login.html')

            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = now
            db.session.commit()

            login_user(user, remember=True, duration=current_app.config.get(
                'REMEMBER_COOKIE_DURATION', timedelta(hours=12)))
            audit.record('login.success', target_type='user', target_id=user.id)
            db.session.commit()

            next_page = request.args.get('next')
            # Only allow relative redirects
            if next_page and not next_page.startswith('/'):
                next_page = None
            if user.is_admin:
                return redirect(next_page or url_for('admin.dashboard'))
            return redirect(next_page or url_for('user.dashboard'))

        # Failed attempt
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            threshold = current_app.config.get('LOCKOUT_THRESHOLD', 5)
            if user.failed_login_count >= threshold:
                user.locked_until = now + timedelta(
                    minutes=current_app.config.get('LOCKOUT_MINUTES', 15))
                audit.record('login.locked', target_type='user', target_id=user.id,
                             details={'count': user.failed_login_count})
            else:
                audit.record('login.failure', target_type='user', target_id=user.id,
                             details={'count': user.failed_login_count})
            db.session.commit()
        else:
            audit.record('login.failure', details={'username': username[:80]})
            db.session.commit()

        flash('Invalid username or password.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_REGISTER', '5 per minute; 20 per hour'),
               methods=['POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        full_name = request.form.get('full_name', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        errors = []
        if not username or len(username) < 3 or not username.replace('_', '').isalnum():
            errors.append('Username must be at least 3 alphanumeric characters.')
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            errors.append('Valid email is required.')
        if not full_name:
            errors.append('Full name is required.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
            errors.append('Password must contain letters and digits.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if User.query.filter_by(username=username).first():
            errors.append('Username already taken.')
        if User.query.filter_by(email=email).first():
            errors.append('Email already registered.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('auth/register.html',
                                   referral_code=(request.form.get('referral_code') or '').strip().upper(),
                                   offers=offers.get_settings())

        pw_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user = User(username=username, email=email, full_name=full_name,
                    password_hash=pw_hash, password_changed_at=utcnow())
        db.session.add(user)
        db.session.commit()
        offers.ensure_referral_code(user)
        audit.record('user.register', target_type='user', target_id=user.id,
                     details={'username': username})
        db.session.commit()

        # A bad or closed referral code must never cost someone their signup.
        referral_code = (request.form.get('referral_code') or '').strip().upper()
        if referral_code:
            _, voucher, problem = offers.register_referral(user, referral_code)
            if voucher is not None:
                flash(f'Referral applied — {voucher.discount_percent:g}% off your first print!',
                      'success')
            elif problem:
                flash(problem, 'error')

        flash('Account created! Please log in.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/register.html',
                           referral_code=request.args.get('ref', '').strip().upper(),
                           offers=offers.get_settings())


@auth_bp.route('/logout')
@login_required
def logout():
    audit.record('logout', target_type='user', target_id=current_user.id)
    db.session.commit()
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/about')
def about():
    return render_template('about.html')


def _reset_limits():
    return current_app.config.get('RATE_LIMIT_RESET', '5 per minute; 20 per hour')


@auth_bp.route('/forgot', methods=['GET', 'POST'])
@limiter.limit(_reset_limits, methods=['POST'])
def forgot_password():
    """Ask for a reset code."""
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    email_on = mailer.is_configured()

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        if not identifier:
            flash('Enter your username or email.', 'error')
            return render_template('auth/forgot.html', email_enabled=email_on)

        delivery, sent = password_reset.request_reset(identifier, ip=request.remote_addr)
        audit.record('password.reset_request',
                     details={'identifier': identifier[:80], 'outcome': delivery})
        db.session.commit()

        # Same answer whichever branch ran, so this form cannot be used to find
        # out which usernames exist. Only the audit log knows the difference.
        if sent:
            flash('If that account exists, a reset code has been emailed to it. '
                  'The code expires in 30 minutes.', 'success')
        else:
            flash('Request received. Ask the shop for your reset code — staff can '
                  'issue one at the counter.', 'info')
        return redirect(url_for('auth.reset_password'))

    return render_template('auth/forgot.html', email_enabled=email_on)


@auth_bp.route('/reset', methods=['GET', 'POST'])
@limiter.limit(_reset_limits, methods=['POST'])
def reset_password():
    """Enter the code and choose a new password."""
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    identifier = request.args.get('user', '').strip()

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        code = request.form.get('code', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        user, row = password_reset.verify(identifier, code)
        if user is None:
            # One message for a wrong code, an expired one and an unknown
            # account alike — anything more specific is a probing oracle.
            audit.record('password.reset_failed',
                         details={'identifier': identifier[:80]})
            db.session.commit()
            flash('That code is not valid, or it has expired. Ask for a new one.',
                  'error')
            return render_template('auth/reset.html', identifier=identifier)

        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('auth/reset.html', identifier=identifier)

        problems = password_reset.consume(user, row, password, ip=request.remote_addr)
        if problems:
            for p in problems:
                flash(p, 'error')
            return render_template('auth/reset.html', identifier=identifier)

        audit.record('password.reset_done', target_type='user', target_id=user.id,
                     details={'delivery': row.delivery})
        db.session.commit()
        flash('Password changed. You can log in now.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset.html', identifier=identifier)
