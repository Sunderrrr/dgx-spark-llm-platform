"""Test helper: set Admin → Config values for the duration of a block.

LDAP, SSO and SMTP live in the database only (parametres.py) — patching the
environment no longer configures anything. `reglages(smtp_host='…')` writes the
settings as the admin panel would, and restores exactly what was there.
"""
from contextlib import contextmanager

import app as portal
import parametres


@contextmanager
def reglages(**valeurs):
    with portal.app.app_context():
        avant = {k: portal.get_db().execute(
            "SELECT value FROM settings WHERE key=?", ('cfg_' + k,)).fetchone() for k in valeurs}
        for k, v in valeurs.items():
            parametres.definir(k, v if v else parametres.EFFACER)
    try:
        yield
    finally:
        with portal.app.app_context():
            for k, row in avant.items():
                parametres.definir(k, row['value'] if row else parametres.EFFACER)
