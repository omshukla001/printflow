"""Kiosk routes — monitor display + phone check-in endpoint."""
import socket
from datetime import datetime, timezone
from flask import Blueprint, render_template, redirect, url_for, flash, jsonify, request, current_app
from flask_login import current_user

from app.extensions import db, csrf, limiter
from app.models import PrintJob, QRScan, User
from app.services.kiosk import (
    get_current_token, generate_new_token, validate_and_consume,
    mark_scanned, get_status, activate_next_token, peek_token,
)
from app.services.queue_manager import fail_job
from app.services.qr_service import generate_qr_png
from app.services.pricing import lock_job_cost
from app.services import audit, print_lock


def _get_site_url():
    site_url = current_app.config.get('SITE_URL', '').rstrip('/')
    if site_url:
        return site_url
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:5000"
    except Exception:
        return "http://127.0.0.1:5000"


kiosk_bp = Blueprint('kiosk', __name__)
checkin_bp = Blueprint('checkin', __name__)


@kiosk_bp.before_request
def restrict_kiosk():
    """In cloud mode, only allow kiosk access with the agent key or from localhost."""
    if current_app.config.get('CLOUD_MODE', False):
        agent_key = current_app.config.get('AGENT_API_KEY', '')
        if agent_key and request.headers.get('X-Agent-Key') != agent_key:
            from flask import abort
            abort(403)


@kiosk_bp.route('/kiosk')
def display():
    token = get_current_token()
    site_url = _get_site_url()
    return render_template('kiosk/display.html', token=token, site_url=site_url)


@kiosk_bp.route('/kiosk/qr.png')
def qr_image():
    token = get_current_token()
    base_url = _get_site_url()
    checkin_url = f"{base_url}/checkin/{token}"
    png_bytes = generate_qr_png(checkin_url, box_size=14, border=2)
    return (png_bytes, 200, {'Content-Type': 'image/png', 'Cache-Control': 'no-store'})


@kiosk_bp.route('/kiosk/qr/<token>.png')
def qr_image_for_token(token):
    base_url = _get_site_url()
    checkin_url = f"{base_url}/checkin/{token}"
    png_bytes = generate_qr_png(checkin_url, box_size=14, border=2)
    return (png_bytes, 200, {'Content-Type': 'image/png', 'Cache-Control': 'no-store'})


@kiosk_bp.route('/kiosk/status')
def status():
    from app.models import Advertisement
    data = get_status()
    data['print_locked'] = print_lock.is_enabled()
    pending_count = PrintJob.query.filter(
        PrintJob.status.in_(['queued', 'prioritized', 'ready_to_print'])
    ).count()
    data['pending_count'] = pending_count
    printing_jobs = PrintJob.query.filter(PrintJob.status == 'printing').all()
    if printing_jobs:
        user_ids = set(j.user_id for j in printing_jobs)
        users = {u.id: u.full_name for u in User.query.filter(User.id.in_(user_ids)).all()}
        data['printing'] = [{
            'user': users.get(j.user_id, 'Unknown'),
            'filename': j.filename,
        } for j in printing_jobs]
    else:
        data['printing'] = []
    ads = Advertisement.query.filter_by(is_active=True).order_by(Advertisement.created_at.desc()).all()
    data['ads'] = [{
        'id': a.id,
        'title': a.title,
        'content': a.content,
        'has_image': bool(a.image_filename),
        'image_url': f'/api/ad-image/{a.id}' if a.image_filename else None,
    } for a in ads]
    return jsonify(data)


@kiosk_bp.route('/kiosk/activate', methods=['POST'])
@csrf.exempt  # kiosk display is a long-lived browser page; activate is internal-only
def activate():
    token = activate_next_token()
    return jsonify({'ok': True, 'token': token})


# --- Public checkin routes (accessible from phones) ---

@checkin_bp.route('/checkin/<token>', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CHECKIN', '20 per minute; 200 per hour'))
def checkin(token):
    """Phone-facing endpoint — user scans QR and lands here."""
    if not current_user.is_authenticated:
        flash('Please log in first, then scan the QR code again.', 'info')
        return redirect(url_for('auth.login', next=f'/checkin/{token}'))

    lock_enabled = print_lock.is_enabled()

    if lock_enabled and request.method == 'GET':
        if not peek_token(token):
            return render_template('kiosk/checkin_expired.html')
        return render_template('kiosk/checkin_locked.html',
                               user=current_user,
                               unlock_url=f'/checkin/{token}',
                               error=None)

    if lock_enabled and request.method == 'POST':
        submitted_code = request.form.get('lock_password', '').strip()
        if not print_lock.check_password(submitted_code):
            return render_template('kiosk/checkin_locked.html',
                                   user=current_user,
                                   unlock_url=f'/checkin/{token}',
                                   error='Wrong code. Try again.')

    if not validate_and_consume(token):
        return render_template('kiosk/checkin_expired.html')

    scanned_at = datetime.now(timezone.utc)
    cloud_mode = current_app.config.get('CLOUD_MODE', False)

    pending_jobs = PrintJob.query.filter(
        PrintJob.user_id == current_user.id,
        PrintJob.status.in_(['queued', 'prioritized'])
    ).order_by(PrintJob.submitted_at.asc()).all()

    printed = []
    errors = []

    if cloud_mode:
        for job in pending_jobs:
            lock_job_cost(job)
            job.status = 'ready_to_print'
            job.scanned_at = scanned_at
            job.last_status_at = scanned_at
            printed.append(job.filename)
        db.session.commit()
    else:
        from app.services.printer import submit_job
        for job in pending_jobs:
            try:
                lock_job_cost(job)
                cups_job_id = submit_job(job.file_path,
                                         f'PrintFlow #{job.id}: {job.filename}', job)
                job.cups_job_id = cups_job_id
                job.status = 'printing'
                job.scanned_at = scanned_at
                job.last_status_at = scanned_at
                db.session.commit()
                printed.append(job.filename)
            except Exception as e:
                fail_job(job, str(e))
                errors.append(f'{job.filename}: {e}')

    scan_record = QRScan(
        qr_token=token, scan_type='kiosk',
        user_id=current_user.id, scanned_by=current_user.id, scanned_at=scanned_at,
    )
    db.session.add(scan_record)
    audit.record('kiosk.checkin', target_type='user', target_id=current_user.id,
                 details={'printed': len(printed), 'errors': len(errors)})
    db.session.commit()

    mark_scanned(current_user.full_name, current_user.username, len(printed))

    return render_template('kiosk/checkin_success.html',
                           printed=printed, errors=errors, user=current_user)
