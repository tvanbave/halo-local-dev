"""Opt-in browser HTTP authentication; uses the existing private API credential."""
import base64
import binascii
import hmac
from aiohttp import web


def authorized(header, token):
    if not isinstance(header, str) or len(header) > 4096:
        return False
    if header.startswith('Bearer '):
        return hmac.compare_digest(header[7:].encode(), token.encode())
    try:
        scheme, encoded = header.split(' ', 1)
        if scheme.lower() != 'basic':
            return False
        username, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    return username == 'halo' and hmac.compare_digest(password.encode(), token.encode())


def basic_header(username, password):
    return 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode('ascii')


def challenge():
    return web.json_response({'error': 'authentication_required'}, status=401, headers={
        'WWW-Authenticate': 'Basic realm="HALO Local", charset="UTF-8"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def backup_owner_refused():
    # A Basic challenge here would prompt for login throughout the everyday UI.
    return web.json_response({'error': 'backup_owner_required',
        'message': 'Enter this Pi’s bridge owner password to continue.'}, status=403,
        headers={'Cache-Control': 'no-store'})
