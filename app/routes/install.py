"""Serving the kiosk installer.

A new Raspberry Pi has no copy of this project and no GitHub access, so the
server hands out both the installer and the agent code itself:

    curl -sSL https://your-server/install.sh | sudo bash

Neither response contains a secret. The installer only becomes useful with an
enrollment code, which is generated in the admin UI, expires, and is single-use.
The agent source is the same code that would sit in a public repository.
"""
import io
import logging
import os
import tarfile

from flask import Blueprint, Response, current_app, request

log = logging.getLogger(__name__)

install_bp = Blueprint('install', __name__)

# What a kiosk needs. Anything else in print_agent/ stays on the server.
BUNDLE_FILES = (
    'print_agent.py',
    'kiosk_server.py',
    'kiosk-browser.sh',
    'install.sh',
    'requirements.txt',
)


def agent_dir():
    """Path to print_agent/ — a sibling of the app package."""
    return os.path.join(os.path.dirname(current_app.root_path), 'print_agent')


def _server_url():
    """The address the Pi should call back on.

    Prefers the configured SITE_URL so the installer points at the real domain
    even when it was fetched through a proxy or an IP.
    """
    configured = (current_app.config.get('SITE_URL') or '').strip()
    return (configured or request.host_url).rstrip('/')


@install_bp.route('/install.sh')
def install_script():
    """The installer, with this server's address already filled in."""
    path = os.path.join(agent_dir(), 'install.sh')
    if not os.path.exists(path):
        return Response('# installer not available on this server\nexit 1\n',
                        status=404, mimetype='text/plain')

    with open(path, 'r', encoding='utf-8') as f:
        script = f.read()
    script = script.replace('__SERVER_URL__', _server_url())

    return Response(script, mimetype='text/x-shellscript',
                    headers={'Cache-Control': 'no-store',
                             'Content-Disposition': 'inline; filename="install.sh"'})


@install_bp.route('/install/agent.tar.gz')
def agent_bundle():
    """The agent source, built into a tarball on the fly."""
    directory = agent_dir()
    if not os.path.isdir(directory):
        return Response('agent bundle not available', status=404, mimetype='text/plain')

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name in BUNDLE_FILES:
            path = os.path.join(directory, name)
            if not os.path.exists(path):
                continue
            info = tar.gettarinfo(path, arcname=name)
            # Reproducible and owned by nobody in particular — the installer
            # chowns to root once it has extracted.
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            info.mtime = 0
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            with open(path, 'rb') as f:
                tar.addfile(info, f)

    buf.seek(0)
    return Response(buf.getvalue(), mimetype='application/gzip',
                    headers={'Cache-Control': 'no-store',
                             'Content-Disposition': 'attachment; filename="agent.tar.gz"'})
