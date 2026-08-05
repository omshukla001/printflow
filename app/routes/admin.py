import os
import uuid
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_required, current_user
from functools import wraps
from werkzeug.utils import secure_filename
from app.extensions import db
from app.models import (
    User, PrintJob, Pricing, PricingHistory, QRScan, PaymentLedger,
    Advertisement, AgentStatus, AuditLog, BulkDiscountTier, DiscountVoucher,
    Referral, StockItem, StockMovement, Alert, get_setting, set_setting,
)
from app.services.queue_manager import (
    get_ordered_queue, get_next_job, complete_job, fail_job, cancel_job,
    prioritize_user_jobs,
)
from app.services.pricing import calculate_cost, lock_job_cost
from app.services.print_options import validate_and_apply
from app.services.file_handler import save_upload, allowed_file, get_extension, CONVERTIBLE_EXTENSIONS
from app.services import (audit, print_lock, guest as guest_service, offers,
                          ads as ad_service, stock as stock_service, enrollment,
                          mailer)
from app.services.kiosk import reset_state as reset_kiosk

admin_bp = Blueprint('admin', __name__)


def _get_printer_status():
    """Get printer status from agent heartbeat (cloud) or direct CUPS (local)."""
    if current_app.config.get('CLOUD_MODE', False):
        agents = AgentStatus.query.order_by(AgentStatus.last_heartbeat.desc()).all()
        # Aggregate view: the first online agent wins for the main dashboard
        offline_seconds = current_app.config.get('AGENT_OFFLINE_SECONDS', 60)
        now = datetime.now(timezone.utc)
        online_agent = None
        for a in agents:
            if a.last_heartbeat and (now - _ensure_aware(a.last_heartbeat)) < timedelta(seconds=offline_seconds):
                online_agent = a
                break
        if online_agent:
            return {
                'name': online_agent.printer_name or online_agent.printer_id or 'Unknown',
                'status': online_agent.printer_status or 'Online',
                'online': True,
                'agents': len(agents),
            }
        if agents:
            return {'name': agents[0].printer_name or agents[0].printer_id or 'Unknown',
                    'status': 'Agent Offline', 'online': False, 'agents': len(agents)}
        return {'name': 'Unknown', 'status': 'Agent Not Connected', 'online': False, 'agents': 0}
    else:
        from app.services.printer import get_printer_status
        return get_printer_status()


def _ensure_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            flash('Admin access required.', 'error')
            return redirect(url_for('user.dashboard'))
        return f(*args, **kwargs)
    return decorated


@admin_bp.before_request
@login_required
def require_admin():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('user.dashboard'))


@admin_bp.route('/dashboard')
def dashboard():
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = today_start.replace(day=1)

    def sum_revenue(since):
        result = db.session.query(db.func.sum(PaymentLedger.amount)).filter(
            PaymentLedger.amount > 0,
            PaymentLedger.entry_type == 'charge',
            PaymentLedger.created_at >= since
        ).scalar()
        return result or 0.0

    revenue_today = sum_revenue(today_start)
    revenue_week = sum_revenue(week_start)
    revenue_month = sum_revenue(month_start)
    revenue_total = db.session.query(db.func.sum(PaymentLedger.amount)).filter(
        PaymentLedger.amount > 0,
        PaymentLedger.entry_type == 'charge',
    ).scalar() or 0.0

    pending_count = PrintJob.query.filter(PrintJob.status.in_(['queued', 'prioritized'])).count()
    total_jobs = PrintJob.query.count()
    total_users = User.query.filter_by(is_admin=False).count()
    stuck_count = PrintJob.query.filter(
        PrintJob.status.in_(('ready_to_print', 'printing')),
        PrintJob.last_status_at < (now - timedelta(minutes=current_app.config.get('STUCK_PRINTING_MINUTES', 15))),
    ).count()

    printer = _get_printer_status()
    print_lock_enabled = print_lock.is_enabled()
    stock_service.check_all()
    today_costs = stock_service.cost_summary(today_start)

    from app.services.password_reset import pending_requests
    return render_template('admin/dashboard.html',
                           alerts=stock_service.active_alerts(),
                           reset_requests=len(pending_requests()),
                           costs_today=today_costs,
                           revenue_today=revenue_today,
                           revenue_week=revenue_week,
                           revenue_month=revenue_month,
                           revenue_total=revenue_total,
                           pending_count=pending_count,
                           total_jobs=total_jobs,
                           total_users=total_users,
                           stuck_count=stuck_count,
                           printer=printer,
                           print_lock_enabled=print_lock_enabled)


@admin_bp.route('/queue')
def queue():
    ordered_jobs = get_ordered_queue()
    printer = _get_printer_status()
    # What each job will cost the shop in paper and toner, shown next to what
    # the customer is being charged.
    costs = {job.id: stock_service.estimate_cost(job) for job in ordered_jobs}
    return render_template('admin/queue.html', jobs=ordered_jobs, printer=printer,
                           costs=costs)


@admin_bp.route('/queue/print-next', methods=['POST'])
def print_next():
    job = get_next_job()
    if not job:
        flash('No jobs in queue.', 'info')
        return redirect(url_for('admin.queue'))
    return _print_job(job)


@admin_bp.route('/queue/print/<int:job_id>', methods=['POST'])
def print_job(job_id):
    job = PrintJob.query.get_or_404(job_id)
    return _print_job(job)


def _print_job(job):
    if job.status not in ('queued', 'prioritized'):
        flash('Job is not in a printable state.', 'error')
        return redirect(url_for('admin.queue'))

    if job.files_purged_at is not None:
        flash('The file for this job has already been deleted from the server.', 'error')
        return redirect(url_for('admin.queue'))

    lock_job_cost(job)

    if current_app.config.get('CLOUD_MODE', False):
        job.status = 'ready_to_print'
        job.last_status_at = datetime.now(timezone.utc)
        db.session.commit()
        audit.record('job.send_to_printer', target_type='job', target_id=job.id,
                     details={'mode': 'cloud'})
        db.session.commit()
        flash(f'Sent to printer: {job.filename}', 'success')
    else:
        try:
            from app.services.printer import submit_job
            cups_job_id = submit_job(job.file_path,
                                      f'PrintFlow #{job.id}: {job.filename}', job)
            job.cups_job_id = cups_job_id
            job.status = 'printing'
            job.last_status_at = datetime.now(timezone.utc)
            db.session.commit()
            audit.record('job.print', target_type='job', target_id=job.id,
                         details={'mode': 'local', 'cups_job_id': cups_job_id})
            db.session.commit()
            flash(f'Printing: {job.filename} (CUPS #{cups_job_id})', 'success')
        except Exception as e:
            fail_job(job, str(e))
            audit.record('job.print.failed', target_type='job', target_id=job.id,
                         details={'error': str(e)})
            db.session.commit()
            flash(f'Print failed: {e}', 'error')

    return redirect(url_for('admin.queue'))


@admin_bp.route('/queue/cancel/<int:job_id>', methods=['POST'], endpoint='cancel_job')
def admin_cancel_job(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if job.cups_job_id and not current_app.config.get('CLOUD_MODE', False):
        from app.services.printer import cancel_cups_job
        cancel_cups_job(job.cups_job_id)
    if not cancel_job(job, by_user_id=current_user.id):
        flash('Job already finished.', 'info')
    else:
        audit.record('job.cancel', target_type='job', target_id=job.id)
        db.session.commit()
        flash(f'Cancelled: {job.filename}', 'info')
    return redirect(url_for('admin.queue'))


@admin_bp.route('/queue/complete/<int:job_id>', methods=['POST'])
def mark_complete(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if not complete_job(job):
        flash('Job already completed.', 'info')
    else:
        audit.record('job.complete_manual', target_type='job', target_id=job.id)
        db.session.commit()
        flash(f'Completed: {job.filename} — ₹{job.cost:.2f} charged.', 'success')
    return redirect(url_for('admin.queue'))


@admin_bp.route('/queue/reorder', methods=['POST'])
def reorder_queue():
    data = request.get_json()
    if not data or 'order' not in data:
        return jsonify({'error': 'Missing order'}), 400
    for i, job_id in enumerate(data['order']):
        job = db.session.get(PrintJob, int(job_id))
        if job:
            job.queue_position = i + 1
    db.session.commit()
    audit.record('queue.reorder', details={'count': len(data['order'])})
    db.session.commit()
    return jsonify({'ok': True})


@admin_bp.route('/queue/reset-stuck', methods=['POST'])
def reset_stuck():
    """Force-fail all stuck jobs in ready_to_print/printing past the configured timeout."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=current_app.config.get('STUCK_PRINTING_MINUTES', 15))
    stuck = PrintJob.query.filter(
        PrintJob.status.in_(('ready_to_print', 'printing')),
        PrintJob.last_status_at < cutoff,
    ).all()
    count = 0
    for job in stuck:
        if fail_job(job, 'Admin force-reset (stuck)'):
            count += 1
    audit.record('queue.reset_stuck', details={'count': count})
    db.session.commit()
    flash(f'Reset {count} stuck job(s).', 'info')
    return redirect(url_for('admin.queue'))


@admin_bp.route('/queue/reset-kiosk', methods=['POST'])
def reset_kiosk_route():
    new_token = reset_kiosk()
    audit.record('kiosk.reset', details={'new_token': new_token[:8] + '…'})
    db.session.commit()
    flash('Kiosk QR token rotated.', 'info')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/scan')
def scan():
    recent_scans = QRScan.query.order_by(QRScan.scanned_at.desc()).limit(10).all()
    return render_template('admin/scan.html', recent_scans=recent_scans)


@admin_bp.route('/scan/process', methods=['POST'])
def process_scan():
    data = request.get_json()
    if not data or 'qr_token' not in data:
        return jsonify({'error': 'No QR data'}), 400

    qr_token = data['qr_token'].strip()
    user = User.query.filter_by(qr_token=qr_token).first()
    if not user:
        return jsonify({'error': 'Unknown QR code'}), 404

    scanned_at = datetime.now(timezone.utc)
    count = prioritize_user_jobs(user.id, scanned_at)

    scan_record = QRScan(
        qr_token=qr_token, scan_type='priority',
        user_id=user.id, scanned_by=current_user.id, scanned_at=scanned_at,
    )
    db.session.add(scan_record)
    audit.record('scan.priority', target_type='user', target_id=user.id,
                 details={'count': count})
    db.session.commit()

    return jsonify({
        'ok': True,
        'user': user.full_name,
        'username': user.username,
        'jobs_prioritized': count,
    })


@admin_bp.route('/pricing', methods=['GET', 'POST'])
def pricing():
    if request.method == 'POST':
        entries = Pricing.query.filter_by(is_active=True).all()
        changes = []
        for entry in entries:
            key = f'price_{entry.id}'
            new_price = request.form.get(key)
            if new_price is None:
                continue
            try:
                new_val = float(new_price)
            except ValueError:
                continue
            if abs(new_val - entry.price_per_page) < 0.001:
                continue
            db.session.add(PricingHistory(
                paper_size=entry.paper_size,
                color_mode=entry.color_mode,
                old_price=entry.price_per_page,
                new_price=new_val,
                changed_by=current_user.id,
            ))
            changes.append((entry.paper_size, entry.color_mode, entry.price_per_page, new_val))
            entry.price_per_page = new_val

        # Duplex rates, edited alongside the simplex ones.
        for entry in entries:
            raw = request.form.get(f'duplex_{entry.id}')
            if raw is None or raw == '':
                continue
            try:
                new_val = float(raw)
            except ValueError:
                continue
            old_val = entry.duplex_price_per_page
            if old_val is not None and abs(new_val - old_val) < 0.001:
                continue
            db.session.add(PricingHistory(
                paper_size=entry.paper_size,
                color_mode=f'{entry.color_mode}-2s',
                old_price=old_val,
                new_price=new_val,
                changed_by=current_user.id,
            ))
            changes.append((entry.paper_size, f'{entry.color_mode}-2s', old_val, new_val))
            entry.duplex_price_per_page = new_val

        db.session.commit()
        if changes:
            audit.record('pricing.update', details={'changes': changes})
            db.session.commit()
        flash('Pricing updated.', 'success')
        return redirect(url_for('admin.pricing'))

    entries = Pricing.query.filter_by(is_active=True).order_by(
        Pricing.paper_size, Pricing.color_mode).all()
    recent_history = PricingHistory.query.order_by(
        PricingHistory.changed_at.desc()).limit(20).all()
    return render_template('admin/pricing.html', entries=entries, history=recent_history,
                           color_enabled=current_app.config.get('COLOR_PRINTING_ENABLED', False))


# --- Stock ----------------------------------------------------------------

@admin_bp.route('/stock')
def stock_page():
    stock_service.check_all()
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    movements = StockMovement.query.order_by(
        StockMovement.created_at.desc()).limit(60).all()
    alerts = Alert.query.order_by(Alert.is_active.desc(),
                                  Alert.created_at.desc()).limit(40).all()

    return render_template('admin/stock.html',
                           items=stock_service.all_items(),
                           movements=movements,
                           alerts=alerts,
                           today=stock_service.cost_summary(day_start),
                           month=stock_service.cost_summary(month_start),
                           overall=stock_service.cost_summary())


@admin_bp.route('/stock/<int:item_id>/restock', methods=['POST'])
def restock_item(item_id):
    item = StockItem.query.get_or_404(item_id)
    raw = (request.form.get('amount') or '').strip()
    packs = (request.form.get('pack_size') or '').strip()
    try:
        amount = int(float(raw))
        if packs:
            amount *= max(1, int(float(packs)))
    except ValueError:
        flash('Enter a number to add.', 'error')
        return redirect(url_for('admin.stock_page'))

    try:
        stock_service.restock(item, amount, actor_id=current_user.id,
                              note=(request.form.get('note') or '').strip()[:200] or None)
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin.stock_page'))

    audit.record('stock.restock', target_type='stock', target_id=item.id,
                 details={'item': item.label, 'added': amount, 'balance': item.quantity})
    db.session.commit()
    flash(f'Added {amount} {item.unit_name} of {item.label} — now {item.quantity}.', 'success')
    return redirect(url_for('admin.stock_page'))


@admin_bp.route('/stock/<int:item_id>/adjust', methods=['POST'])
def adjust_item(item_id):
    """Set the level from a physical count."""
    item = StockItem.query.get_or_404(item_id)
    try:
        counted = int(float(request.form.get('counted', item.quantity)))
    except ValueError:
        flash('Enter the counted number.', 'error')
        return redirect(url_for('admin.stock_page'))

    movement = stock_service.adjust(item, counted, actor_id=current_user.id,
                                    note=(request.form.get('note') or '').strip()[:200] or None)
    if movement is None:
        flash('Count matches what was on record — nothing changed.', 'info')
    else:
        audit.record('stock.adjust', target_type='stock', target_id=item.id,
                     details={'item': item.label, 'change': movement.change,
                              'balance': item.quantity})
        db.session.commit()
        flash(f'{item.label} set to {item.quantity} {item.unit_name}.', 'success')
    return redirect(url_for('admin.stock_page'))


@admin_bp.route('/stock/<int:item_id>/settings', methods=['POST'])
def stock_settings(item_id):
    """Unit cost and the level that triggers a warning."""
    item = StockItem.query.get_or_404(item_id)

    # Toner is priced by the cartridge, so let the admin enter it that way and
    # do the per-page division here — that is the number on the box.
    cartridge_cost = (request.form.get('cartridge_cost') or '').strip()
    cartridge_yield = (request.form.get('cartridge_yield') or '').strip()
    unit_cost_raw = (request.form.get('unit_cost') or '').strip()

    try:
        if cartridge_cost and cartridge_yield:
            yield_pages = float(cartridge_yield)
            if yield_pages <= 0:
                raise ValueError('yield')
            item.unit_cost = round(float(cartridge_cost) / yield_pages, 4)
        elif unit_cost_raw:
            item.unit_cost = max(0.0, float(unit_cost_raw))

        threshold = (request.form.get('low_threshold') or '').strip()
        if threshold:
            item.low_threshold = max(0, int(float(threshold)))
    except ValueError:
        flash('Costs and thresholds must be numbers, and the yield above zero.', 'error')
        return redirect(url_for('admin.stock_page'))

    db.session.commit()
    stock_service.check_item(item)
    audit.record('stock.settings', target_type='stock', target_id=item.id,
                 details={'item': item.label, 'unit_cost': item.unit_cost,
                          'low_threshold': item.low_threshold})
    db.session.commit()
    flash(f'{item.label}: ₹{item.unit_cost:.4f} per {item.unit_name[:-1]}, '
          f'warn below {item.low_threshold}.', 'success')
    return redirect(url_for('admin.stock_page'))


@admin_bp.route('/stock/add', methods=['POST'])
def add_stock_item():
    """Track a consumable that is not on the list yet — A3 paper, say."""
    kind = request.form.get('kind', 'paper')
    key = (request.form.get('key') or '').strip()
    if kind not in StockItem.KINDS or not key:
        flash('Pick a type and give it a name.', 'error')
        return redirect(url_for('admin.stock_page'))
    if StockItem.query.filter_by(kind=kind, key=key, printer_id=None).first():
        flash(f'{kind} "{key}" is already tracked.', 'error')
        return redirect(url_for('admin.stock_page'))

    item = stock_service.get_item(kind, key, create=True)
    audit.record('stock.add_item', target_type='stock', target_id=item.id,
                 details={'kind': kind, 'key': key})
    db.session.commit()
    flash(f'Now tracking {item.label}.', 'success')
    return redirect(url_for('admin.stock_page'))


@admin_bp.route('/alerts/<int:alert_id>/dismiss', methods=['POST'])
def dismiss_alert(alert_id):
    """Close an alert by hand — it will come straight back if still true."""
    alert = Alert.query.get_or_404(alert_id)
    alert.is_active = False
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolved_note = f'Dismissed by {current_user.username}'
    db.session.commit()
    audit.record('alert.dismiss', target_type='alert', target_id=alert_id)
    db.session.commit()
    flash('Alert dismissed.', 'info')
    return redirect(request.referrer or url_for('admin.stock_page'))


# --- Kiosks ---------------------------------------------------------------

def _kiosk_view(agent, now=None):
    """Everything the admin page shows about one kiosk."""
    now = now or datetime.now(timezone.utc)
    offline_seconds = current_app.config.get('AGENT_OFFLINE_SECONDS', 60)
    since = agent.seconds_since_heartbeat(now)
    online = since is not None and since < offline_seconds

    if not online:
        state, state_label = 'offline', 'Offline'
    elif agent.activity == 'printing' or (agent.active_job_count or 0) > 0:
        state, state_label = 'printing', 'Printing'
    elif agent.activity == 'error':
        state, state_label = 'error', 'Needs attention'
    else:
        state, state_label = 'idle', 'Idle — ready'

    printing = []
    if agent.printer_id:
        printing = PrintJob.query.filter(
            PrintJob.status == 'printing',
            db.or_(PrintJob.printer_id == agent.printer_id,
                   PrintJob.claimed_by_agent == agent.printer_id),
        ).all()

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_q = PrintJob.query.filter(PrintJob.status == 'completed',
                                    PrintJob.printed_at >= day_start)
    if agent.printer_id:
        # Jobs with no printer_id were routed to whichever agent claimed them.
        today_q = today_q.filter(db.or_(PrintJob.printer_id == agent.printer_id,
                                        PrintJob.claimed_by_agent == agent.printer_id))
    printed_today = today_q.count()

    return {
        'id': agent.id,
        'printer_id': agent.printer_id or 'default',
        'printer_name': agent.printer_name or 'Unknown printer',
        'printer_status': agent.printer_status or 'Unknown',
        'state': state,
        'state_label': state_label,
        'online': online,
        'seconds_since': int(since) if since is not None else None,
        'enrolled': agent.is_enrolled,
        'key_prefix': agent.key_prefix,
        'mac_address': agent.mac_address or '—',
        'hostname': agent.hostname or '—',
        'ip_address': agent.ip_address or '—',
        'platform': agent.platform or '—',
        'agent_version': agent.agent_version or '—',
        'last_error': agent.last_error,
        'active_job_count': agent.active_job_count or 0,
        'printing_now': [{'id': j.id, 'filename': j.filename,
                          'user': j.user.full_name if j.user else 'Unknown'} for j in printing],
        'printed_today': printed_today,
        'display_up': agent.is_kiosk_display_up(now=now),
        'last_heartbeat': agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        'started_at': agent.agent_started_at.isoformat() if agent.agent_started_at else None,
    }


@admin_bp.route('/kiosks')
def kiosks():
    now = datetime.now(timezone.utc)
    agents = AgentStatus.query.order_by(AgentStatus.printer_id.asc()).all()
    install_url = (current_app.config.get('SITE_URL') or request.host_url).rstrip('/')
    return render_template('admin/kiosks.html',
                           kiosks=[_kiosk_view(a, now) for a in agents],
                           offline_seconds=current_app.config.get('AGENT_OFFLINE_SECONDS', 60),
                           codes=enrollment.recent_codes(5),
                           new_code=request.args.get('code'),
                           install_url=install_url)


@admin_bp.route('/kiosks/enroll', methods=['POST'])
def new_kiosk_code():
    """Open a short enrollment window for a new Pi."""
    label = (request.form.get('label') or '').strip()[:120] or None
    minutes = current_app.config.get('ENROLL_CODE_MINUTES', 15)
    entry = enrollment.create_code(created_by=current_user.id, label=label,
                                   ttl_minutes=minutes)
    audit.record('kiosk.enroll_code', target_type='enrollment', target_id=entry.id,
                 details={'label': label, 'expires_in_min': minutes})
    db.session.commit()
    flash(f'Enrollment code {entry.code} — valid for {minutes} minutes.', 'success')
    return redirect(url_for('admin.kiosks', code=entry.code))


@admin_bp.route('/kiosks/<int:agent_id>/revoke', methods=['POST'])
def revoke_kiosk(agent_id):
    """Cut off a kiosk's key without deleting its history."""
    agent = AgentStatus.query.get_or_404(agent_id)
    if not enrollment.revoke(agent, reason=f'revoked by {current_user.username}'):
        flash('That kiosk has no key to revoke — it uses the shared key.', 'error')
        return redirect(url_for('admin.kiosks'))
    audit.record('kiosk.revoke', target_type='agent', target_id=agent_id,
                 details={'printer_id': agent.printer_id})
    db.session.commit()
    flash(f'Revoked the key for {agent.printer_id}. It will stop working immediately.',
          'success')
    return redirect(url_for('admin.kiosks'))


@admin_bp.route('/kiosks/data')
def kiosks_data():
    """JSON feed so the page can refresh without a reload."""
    now = datetime.now(timezone.utc)
    agents = AgentStatus.query.order_by(AgentStatus.printer_id.asc()).all()
    return jsonify([_kiosk_view(a, now) for a in agents])


@admin_bp.route('/kiosks/<int:agent_id>/forget', methods=['POST'])
def forget_kiosk(agent_id):
    """Drop a kiosk that has been decommissioned."""
    agent = AgentStatus.query.get_or_404(agent_id)
    since = agent.seconds_since_heartbeat()
    if since is not None and since < current_app.config.get('AGENT_OFFLINE_SECONDS', 60):
        flash('That kiosk is still online — stop its agent before removing it.', 'error')
        return redirect(url_for('admin.kiosks'))
    label = agent.printer_id or agent.printer_name or agent.id
    db.session.delete(agent)
    db.session.commit()
    audit.record('kiosk.forget', target_type='agent', target_id=agent_id,
                 details={'printer_id': label})
    db.session.commit()
    flash(f'Removed kiosk {label}.', 'info')
    return redirect(url_for('admin.kiosks'))


# --- Offers ---------------------------------------------------------------

@admin_bp.route('/offers')
def offers_page():
    offers.expire_stale_vouchers()
    recent = Referral.query.order_by(Referral.created_at.desc()).limit(25).all()
    return render_template('admin/offers.html',
                           settings=offers.get_settings(),
                           tiers=BulkDiscountTier.query.order_by(
                               BulkDiscountTier.min_pages.asc()).all(),
                           stats=offers.admin_stats(),
                           referrals=recent)


@admin_bp.route('/offers/settings', methods=['POST'])
def offers_settings():
    """Save the referral campaign settings."""
    values = {
        'referral_enabled': '1' if request.form.get('referral_enabled') else '0',
        'bulk_enabled': '1' if request.form.get('bulk_enabled') else '0',
        'offers_stack': '1' if request.form.get('offers_stack') else '0',
        'offers_headline': (request.form.get('offers_headline') or '').strip()[:200],
    }
    numeric = ('referral_friend_percent', 'referral_referrer_percent',
               'referral_max_discount', 'referral_limit', 'referral_reward_days')
    for key in numeric:
        raw = request.form.get(key)
        if raw is None or raw == '':
            continue
        try:
            val = float(raw)
        except ValueError:
            flash(f'"{key}" must be a number.', 'error')
            return redirect(url_for('admin.offers_page'))
        if val < 0:
            flash('Offer values cannot be negative.', 'error')
            return redirect(url_for('admin.offers_page'))
        if key.endswith('_percent') and val > 100:
            flash('A discount cannot exceed 100%.', 'error')
            return redirect(url_for('admin.offers_page'))
        values[key] = str(int(val))

    offers.save_settings(values)
    audit.record('offers.settings', details=values)
    db.session.commit()
    flash('Offer settings saved.', 'success')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/tiers', methods=['POST'])
def create_tier():
    """Add a bulk-printing discount tier."""
    try:
        min_pages = int(request.form.get('min_pages', 0))
        percent = float(request.form.get('discount_percent', 0))
    except ValueError:
        flash('Pages and percent must be numbers.', 'error')
        return redirect(url_for('admin.offers_page'))

    raw_cap = (request.form.get('max_discount') or '').strip()
    try:
        max_discount = float(raw_cap) if raw_cap else None
    except ValueError:
        flash('Maximum discount must be a number.', 'error')
        return redirect(url_for('admin.offers_page'))

    if min_pages < 1:
        flash('A tier needs a page threshold of at least 1.', 'error')
        return redirect(url_for('admin.offers_page'))
    if not 0 < percent <= 100:
        flash('Discount must be between 0 and 100%.', 'error')
        return redirect(url_for('admin.offers_page'))
    if BulkDiscountTier.query.filter_by(min_pages=min_pages).first():
        flash(f'A tier for {min_pages}+ pages already exists — edit it instead.', 'error')
        return redirect(url_for('admin.offers_page'))

    tier = BulkDiscountTier(
        min_pages=min_pages,
        discount_percent=percent,
        max_discount=max_discount,
        label=(request.form.get('label') or '').strip()[:120] or None,
    )
    db.session.add(tier)
    db.session.commit()
    audit.record('offers.tier_create', target_type='tier', target_id=tier.id,
                 details={'min_pages': min_pages, 'percent': percent})
    db.session.commit()
    flash(f'Added tier: {min_pages}+ pages — {percent:g}% off.', 'success')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/tiers/<int:tier_id>/update', methods=['POST'])
def update_tier(tier_id):
    tier = BulkDiscountTier.query.get_or_404(tier_id)
    try:
        tier.min_pages = max(1, int(request.form.get('min_pages', tier.min_pages)))
        percent = float(request.form.get('discount_percent', tier.discount_percent))
    except ValueError:
        flash('Pages and percent must be numbers.', 'error')
        return redirect(url_for('admin.offers_page'))
    if not 0 < percent <= 100:
        flash('Discount must be between 0 and 100%.', 'error')
        return redirect(url_for('admin.offers_page'))
    tier.discount_percent = percent

    raw_cap = (request.form.get('max_discount') or '').strip()
    try:
        tier.max_discount = float(raw_cap) if raw_cap else None
    except ValueError:
        flash('Maximum discount must be a number.', 'error')
        return redirect(url_for('admin.offers_page'))

    tier.label = (request.form.get('label') or '').strip()[:120] or None
    db.session.commit()
    audit.record('offers.tier_update', target_type='tier', target_id=tier.id)
    db.session.commit()
    flash('Tier updated.', 'success')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/tiers/<int:tier_id>/toggle', methods=['POST'])
def toggle_tier(tier_id):
    tier = BulkDiscountTier.query.get_or_404(tier_id)
    tier.is_active = not tier.is_active
    db.session.commit()
    audit.record('offers.tier_toggle', target_type='tier', target_id=tier.id,
                 details={'is_active': tier.is_active})
    db.session.commit()
    flash(f'Tier {"activated" if tier.is_active else "deactivated"}.', 'info')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/tiers/<int:tier_id>/delete', methods=['POST'])
def delete_tier(tier_id):
    tier = BulkDiscountTier.query.get_or_404(tier_id)
    db.session.delete(tier)
    db.session.commit()
    audit.record('offers.tier_delete', target_type='tier', target_id=tier_id)
    db.session.commit()
    flash('Tier deleted.', 'info')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/voucher', methods=['POST'])
def grant_voucher():
    """Hand a one-off discount to a specific user."""
    username = (request.form.get('username') or '').strip().lower()
    user = User.query.filter_by(username=username).first()
    if user is None:
        flash(f'No user named "{username}".', 'error')
        return redirect(url_for('admin.offers_page'))
    try:
        percent = float(request.form.get('percent', 0))
        raw_cap = (request.form.get('cap') or '').strip()
        cap = float(raw_cap) if raw_cap else None
    except ValueError:
        flash('Percent and cap must be numbers.', 'error')
        return redirect(url_for('admin.offers_page'))
    if not 0 < percent <= 100:
        flash('Discount must be between 0 and 100%.', 'error')
        return redirect(url_for('admin.offers_page'))

    note = (request.form.get('description') or '').strip()[:200]
    voucher = offers.grant_voucher(user, 'manual', percent, cap,
                                   note or f'{percent:g}% off — from the shop')
    if voucher is None:
        flash(f'{user.full_name} is a walk-in guest — offers are for registered '
              f'accounts only. Ask them to register first.', 'error')
        return redirect(url_for('admin.offers_page'))
    audit.record('offers.voucher_grant', target_type='user', target_id=user.id,
                 details={'percent': percent, 'cap': cap, 'voucher': voucher.id})
    db.session.commit()
    flash(f'Gave {user.full_name} a {percent:g}% voucher.', 'success')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/offers/voucher/<int:voucher_id>/revoke', methods=['POST'])
def revoke_voucher(voucher_id):
    voucher = DiscountVoucher.query.get_or_404(voucher_id)
    if voucher.status != 'available':
        flash('Only unused vouchers can be revoked.', 'error')
        return redirect(url_for('admin.offers_page'))
    voucher.status = 'revoked'
    db.session.commit()
    audit.record('offers.voucher_revoke', target_type='voucher', target_id=voucher_id)
    db.session.commit()
    flash('Voucher revoked.', 'info')
    return redirect(url_for('admin.offers_page'))


@admin_bp.route('/users')
def users():
    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=all_users)


@admin_bp.route('/password-resets')
def password_resets():
    """Requests waiting on staff, plus the codes currently live."""
    from app.services import password_reset
    password_reset.expire_stale()
    return render_template('admin/password_resets.html',
                           pending=password_reset.pending_requests(),
                           live=password_reset.live_codes(),
                           email_enabled=mailer.is_configured(),
                           issued_code=request.args.get('code'),
                           issued_for=request.args.get('for'))


@admin_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
def issue_password_reset(user_id):
    """Hand a customer a reset code across the counter.

    The code comes back exactly once, in the redirect, and is never stored in
    plaintext. Clicking again issues a new one and voids this.
    """
    from app.services import password_reset
    user = User.query.get_or_404(user_id)
    if user.is_guest:
        flash('Guest accounts are temporary — there is no password worth resetting.',
              'error')
        return redirect(url_for('admin.user_detail', user_id=user_id))

    code, _row = password_reset.issue_at_counter(user, staff_user=current_user)
    audit.record('password.reset_issued', target_type='user', target_id=user.id,
                 details={'by': current_user.username})
    db.session.commit()

    back = request.form.get('back')
    target = (url_for('admin.password_resets') if back == 'list'
              else url_for('admin.user_detail', user_id=user_id))
    return redirect(f'{target}?code={code}&for={user.username}')


@admin_bp.route('/users/<int:user_id>')
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    jobs = PrintJob.query.filter_by(user_id=user_id).order_by(
        PrintJob.submitted_at.desc()).all()
    ledger = PaymentLedger.query.filter_by(user_id=user_id).order_by(
        PaymentLedger.created_at.desc()).all()
    return render_template('admin/user_detail.html', user=user, jobs=jobs, ledger=ledger)


@admin_bp.route('/users/<int:user_id>/payment', methods=['POST'])
def record_payment(user_id):
    user = User.query.get_or_404(user_id)
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        amount = 0
    if amount <= 0:
        flash('Invalid amount.', 'error')
        return redirect(url_for('admin.user_detail', user_id=user_id))

    user.balance_owed = (user.balance_owed or 0) - amount
    ledger = PaymentLedger(
        user_id=user_id, amount=-amount,
        description='Payment received',
        recorded_by=current_user.id,
        entry_type='payment',
    )
    db.session.add(ledger)
    audit.record('payment.record', target_type='user', target_id=user_id,
                 details={'amount': amount})
    db.session.commit()
    flash(f'Recorded ₹{amount:.2f} payment from {user.full_name}.', 'success')
    return redirect(url_for('admin.user_detail', user_id=user_id))


@admin_bp.route('/users/<int:user_id>/refund', methods=['POST'])
def issue_refund(user_id):
    user = User.query.get_or_404(user_id)
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        amount = 0
    reason = request.form.get('reason', '').strip() or 'Manual refund'
    if amount <= 0:
        flash('Invalid amount.', 'error')
        return redirect(url_for('admin.user_detail', user_id=user_id))

    user.balance_owed = (user.balance_owed or 0) - amount
    db.session.add(PaymentLedger(
        user_id=user_id, amount=-amount,
        description=f'Refund: {reason}',
        recorded_by=current_user.id,
        entry_type='refund',
    ))
    audit.record('payment.refund', target_type='user', target_id=user_id,
                 details={'amount': amount, 'reason': reason})
    db.session.commit()
    flash(f'Refunded ₹{amount:.2f}.', 'success')
    return redirect(url_for('admin.user_detail', user_id=user_id))


@admin_bp.route('/users/<int:user_id>/toggle', methods=['POST'])
def toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Cannot deactivate yourself.', 'error')
    else:
        user.is_active_user = not user.is_active_user
        audit.record('user.toggle', target_type='user', target_id=user.id,
                     details={'is_active': user.is_active_user})
        db.session.commit()
        status = 'activated' if user.is_active_user else 'deactivated'
        flash(f'{user.full_name} {status}.', 'info')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/guest', methods=['POST'])
def create_guest():
    """Issue a walk-in guest account with a one-time printable code."""
    label = request.form.get('label', '').strip() or None
    hours = 4
    try:
        hours = int(request.form.get('hours', 4))
    except ValueError:
        pass
    user, password = guest_service.create_guest(label=label, hours=hours)
    audit.record('user.create_guest', target_type='user', target_id=user.id,
                 details={'hours': hours})
    db.session.commit()
    flash(f'Guest created — username: {user.username}, password: {password} '
          f'(valid {hours}h). Share these once.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/direct-print', methods=['GET', 'POST'])
def direct_print():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected.', 'error')
            return redirect(url_for('admin.direct_print'))

        file = request.files['file']
        if not file.filename or not allowed_file(file.filename):
            flash('Invalid file.', 'error')
            return redirect(url_for('admin.direct_print'))

        try:
            info = save_upload(file)
            ext = info['extension']
            pdf_path = info.get('pdf_path', info['file_path'])
            file_path = pdf_path if ext in CONVERTIBLE_EXTENSIONS and os.path.exists(pdf_path) else info['file_path']

            job = PrintJob(
                user_id=current_user.id,
                filename=info['filename'], stored_filename=info['stored_filename'],
                file_path=file_path, file_size=info['file_size'],
                page_count=info['page_count'],
            )
            errors = validate_and_apply(request.form, job)
            if errors:
                for _, msg in errors:
                    flash(msg, 'error')
                return redirect(url_for('admin.direct_print'))
            # Bulk tiers apply to a walk-in job, but never silently spend the
            # admin's own referral voucher on it.
            offers.apply_to_job(job, voucher=None, commit=False)
            job.cost_locked = True
            db.session.add(job)
            db.session.commit()

            if current_app.config.get('CLOUD_MODE', False):
                job.status = 'ready_to_print'
                job.last_status_at = datetime.now(timezone.utc)
                db.session.commit()
                flash(f'Sent to printer: {info["filename"]} (₹{job.cost:.2f})', 'success')
            else:
                from app.services.printer import submit_job
                cups_job_id = submit_job(file_path,
                                          f'Direct #{job.id}: {info["filename"]}', job)
                job.cups_job_id = cups_job_id
                job.status = 'printing'
                job.last_status_at = datetime.now(timezone.utc)
                db.session.commit()
                flash(f'Sent to printer: {info["filename"]} (CUPS #{cups_job_id})', 'success')
            audit.record('job.direct_print', target_type='job', target_id=job.id)
            db.session.commit()
        except Exception as e:
            flash(f'Error: {e}', 'error')

        return redirect(url_for('admin.direct_print'))

    return render_template('admin/direct_print.html')


@admin_bp.route('/print-lock', methods=['POST'], endpoint='print_lock')
def print_lock_route():
    """Toggle print lock on/off and set password (hashed)."""
    action = request.form.get('action', 'toggle')

    if action == 'enable':
        password = request.form.get('lock_password', '').strip()
        if not password:
            flash('Please set a password for the print lock.', 'error')
            return redirect(url_for('admin.dashboard'))
        print_lock.enable(password)
        audit.record('print_lock.enable')
        db.session.commit()
        flash('Print lock enabled.', 'success')
    elif action == 'disable':
        print_lock.disable()
        audit.record('print_lock.disable')
        db.session.commit()
        flash('Print lock disabled.', 'info')
    elif action == 'update_password':
        password = request.form.get('lock_password', '').strip()
        if not password:
            flash('Password cannot be empty.', 'error')
            return redirect(url_for('admin.dashboard'))
        print_lock.set_password(password)
        audit.record('print_lock.update_password')
        db.session.commit()
        flash('Print lock password updated.', 'success')

    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/audit')
def audit_log():
    """View recent admin actions."""
    page = max(1, int(request.args.get('page', 1)))
    per_page = 100
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(
        per_page).offset((page - 1) * per_page).all()
    return render_template('admin/audit.html', entries=entries, page=page)


# --- Ads Management ---

@admin_bp.route('/ads')
def ads():
    all_ads = Advertisement.query.order_by(
        Advertisement.display_order.asc(), Advertisement.created_at.desc()).all()
    return render_template('admin/ads.html', ads=all_ads,
                           media_types=Advertisement.MEDIA_TYPES)


@admin_bp.route('/ads/create', methods=['POST'])
def create_ad():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('admin.ads'))

    media_type = request.form.get('media_type', 'text')
    if media_type not in Advertisement.MEDIA_TYPES:
        flash('Unknown slide type.', 'error')
        return redirect(url_for('admin.ads'))

    ad = Advertisement(title=title, content=content, created_by=current_user.id,
                       media_type=media_type)
    _apply_ad_fields(ad, request.form)

    upload = request.files.get('media')
    if upload is None or not upload.filename:
        upload = request.files.get('image')  # older form field name

    if media_type in ad_service.UPLOAD_TYPES:
        if upload is None or not upload.filename:
            flash(f'A {media_type} slide needs a file.', 'error')
            return redirect(url_for('admin.ads'))
        try:
            stored, detected, mime, pages = ad_service.save_media(upload, media_type)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('admin.ads'))
        ad.media_filename = stored
        ad.media_mime = mime
        ad.media_pages = pages
        if detected == 'image':
            ad.image_filename = stored  # keeps old kiosk builds working
    elif media_type == 'html' and not ad.html_content:
        flash('An HTML slide needs some markup.', 'error')
        return redirect(url_for('admin.ads'))
    elif media_type == 'url' and not ad.link_url:
        flash('A web page slide needs a URL.', 'error')
        return redirect(url_for('admin.ads'))

    db.session.add(ad)
    db.session.commit()
    audit.record('ad.create', target_type='ad', target_id=ad.id,
                 details={'title': title, 'type': media_type})
    db.session.commit()
    flash(f'{media_type.upper()} slide created.', 'success')
    return redirect(url_for('admin.ads'))


def _apply_ad_fields(ad, form):
    """Shared parsing of the fields that are not the uploaded file."""
    ad.html_content = (form.get('html_content') or '').strip() or None

    link = (form.get('link_url') or '').strip()
    if link and not link.startswith(('http://', 'https://')):
        link = 'https://' + link
    ad.link_url = link[:500] or None

    try:
        ad.duration_seconds = max(2, min(120, int(form.get('duration_seconds') or 6)))
    except (TypeError, ValueError):
        ad.duration_seconds = 6
    try:
        ad.display_order = int(form.get('display_order') or 0)
    except (TypeError, ValueError):
        ad.display_order = 0


@admin_bp.route('/ads/<int:ad_id>/update', methods=['POST'])
def update_ad(ad_id):
    """Edit a slide's text, timing and order. The media file is not replaced here."""
    ad = Advertisement.query.get_or_404(ad_id)
    title = (request.form.get('title') or '').strip()
    if title:
        ad.title = title[:255]
    ad.content = (request.form.get('content') or '').strip() or None
    _apply_ad_fields(ad, request.form)
    db.session.commit()
    audit.record('ad.update', target_type='ad', target_id=ad.id)
    db.session.commit()
    flash('Slide updated.', 'success')
    return redirect(url_for('admin.ads'))


@admin_bp.route('/ads/<int:ad_id>/toggle', methods=['POST'])
def toggle_ad(ad_id):
    ad = Advertisement.query.get_or_404(ad_id)
    ad.is_active = not ad.is_active
    audit.record('ad.toggle', target_type='ad', target_id=ad.id,
                 details={'is_active': ad.is_active})
    db.session.commit()
    flash(f"Ad {'activated' if ad.is_active else 'deactivated'}.", 'info')
    return redirect(url_for('admin.ads'))


@admin_bp.route('/ads/<int:ad_id>/delete', methods=['POST'])
def delete_ad(ad_id):
    ad = Advertisement.query.get_or_404(ad_id)
    if ad.stored_file:
        ad_service.delete_media(ad.stored_file, ad.media_pages)
        if ad.image_filename and ad.image_filename != ad.media_filename:
            ad_service.delete_media(ad.image_filename)
    audit.record('ad.delete', target_type='ad', target_id=ad.id)
    db.session.delete(ad)
    db.session.commit()
    flash('Ad deleted.', 'info')
    return redirect(url_for('admin.ads'))
