"""Test helper: set Admin → Config values for the duration of a block.

LDAP, SSO and SMTP live in the database only (parametres.py) — patching the
environment no longer configures anything. `reglages(smtp_host='…')` writes the
settings as the admin panel would, and restores exactly what was there.
"""
from contextlib import contextmanager

import app as portal
import instance_config


@contextmanager
def live_settings(**values):
    with portal.app.app_context():
        before = {k: portal.get_db().execute(
            "SELECT value FROM settings WHERE key=?", ('cfg_' + k,)).fetchone() for k in values}
        for k, v in values.items():
            instance_config.set_value(k, v if v else instance_config.CLEAR)
    try:
        yield
    finally:
        with portal.app.app_context():
            for k, row in before.items():
                instance_config.set_value(k, row['value'] if row else instance_config.CLEAR)
