import io
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file
from flask_login import login_required, current_user
from app.extensions import db
from app.models import PrintJob, Advertisement
from app.services.file_handler import (
    allowed_file, save_upload, generate_previews, get_extension, CONVERTIBLE_EXTENSIONS,
)
from app.services.pricing import calculate_cost, get_all_pricing, lock_job_cost
from app.services.queue_manager import (
    get_user_queue_position, get_user_active_count, cancel_job as cancel_print_job,
)
from app.services.qr_service import generate_qr_png, get_qr_data_for_user
from app.services.print_options import validate_and_apply, effective_pages, effective_sheets
from app.services import audit, print_lock, preview_worker, offers
from app.models import PrintPreset

user_bp = Blueprint('user', __name__)


@user_bp.before_request
@login_required
def require_login():
    pass


@user_bp.route('/dashboard')
def dashboard():
    active_jobs = PrintJob.query.filter(
        PrintJob.user_id == current_user.id,
        PrintJob.status.in_(['queued', 'prioritized', 'printing'])
    ).order_by(PrintJob.submitted_at.desc()).all()

    queue_pos = get_user_queue_position(current_user.id)
    pricing = get_all_pricing()
    active_ads = Advertisement.query.filter_by(is_active=True).order_by(
        Advertisement.created_at.desc()).all()

    offers.expire_stale_vouchers()
    summary = offers.user_summary(current_user)
    share_base = (current_app.config.get('SITE_URL') or request.host_url).rstrip('/')

    from app.services.pricing import get_price_per_page
    return render_template('user/dashboard.html',
                           active_jobs=active_jobs,
                           queue_position=queue_pos,
                           pricing=pricing,
                           rate_simplex=get_price_per_page('A4', 'bw', 'one-sided'),
                           rate_duplex=get_price_per_page('A4', 'bw', 'two-sided'),
                           ads=active_ads,
                           offers=summary,
                           share_url=f"{share_base}{url_for('auth.register')}?ref={summary['code']}")


@user_bp.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        # Concurrency cap per user
        max_active = current_app.config.get('USER_MAX_ACTIVE_JOBS', 10)
        if get_user_active_count(current_user.id) >= max_active:
            flash(f'You have {max_active} active jobs — finish or cancel one before uploading.',
                  'error')
            return redirect(url_for('user.dashboard'))

        if 'file' not in request.files:
            flash('No file selected.', 'error')
            return redirect(url_for('user.upload'))

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return redirect(url_for('user.upload'))

        if not allowed_file(file.filename):
            flash('File type not allowed. Use PDF, PNG, JPG, DOCX, DOC, or TXT.', 'error')
            return redirect(url_for('user.upload'))

        try:
            info = save_upload(file)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('user.upload'))
        except Exception as e:
            flash(f'Upload failed: {e}', 'error')
            return redirect(url_for('user.upload'))

        ext = info['extension']
        pdf_path = info.get('pdf_path', info['file_path'])

        job = PrintJob(
            user_id=current_user.id,
            filename=info['filename'],
            stored_filename=info['stored_filename'],
            file_path=pdf_path if ext in CONVERTIBLE_EXTENSIONS and os.path.exists(pdf_path) else info['file_path'],
            file_size=info['file_size'],
            page_count=info['page_count'],
            status='queued',
            preview_status='pending',
        )
        db.session.add(job)
        db.session.commit()

        # Async preview generation — returns immediately
        preview_path = pdf_path if os.path.exists(pdf_path) else info['file_path']
        preview_ext = ext if ext not in CONVERTIBLE_EXTENSIONS else 'pdf'
        preview_worker.enqueue(job.id, preview_path, preview_ext)

        return redirect(url_for('user.configure_job', job_id=job.id))

    return render_template('user/upload.html')


@user_bp.route('/job/<int:job_id>/configure', methods=['GET', 'POST'])
def configure_job(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('user.dashboard'))

    pricing = get_all_pricing()
    lock_enabled = print_lock.is_enabled()
    presets = PrintPreset.query.filter_by(user_id=current_user.id).order_by(
        PrintPreset.name.asc()).all()

    if request.method == 'POST':
        if lock_enabled:
            submitted_code = request.form.get('lock_password', '').strip()
            if not print_lock.check_password(submitted_code):
                flash('Incorrect print code. Ask the admin for the code.', 'error')
                return redirect(url_for('user.configure_job', job_id=job.id))

        # Apply preset first if requested
        preset_id = request.form.get('apply_preset')
        if preset_id:
            try:
                preset = PrintPreset.query.filter_by(
                    id=int(preset_id), user_id=current_user.id).first()
                if preset:
                    preset.apply_to(job)
            except (ValueError, TypeError):
                pass

        errors = validate_and_apply(request.form, job)
        if errors:
            for _, msg in errors:
                flash(msg, 'error')
            db.session.rollback()
            return redirect(url_for('user.configure_job', job_id=job.id))

        quote = offers.apply_to_job(job, commit=False)
        job.cost_locked = True
        job.status = 'queued'
        db.session.commit()
        audit.record('job.submit', target_type='job', target_id=job.id,
                     details={'cost': job.cost,
                              'base_cost': quote['base_cost'],
                              'discount': quote['discount_amount'],
                              'pages': job.page_count,
                              'copies': job.copies,
                              'page_ranges': job.page_ranges,
                              'pages_per_sheet': job.pages_per_sheet,
                              'page_set': job.page_set})
        db.session.commit()

        # Save as preset?
        save_name = (request.form.get('save_preset_name') or '').strip()
        if save_name:
            existing = PrintPreset.query.filter_by(
                user_id=current_user.id, name=save_name).first()
            if existing:
                preset = existing
            else:
                preset = PrintPreset(user_id=current_user.id, name=save_name[:80])
                db.session.add(preset)
            preset.copies = job.copies
            preset.color_mode = job.color_mode
            preset.paper_size = job.paper_size
            preset.sides = job.sides
            preset.pages_per_sheet = job.pages_per_sheet
            preset.page_set = job.page_set
            preset.output_order = job.output_order
            preset.orientation = job.orientation
            preset.fit_to_page = job.fit_to_page
            preset.print_quality = job.print_quality
            preset.collate = job.collate
            db.session.commit()
            flash(f'Saved preset "{preset.name}".', 'info')

        saved = ''
        if quote['discount_amount'] > 0:
            saved = f" — you saved ₹{quote['discount_amount']:.2f} ({quote['label']})"
        flash(f'Job submitted! Cost: ₹{job.cost:.2f}'
              f' ({effective_pages(job)} page-impressions, {effective_sheets(job)} sheets){saved}.',
              'success')
        return redirect(url_for('user.dashboard'))

    preview_count = job.preview_pages or 0
    default_cost = calculate_cost(job.page_count, 1, 'A4', 'bw')

    # Per-sheet rates shown on the sides selector so the cost is legible before
    # the live estimate comes back.
    from app.services.print_options import color_enabled
    from app.services.pricing import get_price_per_page
    rate_simplex = get_price_per_page('A4', 'bw', 'one-sided')
    rate_duplex = get_price_per_page('A4', 'bw', 'two-sided')

    return render_template('user/configure.html',
                           job=job,
                           pricing=pricing,
                           presets=presets,
                           preview_count=preview_count,
                           preview_status=job.preview_status,
                           default_cost=default_cost,
                           color_enabled=color_enabled(),
                           rate_simplex=rate_simplex,
                           rate_duplex=rate_duplex,
                           lock_enabled=lock_enabled)


@user_bp.route('/job/<int:job_id>/reprint', methods=['POST'])
def reprint(job_id):
    """Clone a previous job with the same settings — new PrintJob row in 'queued'."""
    source = PrintJob.query.get_or_404(job_id)
    if source.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('user.history'))

    max_active = current_app.config.get('USER_MAX_ACTIVE_JOBS', 10)
    if get_user_active_count(current_user.id) >= max_active:
        flash(f'You have {max_active} active jobs — finish or cancel one before reprinting.',
              'error')
        return redirect(url_for('user.history'))

    if source.files_purged_at is not None or not os.path.exists(source.file_path):
        flash('The file was deleted from the server after printing — upload it again to reprint.',
              'error')
        return redirect(url_for('user.history'))

    job = PrintJob(
        user_id=current_user.id,
        filename=source.filename,
        stored_filename=source.stored_filename,
        file_path=source.file_path,
        file_size=source.file_size,
        page_count=source.page_count,
        copies=source.copies,
        color_mode=source.color_mode,
        paper_size=source.paper_size,
        sides=source.sides,
        page_ranges=source.page_ranges,
        pages_per_sheet=source.pages_per_sheet,
        page_set=source.page_set,
        output_order=source.output_order,
        orientation=source.orientation,
        fit_to_page=source.fit_to_page,
        print_quality=source.print_quality,
        collate=source.collate,
        status='queued',
        preview_status=source.preview_status,
        preview_pages=source.preview_pages,
    )
    db.session.add(job)
    db.session.commit()
    # Priced after the insert so a consumed voucher can point at the new job.
    offers.apply_to_job(job)
    job.cost_locked = True
    db.session.commit()
    audit.record('job.reprint', target_type='job', target_id=job.id,
                 details={'from': source.id, 'cost': job.cost})
    db.session.commit()
    flash(f'Reprint queued: {job.filename} — ₹{job.cost:.2f}', 'success')
    return redirect(url_for('user.dashboard'))


# --- Presets ---

@user_bp.route('/presets', methods=['GET'])
def presets():
    items = PrintPreset.query.filter_by(user_id=current_user.id).order_by(
        PrintPreset.name.asc()).all()
    return render_template('user/presets.html', presets=items)


@user_bp.route('/presets/<int:preset_id>/delete', methods=['POST'])
def delete_preset(preset_id):
    p = PrintPreset.query.filter_by(id=preset_id, user_id=current_user.id).first_or_404()
    db.session.delete(p)
    db.session.commit()
    flash(f'Deleted preset "{p.name}".', 'info')
    return redirect(url_for('user.presets'))


@user_bp.route('/job/<int:job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    job = PrintJob.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('user.dashboard'))

    if job.status in ('queued', 'prioritized'):
        cancel_print_job(job, by_user_id=current_user.id)
        audit.record('job.user_cancel', target_type='job', target_id=job.id)
        db.session.commit()
        flash('Job cancelled.', 'info')
    else:
        flash('Cannot cancel this job.', 'error')

    return redirect(url_for('user.dashboard'))


@user_bp.route('/history')
def history():
    jobs = PrintJob.query.filter_by(user_id=current_user.id).order_by(
        PrintJob.submitted_at.desc()
    ).all()
    return render_template('user/history.html', jobs=jobs)


@user_bp.route('/scan')
def scan():
    return render_template('user/scan.html')


@user_bp.route('/qr')
def qr_code():
    return render_template('user/qr.html')


@user_bp.route('/qr/image')
def qr_image():
    data = get_qr_data_for_user(current_user)
    png_bytes = generate_qr_png(data, box_size=12, border=2)
    return send_file(
        io.BytesIO(png_bytes),
        mimetype='image/png',
        download_name='qr.png'
    )
