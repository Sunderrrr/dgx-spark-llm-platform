"""Consumption statistics: aggregates, rankings, active users.

Extracted from app.py on 28/08. The monolith's « Statistiques » banner
really covered two topics: these computations, and the whole management of
models and sidecars (34 routes). Only the computations are here — no
route, hence no renamed endpoint and no url_for to requalify.

The data comes from two sources mixed on purpose: the LiteLLM database
(Postgres) for FINISHED requests, and the in-flight registry (SQLite) for
ongoing ones — LiteLLM only writes its row at the end, so without this
registry a user mid-generation would stay invisible.
"""
import os
import re
import requests
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import LITELLM_URL, LOCAL_TZ
from db import DB_PATH, _spend_conn, get_db
from litellm_client import litellm_headers
from vllm_health import vllm_health

# The rate is now 1:1 (input=1, output=1) → SpendLogs.spend ≈ real tokens
# for recent requests. We still sum prompt_tokens+completion_tokens
# directly: exact even for history priced at input×0.1. startTime UTC → LOCAL_TZ.

# Pseudo-keys that don't correspond to a user (admin/health calls).
_NON_USER_KEYS = {'litellm_proxy_master_key', 'None', ''}

# What a ranking can be based on: both together, or either one.
RANKING_METRICS = ('total', 'completion', 'prompt')


def _real_tokens_by_user(since_utc=None):
    """Real tokens (prompt + generated) per user, from SpendLogs. If
    `since_utc` (naive UTC datetime) is provided, only counts since that instant —
    used to align the displayed consumption with the (daily) budget period.
    """
    conn = _spend_conn()
    if not conn:
        return {}
    try:
        umap = _key_user_map(conn)
        cur = conn.cursor()
        q = ('SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
             'FROM "LiteLLM_SpendLogs"')
        params = []
        if since_utc is not None:
            q += ' WHERE "startTime" >= %s'
            params.append(since_utc)
        q += ' GROUP BY api_key'
        cur.execute(q, params)
        out = {}
        for api_key, toks in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key)
            if u:
                out[u] = out.get(u, 0) + int(toks or 0)
        return out
    except Exception:
        return {}
    finally:
        conn.close()

# In-flight in-app model requests (Playground / Support), tracked in real time in
# a shared SQLite table (NOT in-memory: gunicorn runs several workers, so the
# admin's /api/home may hit a different worker than the one streaming). SpendLogs
# only records a request at its END, so a long generation shows GPU activity
# ("Sessions X/Y") with nobody in the "who's using" panel until it finishes —
# this registry fills that gap live. One row per active request; a staleness
# sweep drops rows a crashed worker never deleted.
_TOKENS_BY_MODEL_CACHE = {}
_TOKENS_BY_MODEL_TTL = 30.0


def _tokens_by_model(since_utc=None):
    """Real tokens per (chat) model, since `since_utc` (naive UTC). Feeds the
    dashboard's « usage par modèle » — see what consumes the GPU, beyond the
    per-user breakdown.

    Cached 30 s like `user_hourly`: `/api/home` is polled every 5 s and this
    `GROUP BY` scans the WHOLE `LiteLLM_SpendLogs` table (50 000 rows on
    2026-09-14). The value is a cumulative, it does not move by the second.
    """
    cle = since_utc.isoformat() if since_utc is not None else ''
    hit = _TOKENS_BY_MODEL_CACHE.get(cle)
    if hit and time.time() - hit[0] < _TOKENS_BY_MODEL_TTL:
        return hit[1]
    conn = _spend_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        q = ('SELECT model, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
             'FROM "LiteLLM_SpendLogs"')
        params = []
        if since_utc is not None:
            q += ' WHERE "startTime" >= %s'
            params.append(since_utc)
        q += ' GROUP BY model ORDER BY 2 DESC'
        cur.execute(q, params)
        resultat = [{'model': model or 'inconnu', 'tokens': int(toks or 0)}
                    for model, toks in cur.fetchall()]
        _TOKENS_BY_MODEL_CACHE[cle] = (time.time(), resultat)
        return resultat
    except Exception:
        return []
    finally:
        conn.close()


def _inflight_start(username):
    rid = secrets.token_hex(8)
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("INSERT INTO inflight_requests (id, username, started_at) VALUES (?,?,?)",
                  (rid, username, time.time()))
        c.commit(); c.close()
    except Exception:
        pass
    return rid

def _inflight_end(rid):
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("DELETE FROM inflight_requests WHERE id=?", (rid,))
        c.commit(); c.close()
    except Exception:
        pass

def _inflight_tokens(rid, tokens):
    """Tokens generated so far by an in-flight request (live decode gauge)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("UPDATE inflight_requests SET tokens=? WHERE id=?", (int(tokens), rid))
        c.commit()
        c.close()
    except Exception:                                        # noqa: BLE001
        pass


def _inflight_snapshot():
    out = {}
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("DELETE FROM inflight_requests WHERE started_at < ?", (time.time() - 900,))  # staleness sweep
        for u, n in c.execute("SELECT username, COUNT(*) FROM inflight_requests GROUP BY username").fetchall():
            out[u] = n
        c.commit(); c.close()
    except Exception:
        pass
    return out


def debit_decode_live():
    """Live decode throughput of the IN-FLIGHT requests, in tok/s.

    TabbyAPI publishes no /metrics and its stream carries NO token counter
    (usage arrives once, at the very end — verified on a real stream): the only
    live source is what the portal's relay sees. Each in-flight request reports
    `tokens` (~ chars/4, the composer's live convention) and this sums
    `tokens / age` over the ongoing requests: the average decode rate of what
    the model is producing RIGHT NOW. It reads 0 during a prefill (nothing
    generated yet) and the row goes away with the request. Same display
    semantics as the llama.cpp gauge (Δn_decode_total / Δt), one probe less.
    """
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        now = time.time()
        total = 0.0
        for tok, debut in c.execute(
                "SELECT tokens, started_at FROM inflight_requests WHERE tokens > 0"):
            age = now - float(debut)
            if age > 1:
                total += float(tok) / age
        c.close()
        return round(total, 1)
    except Exception:                                        # noqa: BLE001
        return None


def enregistrer_prefill(prompt_tokens, ttft_s):
    """Rate of the LAST prefill, in tok/s (gauge, like llamacpp's
    `prompt_tokens_seconds`: the last value seen, not a current average).

    The engine gives no prefill counter (TabbyAPI has no /metrics), but the
    portal measured the TTFT itself and `usage.prompt_tokens` is exact at the
    end of the request: their ratio IS the observed prefill speed. Caveat kept
    in mind: TTFT also carries any queueing delay, so a busy engine reads a
    lower prefill than its raw speed.
    """
    try:
        if prompt_tokens and ttft_s and ttft_s > 0:
            from db import set_setting
            set_setting('prefill_rate_last', round(float(prompt_tokens) / float(ttft_s), 1))
            set_setting('prefill_rate_at', time.time())
    except Exception:                                        # noqa: BLE001
        pass


def prefill_dernier(max_age_s=120):
    """Last observed prefill rate (tok/s), or None when nothing is fresh.

    Fading with age keeps the house convention: the UI shows NOTHING rather
    than a zero when there is no real measurement to show.
    """
    try:
        from db import get_setting
        v = get_setting('prefill_rate_last')
        ts = get_setting('prefill_rate_at')
        if v is None or ts is None:
            return None
        return round(float(v), 1) if time.time() - float(ts) < max_age_s else None
    except Exception:                                        # noqa: BLE001
        return None


# ── Cumulative generated tokens, across engine counter resets ─────────────
# The llama.cpp and vLLM counters restart from zero at every startup:
# « tokens générés » thus fell back to 0 at every model change or restart,
# wiping the machine's history. We keep the last value seen for each
# model, and archive it when the counter moves BACKWARDS — such a counter
# being monotone, a drop can only be a reset, and the work already done
# stays owed.
#
# MEASUREMENT of 2026-09-14, not to be simplified into « c'est une
# relance »: `tokens_predicted_total` went from 158 864 to 47 and
# `n_decode_total` from 132 694 to 271 **without the process changing**
# (same pid, 7 h 56 of life, NRestarts=0), while `prompt_tokens_total`
# stayed at 174 932. So it is not only a restart: a KV cache reset yields
# the same signature. Whatever the cause — in both cases the cumulative
# must keep growing, and that is what the archiving does.
#
# What the figure is exactly worth: it is exact up to the sampling step.
# The last observation is at most one probe period old (/api/modelhealth at
# 1 s when a dashboard is open, /api/home at 5 s otherwise), so we lose at
# worst the tokens generated during those few seconds before the reset.
# For an accounting figure, the reference is on the LiteLLM side —
# SUM(completion_tokens) on SpendLogs read 21 984 293 on 2026-09-14, and covers the whole platform.
def cumuler_tokens_generes(modele, valeur):
    """Total of the tokens generated by this model, resets included.

    Returns None if the model is unknown or the database unreachable: the
    caller then displays the current launch's counter, never a zero that
    would suggest a reset.
    """
    if not modele or valeur is None:
        return None
    valeur = int(valeur)
    try:
        db = get_db()
        row = db.execute("SELECT base, dernier FROM model_counters WHERE model=?",
                         (modele,)).fetchone()
        if row is None:
            base = 0
        elif valeur >= row['dernier']:
            base = row['base']                      # counter still running
        else:
            base = row['base'] + row['dernier']     # reset: we archive the previous one
        # We only write when something changes: at rest (frozen counter), the
        # 1 s probe must not produce one SQLite write per second.
        # `base != row['base']` was a third, never decisive term: if the first
        # two are false, then `valeur == row['dernier']`, so the « counter still
        # running » branch was taken and `base` already equals
        # `row['base']`.
        if row is None or valeur != row['dernier']:
            db.execute(
                "INSERT INTO model_counters (model, base, dernier, maj) VALUES (?,?,?,?) "
                "ON CONFLICT(model) DO UPDATE SET base=excluded.base, "
                "dernier=excluded.dernier, maj=excluded.maj",
                (modele, base, valeur, time.time()))
            db.commit()
        return base + valeur
    except Exception:
        return None


def _compte_existe(nom):
    """Does this name match an account known to the platform?

    Only used to VALIDATE a name guessed from an API key alias: without this
    check any alias fragment would display as if it were a user. user_prefs,
    and not local_users: LDAP/SSO accounts have no local row, only
    user_prefs sees them all.
    """
    if not nom:
        return False
    try:
        return get_db().execute(
            "SELECT 1 FROM user_prefs WHERE username=? LIMIT 1", (nom,)).fetchone() is not None
    except Exception:
        return False


# 180 s and not 120: the in-flight registry covers ONLY the portal routes
# (Playground/Support). An API client goes through Traefik -> LiteLLM
# without ever crossing the portal: its only trace is SpendLogs, written
# at request END. The measured agentic requests last 100 to 124 s, so
# below ~150 s such a client vanished between two calls while running
# non-stop. Not more than 180 s either: beyond, the panel keeps names of
# long-gone users. It is the guard on engine activity that really bounds
# — engine idle, panel empty, whatever the window.
def _compte_depuis_alias(alias):
    """Account name guessed from an API key alias, or '' when doubtful.

    Aliases of the form « alice-1783112817 » or « Opencode-Omarchy »: we
    guess NOTHING, we only keep a fragment when it matches a known account —
    otherwise we prefer showing the key to inventing a name. Rule shared
    with SpendLogs: a single resolution logic, hence a single place to get
    it wrong.
    """
    for morceau in re.split(r'[-_]', alias or ''):
        if morceau and _compte_existe(morceau):
            return morceau
    return ''


# File written by the LiteLLM callback `cronos_inflight` and mounted here
# READ-ONLY. It holds the IN-FLIGHT requests, i.e. the only source able to
# name someone WHILE they generate: LiteLLM only writes its SpendLogs row
# at the end (measured on 2026-09-14: 44 minutes without a single row
# while two sessions worked). Absent => we fall back on the finished rows;
# the writing happens on the LiteLLM side, the portal only reads.
_EN_VOL_DB = os.environ.get('CRONOS_INFLIGHT_DB', '/run/cronos/inflight.db')


def _en_vol():
    """In-flight requests: {account: age_in_seconds}.

    Never raises and guesses nothing: a display comfort, not a dependency.
    A missing or unreadable file gives {} — and the UI already knows how to
    say « sessions occupees, identites pas encore journalisees ».
    """
    conn = None
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % _EN_VOL_DB, uri=True, timeout=2)
        maintenant = time.time()
        out = {}
        for alias, user_id, debut in conn.execute('SELECT alias, user_id, debut FROM en_vol'):
            # user_id is the account for keys created by the portal, but a key
            # created elsewhere can carry anything: we validate, as for
            # the alias.
            u = _compte_depuis_alias(alias)
            if not u and user_id and _compte_existe(str(user_id)):
                u = str(user_id)
            if not u:
                continue
            try:
                age = max(0.0, maintenant - float(debut))
            except (TypeError, ValueError):
                age = 0.0
            if u not in out or age > out[u]:
                # The OLDEST request of this account wins: it is the one
                # carrying the useful information (« cette session tourne
                # depuis 40 min »). Keeping the most recent would make a
                # request stuck behind a two-second round-trip disappear.
                out[u] = age
        return out
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def _active_users(window_s=1800):
    """Users who queried the model recently, from two sources merged:
      - LiteLLM SpendLogs over the last `window_s` s (attributed by API key → user)
        — recent COMPLETED requests;
      - the live in-flight registry (in-app Playground/Support requests still
        streaming) — SpendLogs only writes at request end, so this shows the
        current user in real time. Such users are marked `live`.
    Feeds the admin "who's using the model" panel on the home page.

    Default window of 30 MINUTES, not 2: measured on 2026-09-14, an
    agentic request lasted 3 min 17 s and the engine stayed 44 minutes
    without a single row being written, while two sessions worked. On 2
    minutes, the panel thus displayed « personne » while a GPU ran at full
    tilt — the worst message since the admin concludes the machine is
    free. So we widen it, and every name carries its AGE (`derniere_s`):
    « il y a 40 min » cannot be confused with « il y a 4 s ».
    """
    # The panel must reflect REAL activity. Without this guard it kept names
    # displayed for the whole window while nothing ran anymore: the admin saw
    # « 0 / 8 sessions » yet two listed users. The engine is the only
    # authority on « est-ce que quelque chose tourne ».
    inflight = _inflight_snapshot()
    en_cours = 0
    try:
        h = vllm_health() or {}
        en_cours = int(h.get('running') or 0) + int(h.get('waiting') or 0)
    except Exception:
        # Engine unreachable: we do NOT empty the panel on a mere probe failure,
        # else a metrics error would make it look like nobody uses the model. We
        # fall back on the SpendLogs window alone.
        en_cours = -1
    if not inflight and en_cours == 0:
        return []
    agg = {}
    conn = _spend_conn()
    if conn:
        try:
            umap = _key_user_map(conn)
            cur = conn.cursor()
            since = datetime.now(ZoneInfo('UTC')).replace(tzinfo=None) - timedelta(seconds=window_s)
            # Filter on endTime, NOT on startTime. LiteLLM only writes the row at the
            # END of the request: by the time it becomes visible, its startTime is
            # already as old as the whole generation. Measured on 23/08 on an agentic
            # client (one account via an API key): requests of 100 to 124 s chained
            # without interruption, thus systematically outside a 120 s window
            # aligned on startTime — the user was invisible to the « qui utilise le
            # modele » panel while saturating the GPU continuously.
            # Two bounds, and it is DELIBERATE. Only startTime is indexed (not
            # endTime): filtering on COALESCE(endTime, startTime) alone forced a full
            # scan — 38 000 rows and 5,7 ms per call, worsening with every recorded
            # request. The wide bound on startTime lets Postgres use its index, the
            # fine bound on endTime keeps the accuracy for a long request. Measured
            # on 23/08: 5,741 ms -> 0,145 ms.
            # 1 h of margin: beyond, a single request that long does not exist.
            large = since - timedelta(seconds=3600)
            cur.execute('SELECT api_key, COUNT(*), '
                        'SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)), '
                        'MAX(COALESCE("user", \'\')), '
                        'MAX(COALESCE(metadata->>\'user_api_key_alias\', \'\')), '
                        'MAX(COALESCE("endTime", "startTime")) '
                        'FROM "LiteLLM_SpendLogs" '
                        'WHERE "startTime" >= %s AND COALESCE("endTime", "startTime") >= %s '
                        'GROUP BY api_key', (large, since))
            maintenant = datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
            for api_key, cnt, toks, col_user, alias, dernier in cur.fetchall():
                if api_key in _NON_USER_KEYS:
                    continue
                # Three attribution sources, from most to least reliable.
                # Before, a key missing from the mapping table was
                # SILENTLY ignored: its owner never appeared, with nothing
                # saying so. Yet a key created outside the portal (or before
                # metadata.user was added) has no such mapping.
                u = umap.get(api_key) or (col_user or '').strip()
                if not u and alias:
                    u = _compte_depuis_alias(alias)
                if not u:
                    u = f"cle {str(api_key)[:8]}…"
                a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0,
                                       'live': False, 'derniere_s': None})
                a['requests'] += int(cnt or 0)
                a['tokens'] += int(toks or 0)
                # Age of the last activity. This is THE figure answering the
                # admin's question (« qui genere la, maintenant ? »): the `live`
                # flag is deliberately coarse (it lights up as soon as the
                # engine works, thus for everyone at once as soon as there is
                # traffic), and two identically displayed users can be one
                # minute apart. We keep the minimum, i.e. the most recent
                # activity of that user.
                if dernier is not None:
                    try:
                        age = (maintenant - dernier).total_seconds()
                    except TypeError:
                        age = None
                    if age is not None and (a['derniere_s'] is None or age < a['derniere_s']):
                        a['derniere_s'] = max(0.0, age)
                if en_cours > 0 and a['derniere_s'] is not None and a['derniere_s'] < 15:
                    # The engine is processing something AND this user just
                    # emitted: it is them (or one of them). The in-flight
                    # registry only sees the portal, so without this an API client
                    # was NEVER marked « live ».
                    #
                    # The 15 s threshold is essential since the window went to
                    # 30 min: without it, `en_cours > 0` lit up « live » for
                    # everyone at once, including an account whose last request
                    # dated back 40 minutes. A doubt is no proof of
                    # activity.
                    a['live'] = True
        except Exception:
            pass
        finally:
            conn.close()
    # Merge live in-flight in-app requests (real time).
    for u, n in inflight.items():
        a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0,
                               'live': False, 'derniere_s': None})
        a['live'] = True
        # In-flight request: this is the present instant, by definition.
        a['derniere_s'] = 0.0
        if a['requests'] == 0:
            a['requests'] = n
    # IN-FLIGHT requests, as LiteLLM noted them at their start. This is the
    # only source naming a user WHILE they generate: the `live` flag set
    # above relies on « the engine works and this account just emitted », i.e.
    # on a deduction, while here it is observed. `depuis_s` says how long the
    # request has been running — a 40-minute request is not the same thing as
    # a one-second round-trip.
    for u, age in _en_vol().items():
        a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0,
                               'live': False, 'derniere_s': None})
        a['live'] = True
        a['en_vol'] = True
        a['depuis_s'] = age
        a['derniere_s'] = 0.0
    # Fields always present: the UI must not distinguish three dict shapes
    # depending on the source that created the entry.
    for a in agg.values():
        a.setdefault('en_vol', False)
        a.setdefault('depuis_s', None)
    return sorted(agg.values(), key=lambda x: (x['live'], x['requests']), reverse=True)

def _account_activity(username, days=182):
    """Daily series (prompt/generated tokens) for a user over `days`
    days, for the heatmap and the "My account" stats.
    """
    empty = {'days': [], 'total': 0, 'prompt': 0, 'completion': 0,
             'peak': 0, 'peak_day': None, 'active_days': 0, 'avg': 0}
    conn = _spend_conn()
    if not conn:
        return empty
    try:
        since_local = (datetime.now(ZoneInfo(LOCAL_TZ)) - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        since_utc = since_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        umap = _key_user_map(conn)
        mine = {k for k, u in umap.items() if u == username}
        if not mine:
            return empty
        cur = conn.cursor()
        cur.execute(
            'SELECT (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date AS d, '
            'SUM(COALESCE(prompt_tokens,0)), SUM(COALESCE(completion_tokens,0)) '
            'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s AND api_key = ANY(%s) '
            'GROUP BY d ORDER BY d',
            (LOCAL_TZ, since_utc, list(mine)))
        rows = cur.fetchall()
    except Exception:
        return empty
    finally:
        try:
            conn.close()
        except Exception:
            pass
    by_day = {str(d): {'prompt': int(p or 0), 'completion': int(c or 0),
                       'tokens': int((p or 0) + (c or 0))} for d, p, c in rows}
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    series = []
    for i in range(days):
        d = str(today - timedelta(days=days - 1 - i))
        series.append({'date': d, 'tokens': by_day.get(d, {}).get('tokens', 0)})
    total = sum(v['tokens'] for v in by_day.values())
    active = [v for v in by_day.values() if v['tokens'] > 0]
    peak_day = max(by_day.items(), key=lambda kv: kv[1]['tokens'], default=(None, {'tokens': 0}))
    return {
        'days': series,
        'total': total,
        'prompt': sum(v['prompt'] for v in by_day.values()),
        'completion': sum(v['completion'] for v in by_day.values()),
        'peak': peak_day[1]['tokens'],
        'peak_day': peak_day[0],
        'active_days': len(active),
        'avg': round(total / len(active)) if active else 0,
    }


_key_user_map_cache = {'t': 0.0, 'v': None}

def _key_user_map(conn):
    """token(hash) -> username, from the keys' metadata (active + deleted).

    Reads the three key tables on every call — and this is called by
    user_hourly, _active_users and the admin consumption view, i.e. on every
    /api/home and /api/admin poll. The mapping only changes when an admin
    creates/revokes a key, so cache it briefly (~30 s) to stop a multi-SELECT
    scan per poll. A new key's attribution may lag up to 30 s (acceptable);
    newly-created keys only get used after that in any case.
    """
    now = time.time()
    if _key_user_map_cache['v'] is not None and now - _key_user_map_cache['t'] < 30:
        return _key_user_map_cache['v']
    mapping = {}
    cur = conn.cursor()
    for table in ('LiteLLM_VerificationToken', 'LiteLLM_DeletedVerificationToken',
                  'LiteLLM_DeprecatedVerificationToken'):
        try:
            cur.execute(f"SELECT token, metadata->>'user' FROM \"{table}\"")
            for token, user in cur.fetchall():
                if token and user and token not in mapping:
                    mapping[token] = user
        except Exception:
            pass
    _key_user_map_cache.update(t=time.time(), v=mapping)
    return mapping

def _comptes_connus():
    """Accounts that really exist: local, or recorded as a login source.

    Used to tell a PERSON from a leftover. Keys created for a test — or
    deleted by hand — leave consumption rows in the name of an account that
    exists nowhere: ranking them among users gives them a rank, and one day
    a medal. Measured on 2026-09-14: `zz-pwtest`, leftover of a password
    policy test (15 requests, 8 633 tokens), appeared in the public
    ranking.

    Same precaution as `auth.etat_compte`: if the local database is
    unreadable we return None and the caller concludes NOTHING. Missing
    data is not proof of absence — better one row too many than a real
    account unmasked as a leftover.
    """
    try:
        db = get_db()
        noms = {r['username'] for r in db.execute('SELECT username FROM local_users')}
        noms |= {r['username'] for r in db.execute('SELECT DISTINCT username FROM user_sources')}
        return noms
    except Exception:
        return None

def _valeur_metrique(prompt, completion, metric):
    """The figure we RANK on: the input, the generated, or both."""
    if metric == 'completion':
        return completion or 0
    if metric == 'prompt':
        return prompt or 0
    return (prompt or 0) + (completion or 0)

# Month arithmetic for the « 12 derniers mois » and « depuis le début »
# periods: timedelta knows no months (unequal lengths), and we avoid
# adding dateutil for so little.
_MIDNIGHT = {'hour': 0, 'minute': 0, 'second': 0, 'microsecond': 0}

def relativedelta_months(n, day=None):
    """Shift of n months, applicable to a datetime via `dt + relativedelta_months(n)`."""
    class _Shift:
        def __radd__(self, dt):
            y, m = dt.year, dt.month + n
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            return dt.replace(year=y, month=m, day=day if day else min(dt.day, 28))
    return _Shift()

def _month_buckets(start, end):
    """List of the 1sts of the month from `start` to `end` included (sparkline keys)."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1; y += 1
    return out

def ranking_full(period='day', me=None, metric='total'):
    """Enriched ranking: really consumed tokens, share of the period total,
    delta vs the previous period, input/generated breakdown and trend,
    per account.

    `metric` chooses what is RANKED: 'total' (input + generated),
    'completion' (generated) or 'prompt' (input). The choice is not
    cosmetic — measured on 2026-09-14 over 30 days: the #1 concentrated
    32,2 % of the total but only 0,46 % of the generated. A « total »
    ranking thus mostly ranks those who SEND context, and the order
    really changes when ranking the generated (one account leaves the top 5).

    Rows matching no account (`inconnu`, test key leftovers) leave the
    ranking: no rank nor medal, and they do not count in the active
    accounts. They stay in the period TOTAL, which describes the platform
    — not the ranking.
    """
    metric = metric if metric in RANKING_METRICS else 'total'
    conn = _spend_conn()
    empty = {'period': period, 'metric': metric, 'rows': [], 'active_count': 0,
             'total': 0, 'avg': 0, 'has_prev': period != 'all'}
    if not conn:
        return empty
    try:
        now_local = datetime.now(ZoneInfo(LOCAL_TZ))
        today = now_local.date()
        if period == 'week':
            cur_start = now_local - timedelta(days=7)
            prev_start = now_local - timedelta(days=14)
            buckets = [today - timedelta(days=i) for i in range(6, -1, -1)]
            bucket_kind = 'day'
        elif period == 'month':
            cur_start = now_local - timedelta(days=30)
            prev_start = now_local - timedelta(days=60)
            buckets = [today - timedelta(days=i) for i in range(29, -1, -1)]
            bucket_kind = 'day'
        elif period in ('year', 'all'):
            # MONTHLY buckets: over a year, 365 points would make an unreadable
            # sparkline (and 30x more rows to aggregate on the SQL side).
            if period == 'year':
                cur_start = now_local.replace(**_MIDNIGHT) + relativedelta_months(-11, day=1)
                prev_start = cur_start + relativedelta_months(-12)
            else:
                # Since the start: we begin at the very first log (otherwise, this month).
                c0 = conn.cursor()
                c0.execute('SELECT MIN("startTime") FROM "LiteLLM_SpendLogs"')
                first = (c0.fetchone() or [None])[0]
                start_date = first.date().replace(day=1) if first else today.replace(day=1)
                cur_start = now_local.replace(**_MIDNIGHT).replace(
                    year=start_date.year, month=start_date.month, day=1)
                prev_start = cur_start  # no previous period → no delta
            buckets = _month_buckets(cur_start.date(), today)
            bucket_kind = 'month'
        else:  # day
            cur_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            prev_start = cur_start - timedelta(days=1)
            buckets = list(range(24))
            bucket_kind = 'hour'
        cur_start_utc = cur_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        prev_start_utc = prev_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        umap = _key_user_map(conn)
        connus = _comptes_connus()
        cur = conn.cursor()
        if bucket_kind == 'hour':
            bexpr = "EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::int"
        elif bucket_kind == 'month':
            bexpr = "date_trunc('month', ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::date"
        else:
            bexpr = "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s)::date"
        # Current period: per bucket AND per key. BOTH counters are
        # fetched, not only their sum: this is what allows ranking on
        # the input, the generated, or both.
        cur.execute(
            f'SELECT {bexpr} AS b, api_key, SUM(prompt_tokens), SUM(completion_tokens) '
            'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s GROUP BY b, api_key',
            (LOCAL_TZ, cur_start_utc))
        agg = {}
        for b, api_key, prompt, comp in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'inconnu')
            p, c = prompt or 0, comp or 0
            a = agg.setdefault(u, {'tokens': 0, 'prompt': 0, 'completion': 0, 'spark': {}})
            a['tokens'] += p + c; a['prompt'] += p; a['completion'] += c
            # The trend follows the displayed metric, not the total: a context
            # curve does not have the same shape as a generation curve.
            v = _valeur_metrique(p, c, metric)
            if v:
                a['spark'][b] = a['spark'].get(b, 0) + v
        # Previous period, same metric: a delta compares what is
        # comparable. « Depuis le debut » has nothing before it — we skip the
        # query, and `has_prev` warns the UI that there is no delta to
        # display. The old version displayed « nouveau » on ALL rows of
        # that period: for lack of distinguishing « no previous period »
        # from « account absent the period before ».
        prev = {}
        if period != 'all':
            cur.execute('SELECT api_key, SUM(COALESCE(prompt_tokens,0)), SUM(COALESCE(completion_tokens,0)) '
                        'FROM "LiteLLM_SpendLogs" '
                        'WHERE "startTime" >= %s AND "startTime" < %s GROUP BY api_key',
                        (prev_start_utc, cur_start_utc))
            for api_key, p, c in cur.fetchall():
                if api_key in _NON_USER_KEYS:
                    continue
                u = umap.get(api_key, 'inconnu')
                prev[u] = prev.get(u, 0) + _valeur_metrique(p, c, metric)
        # The metric is computed ONCE per account: it serves the sort, the
        # total, the share and the delta.
        for a in agg.values():
            a['value'] = _valeur_metrique(a['prompt'], a['completion'], metric)

        def est_residu(u):
            """Row that is nobody's account — see `_comptes_connus`."""
            return u == 'inconnu' or (connus is not None and u not in connus)

        def ligne(u, a, rang):
            pv = prev.get(u, 0)
            residu = est_residu(u)
            return {
                'rank': rang, 'username': u, 'is_me': u == me,
                'is_unattributed': residu,
                'value': int(a['value']),                 # what we rank on
                'tokens': int(a['tokens']),
                'prompt': int(a['prompt']), 'completion': int(a['completion']),
                'share_pct': (a['value'] / total * 100) if total else 0,
                # No delta on an unattributed row: its « +290 919 % » of the
                # month compares nothing useful, and a leftover has no history.
                'delta': None if residu or not pv else (a['value'] - pv) / pv * 100,
                'trend': [a['spark'].get(b, 0) for b in buckets],
            }

        actifs = [(u, a) for u, a in agg.items() if a['value'] > 0 and not est_residu(u)]
        autres = [(u, a) for u, a in agg.items() if a['value'] > 0 and est_residu(u)]
        actifs.sort(key=lambda x: x[1]['value'], reverse=True)
        autres.sort(key=lambda x: x[1]['value'], reverse=True)
        total = sum(a['value'] for _, a in actifs + autres)
        total_actifs = sum(a['value'] for _, a in actifs)
        rows = [ligne(u, a, i + 1) for i, (u, a) in enumerate(actifs)]
        rows += [ligne(u, a, None) for u, a in autres]
        return {'period': period, 'metric': metric, 'rows': rows,
                'active_count': len(actifs), 'total': round(total),
                'avg': round(total_actifs / len(actifs)) if actifs else 0,
                'has_prev': period != 'all'}
    except Exception:
        return empty
    finally:
        conn.close()

_USER_HOURLY_CACHE = {}
_USER_HOURLY_TTL = 30.0

def user_hourly(username):
    """24 hourly points (real tokens consumed = prompt + generated) for today
    for the user, + total, hourly peak and number of active keys in the
    day. We show real tokens, not the weighted cost (input×0.1) which
    underestimates consumption by ~10× on prompt-heavy loads.
    """
    # The Postgres SpendLogs GROUP BY runs on every call (home refresh, hourly
    # admin endpoint, support). Usage barely changes second-to-second, so cache
    # per user for a short window; _key_user_map is already cached separately.
    now = time.time()
    hit = _USER_HOURLY_CACHE.get(username)
    if hit and now - hit[0] < _USER_HOURLY_TTL:
        return hit[1]
    conn = _spend_conn()
    if not conn:
        return None
    empty = {'has_data': False, 'points': [{'hour': h, 'tokens': 0} for h in range(24)],
             'total': 0, 'peak_hour': 0, 'peak_val': 0, 'active_keys': 0}
    try:
        umap = _key_user_map(conn)
        my_keys = {tok for tok, u in umap.items() if u == username}
        if not my_keys:
            _USER_HOURLY_CACHE[username] = (time.time(), empty)
            return empty
        cur = conn.cursor()
        cur.execute(
            'SELECT EXTRACT(HOUR FROM (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s))::int AS h, '
            'api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
            'FROM "LiteLLM_SpendLogs" '
            'WHERE api_key = ANY(%s) '
            '  AND (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date '
            '      = (now() AT TIME ZONE %s)::date '
            'GROUP BY h, api_key', (LOCAL_TZ, list(my_keys), LOCAL_TZ, LOCAL_TZ))
        by_hour = {h: 0 for h in range(24)}
        active = set()
        for h, api_key, toks in cur.fetchall():
            by_hour[h] += (toks or 0)
            if toks:
                active.add(api_key)
        peak_hour = max(range(24), key=lambda h: by_hour[h])
        total = sum(by_hour.values())
        result = {'has_data': total > 0,
                  'points': [{'hour': h, 'tokens': round(by_hour[h])} for h in range(24)],
                  'total': round(total), 'peak_hour': peak_hour,
                  'peak_val': round(by_hour[peak_hour]), 'active_keys': len(active)}
        _USER_HOURLY_CACHE[username] = (time.time(), result)
        return result
    except Exception:
        return empty
    finally:
        conn.close()


# ── Per-sidecar usage, for the administration ──────────────────────────────
# Brought over from the « Apercu » banner on 28/08, where they had nothing
# to do: these are consumption aggregates, like the rest of this module.

def admin_get_user_consumption():
    """Consumption per ACCOUNT: number of keys (local DB) + spend/budget at the
    LiteLLM user level, fetched in ONE /user/list call (instead of one call per key and
    per user — which blocked the admin page render).
    """
    counts = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT username, COUNT(*) c FROM api_keys GROUP BY username"):
            counts[r['username']] = r['c']
        conn.close()
    except Exception:
        pass
    users = {}
    try:
        r = requests.get(f"{LITELLM_URL}/user/list", headers=litellm_headers(),
                         params={"page_size": 100}, timeout=6)
        if r.ok:
            for u in r.json().get('users', []):
                uid = u.get('user_id')
                if uid not in counts:
                    continue  # only display accounts that have keys here
                mb = u.get('max_budget')
                users[uid] = {'username': uid, 'spend': u.get('spend') or 0,
                              'max_budget': mb if mb is not None else 0,
                              'unlimited': mb is None, 'key_count': counts[uid]}
    except Exception:
        pass
    # Accounts with keys but no LiteLLM user object → shown anyway.
    for uname, c in counts.items():
        users.setdefault(uname, {'username': uname, 'spend': 0, 'max_budget': 0,
                                 'unlimited': False, 'key_count': c})
    # Real tokens consumed (prompt + generated) over the current budget period.
    # The budget is daily and resets at 00:00 UTC → we only count
    # since the start of the UTC day, so "consumed" is comparable to
    # "budget / day" (otherwise we showed the all-time cumulative > budget).
    day_start = (datetime.now(ZoneInfo('UTC'))
                 .replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
    toks = _real_tokens_by_user(day_start)
    for uid, u in users.items():
        u['tokens'] = toks.get(uid, 0)
    return sorted(users.values(), key=lambda u: u['tokens'], reverse=True)

def admin_get_ocr_usage():
    """OCR and video never go through a LiteLLM API key (internal backend,
    not exposed — cf. get_ocr_model()/comfyui_is_up()): LiteLLM_SpendLogs knows
    nothing about them. Only the local ocr_jobs/video_jobs tables know who
    uses them.
    """
    rows = get_db().execute(
        "SELECT username, COUNT(*) AS c, MAX(created_at) AS last "
        "FROM ocr_jobs GROUP BY username ORDER BY c DESC"
    ).fetchall()
    return [dict(r) for r in rows]

def admin_get_video_usage():
    rows = get_db().execute(
        "SELECT username, COUNT(*) AS c, MAX(created_at) AS last "
        "FROM video_jobs GROUP BY username ORDER BY c DESC"
    ).fetchall()
    return [dict(r) for r in rows]

def admin_get_voice_usage():
    rows = get_db().execute(
        "SELECT username, COUNT(*) AS c, MAX(created_at) AS last "
        "FROM voice_jobs GROUP BY username ORDER BY c DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── TTFT actually measured ────────────────────────────────────────────────
# llama.cpp publishes no TTFT in /metrics, but it returns a `timings` PER
# REQUEST (including in the last SSE fragment), whose `prompt_ms` is
# exactly the time before the first token. The portal relays every
# generation: it is thus the only place able to keep an average.
#
# Exponential moving average, stored in `settings`: shared between the
# gunicorn workers (a process-memory average would give a different
# figure at every poll depending on the worker hit), bounded by
# construction, and it follows the evolution instead of being wiped by history.
_TTFT_ALPHA = 0.2


def enregistrer_ttft(ms):
    """Integrates a TTFT measurement (milliseconds) into the moving average."""
    try:
        ms = float(ms)
        if ms <= 0 or ms > 600_000:      # guard: aberrant measurement ignored
            return
        from db import get_setting, set_setting
        ancien = get_setting('ttft_ms_ewma')
        nouveau = ms if ancien is None else (1 - _TTFT_ALPHA) * float(ancien) + _TTFT_ALPHA * ms
        set_setting('ttft_ms_ewma', round(nouveau, 1))
    except Exception:                                        # noqa: BLE001
        pass                                                 # never blocking


def ttft_mesure():
    """Moving average of the TTFT in seconds, or None if nothing measured yet."""
    try:
        from db import get_setting
        v = get_setting('ttft_ms_ewma')
        return round(float(v) / 1000.0, 2) if v else None
    except Exception:                                        # noqa: BLE001
        return None


def flux_depuis_spendlogs(fenetre_s=60):
    """Throughput figures for the engines that publish NO /metrics (TabbyAPI).

    `vllm_health()` scrapes /metrics for the decode rate and the cumulative
    counters; on exllamav3 the route answers 404 and the dashboard showed
    nothing. LiteLLM's SpendLogs already holds, per COMPLETED request, its two
    token counts and its two timestamps: the figures below are therefore ratios
    of SUMS — exact in cumulative terms and insensitive to the portal's probe
    frequency, like `_compteurs_llamacpp`.

    Pitfall, the same one as the in-flight panel: a SpendLogs row lands at
    request END, so `debit` undercounts while a long generation is running (its
    tokens all land at once at the end). A fallback, not a gauge — and the
    average keeps the 300 s rule of `_compteurs_llamacpp` (below that, a "tok/s"
    is noise: 47 tokens / 192 s measured once = 0.2).

    Returns None if LiteLLM's database is unreachable.
    """
    conn = _spend_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*), '
                    'SUM(COALESCE(completion_tokens, 0)), '
                    'SUM(COALESCE(prompt_tokens, 0)), '
                    'COALESCE(SUM(EXTRACT(EPOCH FROM ("endTime" - "startTime"))), 0) '
                    'FROM "LiteLLM_SpendLogs" WHERE "endTime" IS NOT NULL')
        requetes, generes, entree, secondes = cur.fetchone()
        generes, entree = int(generes or 0), int(entree or 0)
        secondes = float(secondes or 0)
        cur.execute('SELECT COALESCE(SUM(COALESCE(completion_tokens, 0)), 0) '
                    'FROM "LiteLLM_SpendLogs" WHERE "endTime" >= %s',
                    (datetime.utcnow() - timedelta(seconds=fenetre_s),))
        recents = int(cur.fetchone()[0] or 0)
        return {
            'requetes': int(requetes or 0),
            'generes': generes,
            'entree': entree,
            'secondes_generation': secondes,
            'tps_moyen': round(generes / secondes, 1) if secondes >= 300 else None,
            'debit': round(recents / fenetre_s, 1),
        }
    except Exception:                                        # noqa: BLE001
        return None
