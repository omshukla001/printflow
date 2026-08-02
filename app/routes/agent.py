"""Agent API — endpoints for the RPi print agent to poll and report status."""
import os
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, jsonify, request, current_app, send_file, g

from app.extensions import db, csrf, limiter
from app.models import PrintJob, AgentStatus, User
from app.services.queue_manager import (
    complete_job, fail_job, mark_printing, claim_pending_jobs, TERMINAL_STATES,
)
from app.services.print_options import to_dict as options_to_dict

agent_bp = Blueprint('agent', __name__)
csrf.exempt(agent_bp)  # agent uses API key, not browser cookies


def require_agent_key(f):
    """Authenticate the caller by its X-Agent-Key header.

    Two kinds of key are accepted: the per-device key a kiosk received when it
    enrolled, and the shared AGENT_API_KEY from the older hand-configured setup.
    Both comparisons are constant-time.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from app.services import enrollment

        master = current_app.config.get('AGENT_API_KEY', '')
        presented = request.headers.get('X-Agent-Key')
        agent, is_master = enrollment.authenticate(presented, master)

        if agent is None and not is_master:
            return jsonify({'error': 'Unauthorized'}), 401

        # Remembered for the request so identity comes from the credential
        # rather than a header the caller could put anything in.
        g.agent_row = agent
        g.agent_is_master = is_master
        return f(*args, **kwargs)
    return decorated


def _agent_id():
    """Identifier of the calling agent.

    An enrolled kiosk is identified by its key, so the header is only trusted
    for the legacy shared key — where there is nothing better to go on.
    """
    agent = getattr(g, 'agent_row', None)
    if agent is not None and agent.printer_id:
        return agent.printer_id
    return request.headers.get('X-Agent-Id', 'default')


def _printer_id():
    agent = getattr(g, 'agent_row', None)
    if agent is not None and agent.printer_id:
        return agent.printer_id
    return request.headers.get('X-Printer-Id') or _agent_id()


@agent_bp.route('/enroll', methods=['POST'])
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ENROLL', '10 per hour'))
def enroll():
    """Exchange an enrollment code for this device's own key.

    Deliberately unauthenticated — the code *is* the credential, which is why it
    is short-lived, single-use and rate limited.
    """
    from app.services import enrollment

    data = request.get_json(silent=True) or {}
    code = data.get('code') or ''

    # The code and the key it returns both cross the wire here, so refuse to do
    # it in the clear unless the operator has explicitly accepted the risk.
    if not request.is_secure and not current_app.config.get('ALLOW_INSECURE_ENROLL', False):
        forwarded = request.headers.get('X-Forwarded-Proto', '')
        if forwarded != 'https':
            return jsonify({
                'error': 'Enrollment requires HTTPS.',
                'detail': 'Use an https:// server URL, or set ALLOW_INSECURE_ENROLL=true '
                          'on the server if this is a trusted local network.',
            }), 400

    payload, problem = enrollment.enroll(
        code,
        device={
            'mac_address': data.get('mac_address'),
            'hostname': data.get('hostname'),
            'ip_address': data.get('ip_address'),
            'platform': data.get('platform'),
        },
        remote_ip=request.remote_addr,
    )
    if payload is None:
        return jsonify({'error': problem}), 400

    # Hand back the settings the Pi would otherwise have to be told, so the
    # installer only ever asks for a URL and a code.
    payload['site_url'] = (current_app.config.get('SITE_URL')
                           or request.host_url.rstrip('/'))
    payload['ok'] = True
    return jsonify(payload)


@agent_bp.route('/pending-jobs')
@require_agent_key
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_AGENT', '600 per minute'))
def pending_jobs():
    """Return jobs ready to be claimed by this agent.

    Uses atomic claim so two agents won't both grab the same job.
    """
    agent_id = _agent_id()
    printer_id = _printer_id()
    claimed = claim_pending_jobs(printer_id=printer_id, agent_id=agent_id, limit=10)

    def _job_payload(j):
        payload = options_to_dict(j)
        payload['id'] = j.id
        payload['filename'] = j.filename
        return payload

    return jsonify([_job_payload(j) for j in claimed])


@agent_bp.route('/download/<int:job_id>')
@require_agent_key
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_AGENT', '600 per minute'))
def download(job_id):
    """Stream the print file for a job — only if claimed by this agent."""
    job = PrintJob.query.get_or_404(job_id)
    agent_id = _agent_id()
    if job.claimed_by_agent and job.claimed_by_agent != agent_id:
        return jsonify({'error': 'Job claimed by another agent'}), 403
    if job.files_purged_at is not None:
        # Job already finished; the file was deleted after the print completed.
        return jsonify({'error': 'File purged after completion', 'state': job.status}), 410
    if not os.path.exists(job.file_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(job.file_path, as_attachment=True, download_name=job.filename)


@agent_bp.route('/job/<int:job_id>/started', methods=['POST'])
@require_agent_key
def job_started(job_id):
    """Agent reports it submitted the job to CUPS. Idempotent."""
    job = PrintJob.query.get_or_404(job_id)
    data = request.get_json() or {}
    cups_job_id = data.get('cups_job_id')

    if job.status in TERMINAL_STATES:
        return jsonify({'ok': True, 'state': job.status, 'noop': True})
    if job.status == 'printing':
        return jsonify({'ok': True, 'state': 'printing', 'noop': True})

    if not mark_printing(job, cups_job_id):
        return jsonify({'error': 'Job not in printable state', 'state': job.status}), 409
    return jsonify({'ok': True, 'state': 'printing'})


@agent_bp.route('/job/<int:job_id>/status', methods=['POST'])
@require_agent_key
def job_status(job_id):
    """Agent reports job completed or failed. Idempotent."""
    job = PrintJob.query.get_or_404(job_id)
    data = request.get_json() or {}
    status = data.get('status')

    # If the job is already terminal, acknowledge without flipping state.
    if job.status in TERMINAL_STATES:
        return jsonify({'ok': True, 'state': job.status, 'noop': True})

    if status == 'completed':
        complete_job(job)
    elif status == 'failed':
        fail_job(job, data.get('error', 'Print failed'))
    elif status == 'cancelled':
        fail_job(job, 'Cancelled at printer')
    else:
        return jsonify({'error': 'Invalid status'}), 400

    return jsonify({'ok': True, 'state': job.status})


@agent_bp.route('/heartbeat', methods=['POST'])
@require_agent_key
def heartbeat():
    """Agent reports its printer status."""
    data = request.get_json() or {}
    printer_id = _printer_id()

    # An enrolled kiosk already resolved to its own row during authentication.
    agent = getattr(g, 'agent_row', None)
    if agent is None:
        agent = AgentStatus.query.filter_by(printer_id=printer_id).first()
    if agent is None:
        # Migrate the legacy singleton row if it exists
        legacy = db.session.get(AgentStatus, 1)
        if legacy and legacy.printer_id is None:
            legacy.printer_id = printer_id
            agent = legacy
        else:
            agent = AgentStatus(printer_id=printer_id)
            db.session.add(agent)

    agent.is_online = True
    agent.printer_name = data.get('printer_name', 'Unknown')
    agent.printer_status = data.get('printer_status', 'Unknown')
    agent.last_heartbeat = datetime.now(timezone.utc)

    # Identity fields. Absent keys leave the stored value alone so an older
    # agent build never blanks out what we already know about the device.
    for field in ('agent_version', 'mac_address', 'hostname', 'ip_address', 'platform'):
        value = data.get(field)
        if value:
            setattr(agent, field, str(value)[:160])

    if data.get('activity'):
        agent.activity = str(data['activity'])[:20]
    agent.active_job_count = int(data.get('active_job_count') or 0)
    agent.last_error = (str(data['last_error'])[:300] if data.get('last_error') else None)

    started = data.get('started_at')
    if started:
        try:
            agent.agent_started_at = datetime.fromisoformat(started)
        except (TypeError, ValueError):
            pass

    db.session.commit()

    # Close the outage as soon as the kiosk speaks, rather than waiting for the
    # next watchdog sweep.
    try:
        from app.services import stock
        stock.resolve_alert(f'kiosk:{agent.printer_id or agent.id}',
                            note='Kiosk came back online')
    except Exception:
        pass

    return jsonify({'ok': True})


@agent_bp.route('/reconcile', methods=['POST'])
@require_agent_key
def reconcile():
    """Agent startup reconciliation.

    Body: { "in_flight": [{"server_id": int, "cups_job_id": int, "state": str}, ...] }

    For every job the server believes this agent owns (printing / ready_to_print),
    if it's not in the agent's in_flight list, mark it failed — the agent has no
    record of it, so it was lost during a crash/restart. If it is in flight, sync
    the state.
    """
    agent_id = _agent_id()
    printer_id = _printer_id()
    data = request.get_json() or {}
    in_flight = {item['server_id']: item for item in data.get('in_flight', [])
                 if 'server_id' in item}

    owned = PrintJob.query.filter(
        PrintJob.status.in_(('ready_to_print', 'printing')),
        PrintJob.claimed_by_agent == agent_id,
    ).all()

    lost = 0
    synced = 0
    for job in owned:
        info = in_flight.pop(job.id, None)
        if info is None:
            fail_job(job, 'Agent restart — job lost')
            lost += 1
        else:
            state = info.get('state')
            if state == 'completed':
                complete_job(job)
                synced += 1
            elif state in ('failed', 'aborted', 'cancelled'):
                fail_job(job, f'CUPS state: {state}')
                synced += 1
            elif state == 'processing' and job.status != 'printing':
                mark_printing(job, info.get('cups_job_id'))
                synced += 1
    return jsonify({'ok': True, 'lost': lost, 'synced': synced,
                    'unknown_to_server': list(in_flight.keys())})


# --- Kiosk proxy endpoints (for RPi local kiosk server) ---

@agent_bp.route('/kiosk-status')
@require_agent_key
def kiosk_status():
    from app.services.kiosk import get_status
    from app.models import Advertisement
    from app.services import print_lock as _pl

    # The display polls this every 1.5s, which doubles as its liveness signal —
    # the screen can be dead while the print agent is fine.
    agent = AgentStatus.query.filter_by(printer_id=_printer_id()).first()
    if agent is not None:
        agent.kiosk_last_seen = datetime.now(timezone.utc)
        db.session.commit()

    data = get_status()
    data['print_locked'] = _pl.is_enabled()
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

    ads = Advertisement.query.filter_by(is_active=True).order_by(
        Advertisement.display_order.asc(), Advertisement.created_at.desc()).all()
    data['ads'] = [a.to_kiosk_dict() for a in ads]
    return jsonify(data)


@agent_bp.route('/kiosk-activate', methods=['POST'])
@require_agent_key
def kiosk_activate():
    from app.services.kiosk import activate_next_token
    token = activate_next_token()
    return jsonify({'ok': True, 'token': token})


@agent_bp.route('/kiosk-token')
@require_agent_key
def kiosk_token():
    from app.services.kiosk import get_current_token
    return jsonify({'token': get_current_token()})
