"""Sign in with Google.

The authorization-code flow, written against `requests` rather than pulling in
an OAuth library. The whole exchange is three HTTPS calls and the dependency
would be one more package to keep patched on an internet-facing box.

Why the ID token's signature is not verified here: the code is exchanged
server-to-server over TLS using the client secret, and the profile is then read
from Google's userinfo endpoint over that same channel. Nothing in the flow
takes the browser's word for who the user is. Signature verification matters
when an ID token arrives from an untrusted party — it does not here, and doing
it properly needs key fetching, caching and rotation that would be more
surface, not less.

Configure with GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET. Until both are set,
is_configured() is False and the button never appears.
"""
import logging
import secrets

import requests
from flask import current_app, url_for

log = logging.getLogger(__name__)

AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
USERINFO_ENDPOINT = 'https://openidconnect.googleapis.com/v1/userinfo'

TIMEOUT = 10


def is_configured():
    """Whether Google sign-in can actually be offered."""
    cfg = current_app.config
    return bool(cfg.get('GOOGLE_CLIENT_ID') and cfg.get('GOOGLE_CLIENT_SECRET'))


def redirect_uri():
    """Where Google sends the browser back.

    Must match an Authorized redirect URI on the OAuth client exactly, down to
    the scheme and trailing path. _external with https is deliberate: behind
    nginx the request itself looks like plain http, and a http:// callback URL
    will not match what is registered.
    """
    configured = current_app.config.get('GOOGLE_REDIRECT_URI')
    if configured:
        return configured
    return url_for('auth.google_callback', _external=True, _scheme='https')


def new_state():
    """Opaque value tying the callback to the browser that started the flow."""
    return secrets.token_urlsafe(24)


def authorize_url(state):
    params = {
        'client_id': current_app.config['GOOGLE_CLIENT_ID'],
        'redirect_uri': redirect_uri(),
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        # Ask every time rather than silently reusing a signed-in Google
        # account: this is a shared counter machine as often as a phone.
        'prompt': 'select_account',
    }
    return AUTH_ENDPOINT + '?' + requests.compat.urlencode(params)


def exchange_code(code):
    """Trade the one-time code for an access token. None if Google refuses."""
    try:
        resp = requests.post(TOKEN_ENDPOINT, timeout=TIMEOUT, data={
            'code': code,
            'client_id': current_app.config['GOOGLE_CLIENT_ID'],
            'client_secret': current_app.config['GOOGLE_CLIENT_SECRET'],
            'redirect_uri': redirect_uri(),
            'grant_type': 'authorization_code',
        })
    except requests.RequestException as e:
        log.warning('Google token exchange failed: %s', e)
        return None
    if resp.status_code != 200:
        # Body carries Google's reason (redirect_uri_mismatch and friends),
        # which is the difference between a five-minute fix and an afternoon.
        log.warning('Google token exchange HTTP %s: %s',
                    resp.status_code, resp.text[:300])
        return None
    return resp.json().get('access_token')


def fetch_profile(access_token):
    """The signed-in user's profile, or None.

    Returns only a profile whose email Google reports as verified. An
    unverified address could belong to somebody else, and this profile is about
    to be matched against existing accounts by email.
    """
    try:
        resp = requests.get(USERINFO_ENDPOINT, timeout=TIMEOUT,
                            headers={'Authorization': f'Bearer {access_token}'})
    except requests.RequestException as e:
        log.warning('Google userinfo failed: %s', e)
        return None
    if resp.status_code != 200:
        log.warning('Google userinfo HTTP %s', resp.status_code)
        return None

    data = resp.json()
    if not data.get('email') or not data.get('sub'):
        return None
    if not data.get('email_verified'):
        log.info('Google sign-in refused: email not verified')
        return None
    return {
        'sub': str(data['sub']),
        'email': data['email'].strip().lower(),
        'name': (data.get('name') or '').strip() or data['email'].split('@')[0],
    }
