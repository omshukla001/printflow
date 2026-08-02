import os
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, send_file, current_app, request
from flask_login import login_required, current_user
from sqlalchemy import text as sql_text
from app.extensions import db
from app.models import PrintJob, Advertisement, AgentStatus
from app.services.queue_manager import get_ordered_queue
from app.services.pricing import calculate_cost, get_price_per_page
from app.services.receipts import generate_receipt
from app.services.sse import stream_job_status
from app.services import offers, ads as ad_service
from app.services.print_options import (count_pages_in_range, normalize_page_ranges,
                                        color_enabled)

api_bp = Blueprint('api', __name__)


@api_bp.route('/preview/<int:job_id>/<int:page>')
@login_required
def preview_image(job_id, page):
    job = PrintJob.query.get_or_404(job_id)
    if job.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403

    if job.files_purged_at is not None:
        return jsonify({'error': 'File deleted after printing'}), 410

    preview_dir = os.path.join(current_app.config['PREVIEW_FOLDER'], str(job_id))
    preview_file = os.path.join(preview_dir, f'page_{page}.png')
    if not os.path.exists(preview_file):
        return jsonify({'error': 'Preview not found'}), 404

    return send_file(preview_file, mimetype='image/png',
                     max_age=3600)


@api_bp.route('/job/<int:job_id>/status')
@login_required
def job_status(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if job.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    return jsonify(job.to_dict())


@api_bp.route('/job/<int:job_id>/receipt')
@login_required
def job_receipt(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if job.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    if job.status != 'completed':
        return jsonify({'error': 'Receipt only available for completed jobs'}), 400

    if not job.receipt_filename:
        fname = generate_receipt(job)
        if fname:
            job.receipt_filename = fname
            db.session.commit()
    if not job.receipt_filename:
        return jsonify({'error': 'Receipt generation failed'}), 500

    path = os.path.join(current_app.config['RECEIPT_FOLDER'], job.receipt_filename)
    if not os.path.exists(path):
        # File missing — regenerate
        fname = generate_receipt(job)
        if not fname:
            return jsonify({'error': 'Receipt generation failed'}), 500
        job.receipt_filename = fname
        db.session.commit()
        path = os.path.join(current_app.config['RECEIPT_FOLDER'], fname)

    return send_file(path, as_attachment=True,
                     download_name=f'PrintFlow_Receipt_{job.id}.pdf',
                     mimetype='application/pdf')


@api_bp.route('/jobs/stream')
@login_required
def jobs_stream():
    """SSE stream of the current user's active job statuses."""
    return stream_job_status(current_user.id)


@api_bp.route('/queue')
@login_required
def queue_status():
    if not current_user.is_admin:
        return jsonify({'error': 'Admin required'}), 403
    jobs = get_ordered_queue()
    return jsonify([j.to_dict() for j in jobs])


@api_bp.route('/pricing/calculate')
@login_required
def calculate_price():
    """Live cost estimate honoring all print options."""
    try:
        pages = max(1, int(request.args.get('pages', 1)))
        copies = max(1, int(request.args.get('copies', 1)))
        nup = int(request.args.get('pages_per_sheet', 1))
    except ValueError:
        return jsonify({'error': 'Invalid params'}), 400
    paper = request.args.get('paper_size', 'A4')
    color = request.args.get('color_mode', 'bw')
    # Keep the estimate honest: if colour is switched off the job will be
    # forced to mono on submit, so quote the mono rate here too.
    if color == 'color' and not color_enabled():
        color = 'bw'
    sides = request.args.get('sides', 'one-sided')
    page_set = request.args.get('page_set', 'all')
    page_ranges_raw = request.args.get('page_ranges', '').strip() or None
    range_error = None
    try:
        page_ranges = normalize_page_ranges(page_ranges_raw, max_page=pages)
    except ValueError as e:
        range_error = str(e)
        page_ranges = None

    base_pages = count_pages_in_range(page_ranges, pages)
    if page_set == 'odd':
        base_pages = (base_pages + 1) // 2
    elif page_set == 'even':
        base_pages = base_pages // 2

    total_pages = base_pages * copies
    if nup in (1, 2, 4, 6, 9) and nup > 1:
        sheets = (total_pages + nup - 1) // nup
    else:
        sheets = total_pages
    if sides == 'two-sided':
        sheets = (sheets + 1) // 2

    price = get_price_per_page(paper, color, sides)
    base_cost = round(price * sheets, 2)

    # Quote the offers too, so the price on screen is the price charged.
    quote = offers.quote(base_cost, total_pages, user=current_user)

    return jsonify({
        'cost': quote['total'],
        'base_cost': base_cost,
        'discount': quote['discount_amount'],
        'discount_label': quote['label'],
        'discount_lines': [{'label': l['label'], 'amount': l['amount']}
                           for l in quote['lines']],
        'sheets': sheets,
        'rate': price,
        'color_mode': color,
        'effective_pages': base_pages,
        'normalized_range': page_ranges,
        'range_error': range_error,
    })


@api_bp.route('/user/jobs')
@login_required
def user_jobs():
    jobs = PrintJob.query.filter(
        PrintJob.user_id == current_user.id,
        PrintJob.status.in_(['queued', 'prioritized', 'printing', 'ready_to_print'])
    ).order_by(PrintJob.submitted_at.desc()).all()
    return jsonify([j.to_dict() for j in jobs])


@api_bp.route('/ad-media/<int:ad_id>')
def ad_media(ad_id):
    """Stream an ad's file — image, video or the raw PDF."""
    ad = Advertisement.query.get_or_404(ad_id)
    path = ad_service.media_path(ad.stored_file)
    if path is None:
        return jsonify({'error': 'No media for this ad'}), 404
    # conditional=True gives the video element range requests, so seeking and
    # looping work instead of re-downloading the whole file.
    return send_file(path, mimetype=ad.media_mime or None,
                     max_age=3600, conditional=True)


@api_bp.route('/ad-media/<int:ad_id>/page/<int:page>')
def ad_media_page(ad_id, page):
    """One rendered page of a PDF ad."""
    ad = Advertisement.query.get_or_404(ad_id)
    if not ad.stored_file or page < 1 or page > (ad.media_pages or 0):
        return jsonify({'error': 'No such page'}), 404
    path = ad_service.media_path(ad_service.page_name(ad.stored_file, page))
    if path is None:
        return jsonify({'error': 'Page not found'}), 404
    return send_file(path, mimetype='image/png', max_age=3600)


@api_bp.route('/ad-image/<int:ad_id>')
def ad_image(ad_id):
    """Back-compat alias — the kiosk used to fetch images from this path."""
    return ad_media(ad_id)


@api_bp.route('/ads/active')
def active_ads():
    ads = Advertisement.query.filter_by(is_active=True).order_by(
        Advertisement.display_order.asc(), Advertisement.created_at.desc()).all()
    return jsonify([a.to_kiosk_dict() for a in ads])


# --- Health endpoint (public, no auth) ---

@api_bp.route('/healthz')
def healthz():
    """Liveness + readiness probe.

    Returns 200 with details if DB reachable; 503 otherwise. Includes a freshness
    check on agent heartbeats in cloud mode.
    """
    db_ok = True
    db_error = None
    try:
        db.session.execute(sql_text('SELECT 1'))
    except Exception as e:
        db_ok = False
        db_error = str(e)

    agents_payload = []
    offline_seconds = current_app.config.get('AGENT_OFFLINE_SECONDS', 60)
    now = datetime.now(timezone.utc)
    try:
        for a in AgentStatus.query.all():
            fresh = bool(a.last_heartbeat and
                         (now - (a.last_heartbeat.replace(tzinfo=timezone.utc)
                                 if a.last_heartbeat.tzinfo is None else a.last_heartbeat))
                         < timedelta(seconds=offline_seconds))
            agents_payload.append({
                'printer_id': a.printer_id,
                'printer_name': a.printer_name,
                'fresh': fresh,
                'last_heartbeat': a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            })
    except Exception:
        pass

    payload = {
        'status': 'ok' if db_ok else 'error',
        'db': db_ok,
        'db_error': db_error,
        'cloud_mode': current_app.config.get('CLOUD_MODE', False),
        'agents': agents_payload,
        'time': now.isoformat(),
    }
    return jsonify(payload), (200 if db_ok else 503)
