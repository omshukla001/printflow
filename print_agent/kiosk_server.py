#!/usr/bin/env python3
"""PrintFlow Kiosk Server — runs on RPi, serves the kiosk display locally.

Proxies data from the AWS server so the kiosk display works even if
the internet connection is briefly interrupted (shows a fallback).
"""
import os
import sys
import io
import logging
import requests
from flask import Flask, render_template, jsonify, Response

# Try to import QR generation
try:
    import qrcode
    from qrcode.image.pil import PilImage
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False
    print("WARNING: qrcode package not installed. QR generation disabled.")

# Configuration from environment
SERVER_URL = os.environ.get('SERVER_URL', 'http://127.0.0.1:5000').rstrip('/')
AGENT_KEY = os.environ.get('AGENT_KEY', '')
SITE_URL = os.environ.get('SITE_URL', SERVER_URL)
KIOSK_PORT = int(os.environ.get('KIOSK_PORT', '5000'))
# Must match the print agent on this device so the admin page shows one kiosk,
# not two half-populated ones.
AGENT_ID = os.environ.get('AGENT_ID', os.uname().nodename if hasattr(os, 'uname') else 'default')
PRINTER_ID = os.environ.get('PRINTER_ID', AGENT_ID)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('kiosk-server')

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), '..', 'app', 'templates'))

# Cache for last known state (offline fallback)
_cached_status = None
_cached_token = None


def api_headers():
    # The printer id lets the server record that this kiosk's *screen* is alive,
    # separately from its print agent.
    return {
        'X-Agent-Key': AGENT_KEY,
        'X-Agent-Id': AGENT_ID,
        'X-Printer-Id': PRINTER_ID,
    }


def _fetch_token():
    """Fetch the current kiosk token from the server."""
    global _cached_token
    try:
        resp = requests.get(f'{SERVER_URL}/api/agent/kiosk-token',
                            headers=api_headers(), timeout=5)
        if resp.status_code == 200:
            _cached_token = resp.json().get('token')
            return _cached_token
    except requests.RequestException:
        pass
    return _cached_token


def _generate_qr_png(url, box_size=14, border=2):
    """Generate a QR code PNG image."""
    if not HAS_QRCODE:
        return b''
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=box_size, border=border)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


@app.route('/kiosk')
def display():
    """Serve the kiosk display page."""
    token = _fetch_token()
    return render_template('kiosk/display.html', token=token, site_url=SITE_URL)


@app.route('/kiosk/qr.png')
def qr_image():
    """Generate QR code pointing to the cloud checkin URL."""
    token = _fetch_token()
    if not token:
        return Response(b'', status=503, content_type='image/png')
    checkin_url = f"{SITE_URL}/checkin/{token}"
    png_bytes = _generate_qr_png(checkin_url)
    return Response(png_bytes, status=200, content_type='image/png',
                    headers={'Cache-Control': 'no-store'})


@app.route('/kiosk/qr/<token>.png')
def qr_image_for_token(token):
    """Generate QR code for a specific token (preloading)."""
    checkin_url = f"{SITE_URL}/checkin/{token}"
    png_bytes = _generate_qr_png(checkin_url)
    return Response(png_bytes, status=200, content_type='image/png',
                    headers={'Cache-Control': 'no-store'})


@app.route('/kiosk/status')
def kiosk_status():
    """Proxy the kiosk status from the AWS server."""
    global _cached_status
    try:
        resp = requests.get(f'{SERVER_URL}/api/agent/kiosk-status',
                            headers=api_headers(), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # Ad media lives on the server; the display fetches it directly, so
            # every relative path has to become absolute.
            for ad in data.get('ads', []):
                for key in ('image_url', 'media_url'):
                    if ad.get(key) and ad[key].startswith('/'):
                        ad[key] = f"{SERVER_URL}{ad[key]}"
                if ad.get('pages'):
                    ad['pages'] = [f"{SERVER_URL}{p}" if p.startswith('/') else p
                                   for p in ad['pages']]
            _cached_status = data
            return jsonify(data)
    except requests.RequestException:
        pass

    # Offline fallback
    if _cached_status:
        return jsonify(_cached_status)
    return jsonify({
        'token': _cached_token,
        'is_scanned': False,
        'scanned_by': None,
        'jobs_prioritized': 0,
        'pending_count': 0,
        'print_locked': False,
        'printing': [],
        'ads': [],
    })


@app.route('/kiosk/activate', methods=['POST'])
def kiosk_activate():
    """Proxy token activation to the AWS server."""
    try:
        resp = requests.post(f'{SERVER_URL}/api/agent/kiosk-activate',
                             headers=api_headers(), timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except requests.RequestException:
        pass
    return jsonify({'ok': False, 'error': 'Server unreachable'}), 502


if __name__ == '__main__':
    if not AGENT_KEY:
        log.error("AGENT_KEY not set!")
        sys.exit(1)
    log.info(f"Kiosk server starting — server: {SERVER_URL}, site: {SITE_URL}")
    app.run(host='0.0.0.0', port=KIOSK_PORT, debug=False)
