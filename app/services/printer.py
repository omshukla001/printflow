"""CUPS printing wrapper."""
import cups
from flask import current_app
from app.services.print_options import to_cups_options


# IPP job states (RFC 8011):
#   3 pending, 4 pending-held, 5 processing, 6 processing-stopped,
#   7 canceled, 8 aborted, 9 completed
CUPS_JOB_STATE_MAP = {
    3: 'queued',
    4: 'held',
    5: 'printing',
    6: 'stopped',
    7: 'cancelled',
    8: 'failed',
    9: 'completed',
}

# IPP printer states: 3=idle, 4=printing, 5=stopped
CUPS_PRINTER_STATE_MAP = {3: 'Idle', 4: 'Printing', 5: 'Stopped'}


def get_connection():
    return cups.Connection()


def get_default_printer():
    name = current_app.config.get('DEFAULT_PRINTER', '')
    if name:
        return name
    try:
        conn = get_connection()
        return conn.getDefault()
    except Exception:
        return None


def get_printers():
    try:
        conn = get_connection()
        return conn.getPrinters()
    except Exception:
        return {}


def get_printer_status():
    try:
        printer_name = get_default_printer()
        if not printer_name:
            return {'name': None, 'status': 'No printer configured', 'online': False}

        conn = get_connection()
        printers = conn.getPrinters()
        if printer_name in printers:
            info = printers[printer_name]
            state = info.get('printer-state', 0)
            reasons = info.get('printer-state-reasons', '')
            return {
                'name': printer_name,
                'status': CUPS_PRINTER_STATE_MAP.get(state, 'Unknown'),
                'state': state,
                'reasons': reasons,
                'info': info.get('printer-info', ''),
                'online': state in (3, 4),
            }
        return {'name': printer_name, 'status': 'Not found', 'online': False}
    except Exception as e:
        return {'name': None, 'status': f'Error: {e}', 'online': False}


def submit_job(file_path, job_title='PrintFlow Job', options=None):
    """Submit a print job to CUPS. Returns CUPS job ID or raises exception."""
    printer_name = get_default_printer()
    if not printer_name:
        raise RuntimeError('No printer configured')

    conn = get_connection()
    cups_options = to_cups_options(options or {})
    return conn.printFile(printer_name, file_path, job_title, cups_options)


def get_job_status(cups_job_id):
    """Check status of a CUPS job. Returns a status string from CUPS_JOB_STATE_MAP."""
    try:
        conn = get_connection()
        attrs = conn.getJobAttributes(cups_job_id)
        state = attrs.get('job-state', 0)
        return CUPS_JOB_STATE_MAP.get(state, 'unknown')
    except Exception:
        return 'unknown'


def cancel_cups_job(cups_job_id):
    try:
        conn = get_connection()
        conn.cancelJob(cups_job_id)
        return True
    except Exception:
        return False
