"""The SSO (OIDC) client, built from the LIVE configuration.

The client used to be registered once, at import, from the .env values: the
SSO fields of Admin → Config were stored but never used, and any change needed
a redeploy. It is now (re)built on demand from `parametres.oidc_config()` —
Admin first, then .env — and rebuilt as soon as the metadata URL, client ID or
secret change. Each gunicorn worker keeps its own client; the check is one
settings read per SSO request.
"""
import threading
import time

import requests

from parametres import oidc_config

_UA = 'Mozilla/5.0 (DGX portal)'
_NOM = 'oidc'
_RETRY_S = 60            # metadata unreachable: retry at most once a minute

_verrou = threading.Lock()
_etat = {'oauth': None, 'cle': None, 'metadata_ok': False, 'essai': 0.0}


def _metadata(url):
    """Fetch the provider metadata ourselves: some IdPs behind Cloudflare
    answer 403 to non-browser User-Agents, including authlib's."""
    try:
        return requests.get(url, timeout=15, headers={'User-Agent': _UA}).json()
    except Exception:
        return {}


def sso_client(app):
    """The OAuth client for the current configuration, or None when SSO is not
    configured (metadata URL + client ID + secret)."""
    c = oidc_config()
    if not c['enabled']:
        return None
    cle = (c['metadata_url'], c['client_id'], c['client_secret'])
    with _verrou:
        recharger = (cle != _etat['cle']
                     or (not _etat['metadata_ok'] and time.time() - _etat['essai'] > _RETRY_S))
        if recharger:
            from authlib.integrations.flask_client import OAuth
            if _etat['oauth'] is None:
                _etat['oauth'] = OAuth(app)
            oauth = _etat['oauth']
            md = _metadata(c['metadata_url'])
            if not md:
                app.logger.warning("SSO: OIDC metadata unreachable (%s)", c['metadata_url'])
            oauth._clients.pop(_NOM, None)          # authlib caches the built client
            oauth.register(
                name=_NOM,
                client_id=c['client_id'],
                client_secret=c['client_secret'],
                server_metadata_url=(c['metadata_url'] if not md else None),
                client_kwargs={'scope': 'openid profile email'},
                user_agent=_UA,
                **md,
            )
            _etat.update(cle=cle, metadata_ok=bool(md), essai=time.time())
        return _etat['oauth'].create_client(_NOM)
