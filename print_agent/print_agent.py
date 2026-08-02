#!/usr/bin/env python3
"""PrintFlow Print Agent — polls AWS server for print jobs, prints via local CUPS."""
import os
import sys
import time
import logging
import requests

try:
    import cups
except ImportError:
    print("ERROR: pycups not installed. Install with: sudo apt install python3-cups")
    sys.exit(1)

AGENT_VERSION = '1.1.0'

# --- Configuration ---
SERVER_URL = os.environ.get('SERVER_URL', 'http://127.0.0.1:5000').rstrip('/')
AGENT_KEY = os.environ.get('AGENT_KEY', '')
AGENT_ID = os.environ.get('AGENT_ID', os.uname().nodename if hasattr(os, 'uname') else 'default')
PRINTER_ID = os.environ.get('PRINTER_ID', AGENT_ID)
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '2'))
CUPS_CHECK_INTERVAL = int(os.environ.get('CUPS_CHECK_INTERVAL', '5'))
HEARTBEAT_INTERVAL = int(os.environ.get('HEARTBEAT_INTERVAL', '30'))
DEFAULT_PRINTER = os.environ.get('DEFAULT_PRINTER', '')
TEMP_DIR = os.environ.get('TEMP_DIR', '/tmp/printflow')
HTTPS_REQUIRED = os.environ.get('HTTPS_REQUIRED', 'false').lower() in ('1', 'true', 'yes')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('print-agent')

active_jobs = {}  # { server_job_id: cups_job_id }
last_heartbeat = 0
last_cups_check = 0
backoff_seconds = 0
MAX_BACKOFF = 60
last_error = None      # most recent failure, surfaced to the admin kiosk page
STARTED_AT = None      # ISO timestamp, set in main_loop


def api_headers():
    return {
        'X-Agent-Key': AGENT_KEY,
        'X-Agent-Id': AGENT_ID,
        'X-Printer-Id': PRINTER_ID,
    }


def request_with_retry(method, path, **kwargs):
    """Wrapper around requests with exponential backoff on transport errors."""
    global backoff_seconds
    url = f'{SERVER_URL}{path}'
    kwargs.setdefault('headers', {}).update(api_headers())
    kwargs.setdefault('timeout', 10)
    try:
        resp = requests.request(method, url, **kwargs)
        backoff_seconds = 0  # reset on success
        return resp
    except requests.RequestException as e:
        backoff_seconds = min(max(1, backoff_seconds * 2 if backoff_seconds else 2), MAX_BACKOFF)
        log.warning('Network error on %s %s: %s (backoff=%ds)', method, path, e, backoff_seconds)
        time.sleep(backoff_seconds)
        return None


def _read_mac():
    """MAC of the interface actually carrying traffic.

    Prefers a real wired/wireless interface over docker/veth bridges, and falls
    back to uuid.getnode() when /sys is unreadable.
    """
    net_dir = '/sys/class/net'
    preferred = ('eth', 'en', 'wlan', 'wl')
    candidates = []
    try:
        for name in sorted(os.listdir(net_dir)):
            if name == 'lo' or name.startswith(('docker', 'veth', 'br-', 'virbr')):
                continue
            try:
                with open(os.path.join(net_dir, name, 'address')) as f:
                    mac = f.read().strip()
            except OSError:
                continue
            if not mac or mac == '00:00:00:00:00:00':
                continue
            rank = 0 if name.startswith(preferred) else 1
            candidates.append((rank, name, mac))
    except OSError:
        pass

    if candidates:
        candidates.sort()
        return candidates[0][2].upper()

    import uuid as _uuid
    node = _uuid.getnode()
    return ':'.join(f'{(node >> b) & 0xFF:02X}' for b in range(40, -8, -8))


def _read_ip():
    """Primary LAN address — found by asking the routing table, no traffic sent."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return ''
    finally:
        s.close()


def device_info():
    """Static identity of this kiosk, gathered once at startup."""
    import platform as _platform
    import socket
    return {
        'mac_address': _read_mac(),
        'hostname': socket.gethostname(),
        'ip_address': _read_ip(),
        'platform': f'{_platform.system()} {_platform.release()} ({_platform.machine()})',
    }


DEVICE = {}


def get_cups_connection():
    return cups.Connection()


def get_default_printer(conn):
    if DEFAULT_PRINTER:
        printers = conn.getPrinters()
        if DEFAULT_PRINTER in printers:
            return DEFAULT_PRINTER
    return conn.getDefault()


def get_printer_info(conn):
    printer_name = get_default_printer(conn)
    if not printer_name:
        return 'No printer', 'Not configured'
    printers = conn.getPrinters()
    if printer_name in printers:
        state = printers[printer_name].get('printer-state', 0)
        state_map = {3: 'Idle', 4: 'Printing', 5: 'Stopped'}
        return printer_name, state_map.get(state, f'State {state}')
    return printer_name, 'Unknown'


def fetch_pending_jobs():
    resp = request_with_retry('GET', '/api/agent/pending-jobs')
    if resp is None:
        return []
    if resp.status_code != 200:
        log.warning('Pending jobs HTTP %s: %s', resp.status_code, resp.text[:200])
        return []
    return resp.json()


def download_file(job_id, filename):
    os.makedirs(TEMP_DIR, exist_ok=True)
    safe_name = os.path.basename(filename).replace('/', '_')[:200]
    local_path = os.path.join(TEMP_DIR, f"{job_id}_{safe_name}")
    resp = request_with_retry('GET', f'/api/agent/download/{job_id}', stream=True, timeout=120)
    if resp is None or resp.status_code != 200:
        if resp is not None:
            log.error('Download HTTP %s for job %s', resp.status_code, job_id)
        return None
    with open(local_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_path


def report_started(job_id, cups_job_id):
    resp = request_with_retry('POST', f'/api/agent/job/{job_id}/started',
                              json={'cups_job_id': cups_job_id})
    if resp is None:
        return False
    if resp.status_code == 200:
        return True
    log.warning('Report started HTTP %s for job %s', resp.status_code, job_id)
    return False


def report_status(job_id, status, error=None):
    payload = {'status': status}
    if error:
        payload['error'] = error
    resp = request_with_retry('POST', f'/api/agent/job/{job_id}/status', json=payload)
    if resp is None:
        return False
    if resp.status_code == 200:
        return True
    log.warning('Report status HTTP %s for job %s', resp.status_code, job_id)
    return False


def send_heartbeat(conn):
    """Tell the server this kiosk is alive, who it is, and what it is doing."""
    global last_error
    try:
        printer_name, printer_status = get_printer_info(conn)
        if active_jobs:
            activity = 'printing'
        elif printer_status in ('Stopped', 'Not configured', 'Unknown'):
            activity = 'error'
        else:
            activity = 'idle'

        payload = {
            'printer_name': printer_name,
            'printer_status': printer_status,
            'agent_version': AGENT_VERSION,
            'activity': activity,
            'active_job_count': len(active_jobs),
            'last_error': last_error,
            'started_at': STARTED_AT,
        }
        payload.update(DEVICE)
        request_with_retry('POST', '/api/agent/heartbeat', json=payload)
    except Exception as e:
        log.debug('Heartbeat failed: %s', e)


def reconcile_on_startup(conn):
    """Tell the server which jobs we actually have in CUPS so it can mark
    abandoned jobs as failed."""
    in_flight = []
    try:
        all_jobs = conn.getJobs(which_jobs='not-completed', my_jobs=False)
        for cups_id, info in all_jobs.items():
            title = info.get('title', '')
            server_id = _parse_server_id(title)
            if server_id is None:
                continue
            state = info.get('job-state', 5)
            state_str = {3: 'pending', 4: 'held', 5: 'processing',
                         6: 'stopped', 7: 'cancelled', 8: 'aborted', 9: 'completed'}.get(state, 'unknown')
            in_flight.append({'server_id': server_id, 'cups_job_id': cups_id, 'state': state_str})
    except Exception as e:
        log.warning('Could not list CUPS jobs during reconcile: %s', e)

    resp = request_with_retry('POST', '/api/agent/reconcile', json={'in_flight': in_flight})
    if resp is not None and resp.status_code == 200:
        data = resp.json()
        log.info('Reconcile complete: lost=%s synced=%s', data.get('lost'), data.get('synced'))
        # Restore active_jobs map for entries that are still printing
        for item in in_flight:
            if item['state'] in ('pending', 'processing', 'held'):
                active_jobs[item['server_id']] = item['cups_job_id']
    else:
        log.warning('Reconcile failed or server unreachable; continuing anyway')


def _parse_server_id(title):
    # Titles look like "PrintFlow #123: filename" or "Direct #45: filename"
    import re
    m = re.search(r'#(\d+)', title or '')
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


_ORIENTATION_IPP = {'portrait': '3', 'landscape': '4'}
_QUALITY_IPP = {'draft': '3', 'normal': '4', 'high': '5'}


def build_cups_options(job):
    """Same translation as the server's print_options.to_cups_options.

    Duplicated here so the agent doesn't need to import server code.
    """
    options = {}

    copies = job.get('copies', 1) or 1
    if copies > 1:
        options['copies'] = str(copies)

    color_mode = job.get('color_mode', 'bw')
    options['print-color-mode'] = 'monochrome' if color_mode == 'bw' else 'color'

    paper_size = job.get('paper_size', 'A4')
    options['media'] = paper_size if paper_size in ('A4', 'A3', 'Letter') else 'A4'

    sides = job.get('sides', 'one-sided')
    options['sides'] = 'two-sided-long-edge' if sides == 'two-sided' else 'one-sided'

    page_ranges = job.get('page_ranges')
    if page_ranges:
        options['page-ranges'] = page_ranges

    nup = job.get('pages_per_sheet') or 1
    if nup > 1:
        options['number-up'] = str(nup)

    page_set = job.get('page_set', 'all')
    if page_set in ('odd', 'even'):
        options['page-set'] = page_set

    if job.get('output_order') == 'reverse':
        options['outputorder'] = 'reverse'

    orientation = job.get('orientation', 'auto')
    if orientation in _ORIENTATION_IPP:
        options['orientation-requested'] = _ORIENTATION_IPP[orientation]

    if job.get('fit_to_page'):
        options['fit-to-page'] = ''

    quality = job.get('print_quality', 'normal')
    if quality in _QUALITY_IPP and quality != 'normal':
        options['print-quality'] = _QUALITY_IPP[quality]

    if job.get('collate') is False:
        options['Collate'] = 'False'

    return options


def submit_to_cups(conn, file_path, job):
    printer = get_default_printer(conn)
    if not printer:
        raise Exception("No printer configured")

    options = build_cups_options(job)
    title = f"PrintFlow #{job['id']}: {job['filename']}"
    return conn.printFile(printer, file_path, title, options)


def discard_local_copy(server_id):
    """Delete this agent's cached copy of a job's file.

    Called once the job is terminal — the document only ever lives here between
    download and the printer finishing with it.
    """
    if not os.path.isdir(TEMP_DIR):
        return
    prefix = f"{server_id}_"
    try:
        for name in os.listdir(TEMP_DIR):
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(TEMP_DIR, name))
                    log.debug('Removed local copy %s', name)
                except OSError as e:
                    log.warning('Could not remove %s: %s', name, e)
    except OSError as e:
        log.warning('Could not scan %s: %s', TEMP_DIR, e)


def sweep_temp_dir(keep_ids):
    """Drop cached files left behind by a crash or a hard restart.

    Anything not belonging to a job we are still tracking is dead weight — the
    server has its own copy until the print is confirmed.
    """
    if not os.path.isdir(TEMP_DIR):
        return
    keep_prefixes = tuple(f"{i}_" for i in keep_ids)
    removed = 0
    try:
        for name in os.listdir(TEMP_DIR):
            if keep_prefixes and name.startswith(keep_prefixes):
                continue
            try:
                os.remove(os.path.join(TEMP_DIR, name))
                removed += 1
            except OSError:
                pass
    except OSError as e:
        log.warning('Could not sweep %s: %s', TEMP_DIR, e)
    if removed:
        log.info('Removed %d stale cached file(s) from %s', removed, TEMP_DIR)


def check_cups_jobs(conn):
    completed = []
    for server_id, cups_id in list(active_jobs.items()):
        try:
            attrs = conn.getJobAttributes(cups_id)
            state = attrs.get('job-state', 0)
            # 3=pending,4=held,5=processing,6=stopped,7=cancelled,8=aborted,9=completed
            if state == 9:
                log.info('Job %s (CUPS #%s) completed', server_id, cups_id)
                report_status(server_id, 'completed')
                completed.append(server_id)
            elif state in (7, 8):
                reason = 'cancelled' if state == 7 else 'aborted'
                log.warning('Job %s (CUPS #%s) %s', server_id, cups_id, reason)
                report_status(server_id, 'failed', f'CUPS: {reason}')
                completed.append(server_id)
            elif state == 6:
                # stopped — let the server know but keep tracking
                log.info('Job %s (CUPS #%s) stopped (paper jam/out?)', server_id, cups_id)
        except Exception as e:
            log.warning('Error checking CUPS job %s: %s', cups_id, e)

    for server_id in completed:
        del active_jobs[server_id]
        discard_local_copy(server_id)


def main_loop():
    global STARTED_AT
    from datetime import datetime, timezone
    STARTED_AT = datetime.now(timezone.utc).isoformat()

    log.info('PrintFlow Agent v%s starting — server: %s', AGENT_VERSION, SERVER_URL)
    log.info('Agent ID: %s — Printer ID: %s', AGENT_ID, PRINTER_ID)

    DEVICE.update(device_info())
    log.info('Device: %s / MAC %s / IP %s / %s', DEVICE['hostname'],
             DEVICE['mac_address'], DEVICE['ip_address'] or 'no IP', DEVICE['platform'])

    if HTTPS_REQUIRED and not SERVER_URL.startswith('https://'):
        log.error('HTTPS_REQUIRED=true but SERVER_URL is not https. Refusing to start.')
        sys.exit(2)

    if not AGENT_KEY:
        log.error('AGENT_KEY not set! Set the AGENT_KEY environment variable.')
        sys.exit(1)

    conn = get_cups_connection()
    printer = get_default_printer(conn)
    log.info('Default printer: %s', printer or 'NONE')

    reconcile_on_startup(conn)
    # reconcile restored active_jobs; anything else cached here is orphaned.
    sweep_temp_dir(active_jobs.keys())

    global last_heartbeat, last_cups_check, last_error

    while True:
        now = time.time()

        try:
            pending = fetch_pending_jobs()
            for job in pending:
                job_id = job['id']
                if job_id in active_jobs:
                    continue

                log.info('New job: #%s - %s', job_id, job['filename'])
                file_path = download_file(job_id, job['filename'])
                if not file_path:
                    log.error('Failed to download job #%s', job_id)
                    last_error = f'Download failed for job #{job_id}'
                    report_status(job_id, 'failed', 'agent: download failed')
                    continue

                try:
                    cups_id = submit_to_cups(conn, file_path, job)
                    log.info('Job #%s submitted to CUPS as #%s', job_id, cups_id)
                    last_error = None
                    if report_started(job_id, cups_id):
                        active_jobs[job_id] = cups_id
                    else:
                        log.warning('Could not report job #%s as started', job_id)
                except Exception as e:
                    log.error('CUPS submit failed for job #%s: %s', job_id, e)
                    last_error = f'Job #{job_id}: {e}'[:300]
                    report_status(job_id, 'failed', str(e))
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass

            if now - last_cups_check >= CUPS_CHECK_INTERVAL and active_jobs:
                check_cups_jobs(conn)
                last_cups_check = now

            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_heartbeat(conn)
                last_heartbeat = now

        except Exception as e:
            log.error('Loop error: %s', e)
            last_error = str(e)[:300]
            try:
                conn = get_cups_connection()
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main_loop()
