"""In-flight request activity, for the portal's admin panel.

**Why this file exists.** LiteLLM only writes its SpendLogs row at the END of
a request, and exposes no in-flight request endpoint (measured on
2026-09-14: 518 published routes, none lists the active ones; `/metrics`
answers 404, the Prometheus callback being inactive). The portal could thus
only answer « who finished recently », never « who is generating right now »
— while the engine clearly saw 2 to 4 busy sessions. Same-day measurements:
**44 minutes without a single SpendLogs row** while two sessions were
working, one agentic request lasting **3 min 17 s** for 22 281 input
tokens.

This callback records the request at START and removes it at the end. Three rules:

- an error here must **never** fail a request: everything is wrapped, the
  incident goes to stderr and the request continues;
- **the SYNCHRONOUS hooks are the ones that count**: in this version of LiteLLM,
  `async_log_pre_api_call` is indeed declared by `CustomLogger` but **never
  called** (checked in the installed package: the only occurrence is its
  definition). The pre-call comes from `litellm.input_callback`, and the
  success event from `litellm.callbacks` — two lists that invoke
  `log_pre_api_call` / `log_success_event`, the SYNCHRONOUS methods. A class
  that only implements the async variants therefore receives **nothing**, and
  that is exactly what happened on 2026-09-14: the table existed, stayed
  empty, and no error was raised. The synchronous write costs ~1 ms (two
  statements): the price to pay to be called;
- the file is shared with the portal, which mounts it **read-only**: a single
  writer (here), and nothing on this side can alter the portal.

**Why a SQLite and not the LiteLLM database**: the proxy image contains no
PostgreSQL driver — neither `psycopg2`, nor `psycopg`, nor `asyncpg` (checked)
— and `prisma` cannot be used directly. The portal already reads
SpendLogs from Postgres; it will read this file as well.

What is stored is deliberately RAW (key alias, user_id, model): it is the
portal that resolves an alias into an account name, with the same rule as for
SpendLogs. A single resolution logic, hence a single place to get it wrong.
"""
import asyncio
import os
import sqlite3
import sys
import time

CHEMIN = os.environ.get('CRONOS_INFLIGHT_DB', '/run/cronos/inflight.db')
# Past this point the row is considered orphaned: a client killed mid-flight
# triggers neither success nor failure, so nobody would come and remove it.
PEREMPTION_S = float(os.environ.get('CRONOS_INFLIGHT_TTL', '7200'))

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:                                    # hors conteneur LiteLLM
    class CustomLogger:                              # (tests) : hooks inertes
        pass


def _ouvre():
    dossier = os.path.dirname(CHEMIN)
    if dossier:
        os.makedirs(dossier, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(CHEMIN, timeout=2)
    # 0600: key aliases and account names are business data (2026-10-03 scan).
    try:
        os.chmod(CHEMIN, 0o600)
    except OSError:
        pass
    conn.execute('CREATE TABLE IF NOT EXISTS en_vol ('
                 'cle TEXT PRIMARY KEY, alias TEXT, user_id TEXT, '
                 'modele TEXT, debut REAL NOT NULL)')
    return conn


def _identite(kwargs):
    """(key, alias, user_id, model) as LiteLLM gives them for a request.

    The key is LiteLLM's call identifier: it is what allows removing exactly
    the opened row, without depending on the model or the alias.
    """
    params = (kwargs or {}).get('litellm_params') or {}
    meta = params.get('metadata') or {}
    cle = (kwargs.get('litellm_call_id') or meta.get('litellm_call_id')
           or meta.get('user_api_key') or '')
    return (str(cle),
            str(meta.get('user_api_key_alias') or ''),
            str(meta.get('user_api_key_user_id') or meta.get('user_id') or ''),
            str(kwargs.get('model') or meta.get('model') or ''))


def _enregistre(kwargs):
    """Records a request opening. Returns 1 if a row was written."""
    cle, alias, user_id, modele = _identite(kwargs)
    if not cle:
        return 0
    conn = _ouvre()
    try:
        conn.execute('DELETE FROM en_vol WHERE debut < ?', (time.time() - PEREMPTION_S,))
        conn.execute('INSERT OR REPLACE INTO en_vol (cle, alias, user_id, modele, debut) '
                     'VALUES (?,?,?,?,?)', (cle, alias, user_id, modele, time.time()))
        conn.commit()
        return 1
    finally:
        conn.close()


def _retire(kwargs):
    """Removes the row of a finished request, on success as on failure."""
    cle, _, _, _ = _identite(kwargs)
    if not cle:
        return 0
    conn = _ouvre()
    try:
        cur = conn.execute('DELETE FROM en_vol WHERE cle = ?', (cle,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


class ActiviteEnVol(CustomLogger):
    """LiteLLM hooks: opening at start, removal at the end (success or failure).

    The SYNCHRONOUS methods are the only ones this version calls (see the
    header); the async ones are kept for the paths that still use them, and
    both go through the same `_enregistre` / `_retire` functions, so a single
    write logic.
    """

    # ── Paths actually taken by the proxy ───────────────────────────────────
    def log_pre_api_call(self, model=None, messages=None, kwargs=None):
        self._sur(_enregistre, kwargs)

    def log_success_event(self, kwargs=None, response_obj=None,
                          start_time=None, end_time=None):
        self._sur(_retire, kwargs)

    def log_failure_event(self, kwargs=None, response_obj=None,
                          start_time=None, end_time=None):
        self._sur(_retire, kwargs)

    # ── Asynchronous variants (other versions / other paths) ───────────────
    async def async_log_pre_api_call(self, model, messages, kwargs):
        await self._hors_boucle(_enregistre, kwargs)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await self._hors_boucle(_retire, kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await self._hors_boucle(_retire, kwargs)

    @staticmethod
    def _sur(fn, kwargs):
        """Synchronous call: a write error must never propagate."""
        try:
            fn(kwargs)
        except Exception as e:                        # noqa: BLE001 — never fatal
            print('cronos_inflight: %s: %s' % (type(e).__name__, e), file=sys.stderr)

    @staticmethod
    async def _hors_boucle(fn, kwargs):
        try:
            await asyncio.to_thread(fn, kwargs)
        except Exception as e:                        # noqa: BLE001 — never fatal
            print('cronos_inflight: %s: %s' % (type(e).__name__, e), file=sys.stderr)


en_vol = ActiviteEnVol()
