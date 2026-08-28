"""Statistiques de consommation : agregats, classements, utilisateurs actifs.

Extrait de app.py le 28/08. La banniere « Statistiques » du monolithe couvrait
en realite deux sujets : ces calculs, et toute l'administration des modeles et
sidecars (34 routes). Seuls les calculs sont ici — aucune route, donc aucun
endpoint renomme et aucun url_for a requalifier.

Les donnees viennent de deux sources melangees a dessein : la base LiteLLM
(Postgres) pour les requetes TERMINEES, et le registre in-flight (SQLite) pour
celles en cours — LiteLLM n'ecrit sa ligne qu'a la fin, donc sans ce registre un
utilisateur en pleine generation resterait invisible.
"""
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


def _compte_existe(nom):
    """Ce nom correspond-il a un compte connu de la plateforme ?

    Sert uniquement a VALIDER un nom devine depuis l'alias d'une cle API : sans
    cette verification on afficherait n'importe quel morceau d'alias comme s'il
    s'agissait d'un utilisateur. user_prefs, et non local_users : les comptes
    LDAP/SSO n'ont pas de ligne locale, seul user_prefs les voit tous.
    """
    if not nom:
        return False
    try:
        return get_db().execute(
            "SELECT 1 FROM user_prefs WHERE username=? LIMIT 1", (nom,)).fetchone() is not None
    except Exception:
        return False


# 180 s et non 120 : le registre in-flight ne couvre QUE les routes du portail
# (Playground/Support). Un client API passe par Traefik -> LiteLLM sans jamais
# traverser le portail : sa seule trace est SpendLogs, ecrite en FIN de requete.
# Les requetes agentiques mesurees durent 100 a 124 s, donc sous ~150 s un tel
# client disparaissait entre deux appels alors qu'il tournait sans discontinuer.
# Pas plus de 180 s non plus : au-dela, le panneau garde des noms partis depuis
# longtemps. C'est le garde-fou sur l'activite du moteur qui borne vraiment —
# moteur au repos, panneau vide, quelle que soit la fenetre.
def _active_users(window_s=180):
    """Users who queried the model recently, from two sources merged:
      - LiteLLM SpendLogs over the last `window_s` s (attributed by API key → user)
        — recent COMPLETED requests;
      - the live in-flight registry (in-app Playground/Support requests still
        streaming) — SpendLogs only writes at request end, so this shows the
        current user in real time. Such users are marked `live`.
    Feeds the admin "who's using the model" panel on the home page.
    """
    # Le panneau doit refleter l'activite REELLE. Sans ce garde-fou il gardait des
    # noms affiches pendant toute la fenetre alors que plus rien ne tournait :
    # l'admin voyait « 0 / 8 sessions » et pourtant deux utilisateurs listes. Le
    # moteur est la seule autorite sur « est-ce que quelque chose tourne ».
    inflight = _inflight_snapshot()
    en_cours = 0
    try:
        h = vllm_health() or {}
        en_cours = int(h.get('running') or 0) + int(h.get('waiting') or 0)
    except Exception:
        # Moteur injoignable : on ne VIDE PAS le panneau sur une simple panne de
        # sonde, sinon une erreur de metriques ferait croire que personne n'utilise
        # le modele. On retombe sur la fenetre SpendLogs seule.
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
            # Filtre sur endTime, PAS sur startTime. LiteLLM n'ecrit la ligne qu'a la
            # FIN de la requete : au moment ou elle devient visible, son startTime est
            # deja vieux de toute la duree de la generation. Mesure du 23/08 sur un
            # client agentique (mpigeon via une cle API) : requetes de 100 a 124 s
            # enchainees sans interruption, donc systematiquement hors d'une fenetre
            # de 120 s calee sur startTime — l'utilisateur etait invisible du panneau
            # « qui utilise le modele » alors qu'il saturait le GPU en continu.
            # Deux bornes, et c'est VOULU. Seul startTime est indexe (pas endTime) :
            # filtrer sur le seul COALESCE(endTime, startTime) forcait un balayage
            # complet — 38 000 lignes et 5,7 ms par appel, qui empirent a chaque
            # requete enregistree. La borne large sur startTime laisse Postgres
            # utiliser son index, la borne fine sur endTime garde la justesse pour
            # une requete longue. Mesure du 23/08 : 5,741 ms -> 0,145 ms.
            # 1 h de marge : au-dela, une requete unique aussi longue n'existe pas.
            large = since - timedelta(seconds=3600)
            cur.execute('SELECT api_key, COUNT(*), '
                        'SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)), '
                        'MAX(COALESCE("user", \'\')), '
                        'MAX(COALESCE(metadata->>\'user_api_key_alias\', \'\')) '
                        'FROM "LiteLLM_SpendLogs" '
                        'WHERE "startTime" >= %s AND COALESCE("endTime", "startTime") >= %s '
                        'GROUP BY api_key', (large, since))
            for api_key, cnt, toks, col_user, alias in cur.fetchall():
                if api_key in _NON_USER_KEYS:
                    continue
                # Trois sources d'attribution, de la plus fiable a la plus faible.
                # Avant, une cle absente de la table de correspondance etait
                # SILENCIEUSEMENT ignoree : son proprietaire n'apparaissait jamais,
                # sans que rien ne le signale. Or une cle creee hors du portail (ou
                # avant l'ajout de metadata.user) n'a pas cette correspondance.
                u = umap.get(api_key) or (col_user or '').strip()
                if not u and alias:
                    # Alias de la forme « mpigeon-1783112817 » ou « laptop-mboitel » :
                    # on ne devine RIEN, on ne retient que s'il correspond a un compte
                    # connu — sinon on prefere afficher la cle que d'inventer un nom.
                    for morceau in re.split(r'[-_]', alias):
                        if morceau and _compte_existe(morceau):
                            u = morceau
                            break
                if not u:
                    u = f"cle {str(api_key)[:8]}…"
                a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0, 'live': False})
                a['requests'] += int(cnt or 0)
                a['tokens'] += int(toks or 0)
                if en_cours > 0:
                    # Le moteur traite quelque chose et cet utilisateur vient d'emettre :
                    # c'est lui (ou l'un d'eux). Le registre in-flight ne voit que le
                    # portail, donc sans ca un client API n'etait JAMAIS marque « live ».
                    a['live'] = True
        except Exception:
            pass
        finally:
            conn.close()
    # Merge live in-flight in-app requests (real time).
    for u, n in inflight.items():
        a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0, 'live': False})
        a['live'] = True
        if a['requests'] == 0:
            a['requests'] = n
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

def _series_for(usernames):
    """username -> stable color class (alphabetical order, 8 slots + 'other')."""
    out = {}
    for i, u in enumerate(sorted(usernames)):
        out[u] = f"s{i+1}" if i < 8 else "other"
    return out

def _spark_points(spark, w=88, h=24):
    """Points of an SVG polyline (normalized on its own max)."""
    n = len(spark)
    if n < 2:
        return ''
    mx = max(spark) or 1
    return ' '.join(
        f"{(j/(n-1)*w):.1f},{(h - 1 - (v/mx)*(h-2)):.1f}" for j, v in enumerate(spark))

# Arithmétique de mois pour les périodes « 12 derniers mois » et « depuis le
# début » : timedelta ne connaît pas les mois (durées inégales), et on évite
# d'ajouter dateutil pour si peu.
_MIDNIGHT = {'hour': 0, 'minute': 0, 'second': 0, 'microsecond': 0}

def relativedelta_months(n, day=None):
    """Décalage de n mois, applicable à un datetime via `dt + relativedelta_months(n)`."""
    class _Shift:
        def __radd__(self, dt):
            y, m = dt.year, dt.month + n
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            return dt.replace(year=y, month=m, day=day if day else min(dt.day, 28))
    return _Shift()

def _month_buckets(start, end):
    """Liste des 1ers du mois de `start` à `end` inclus (clés des sparklines)."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1; y += 1
    return out

def ranking_full(period='day', me=None):
    """Enriched ranking: real tokens consumed (prompt + generated), delta vs
    the previous period, prompt/generated split, and a trend sparkline, per
    user.
    """
    conn = _spend_conn()
    empty = {'period': period, 'rows': [], 'active_count': 0}
    if not conn:
        return empty
    UTC = ZoneInfo('UTC')
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
            # Buckets MENSUELS : sur un an, 365 points feraient une sparkline
            # illisible (et 30x plus de lignes à agréger côté SQL).
            if period == 'year':
                cur_start = now_local.replace(**_MIDNIGHT) + relativedelta_months(-11, day=1)
                prev_start = cur_start + relativedelta_months(-12)
            else:
                # Depuis le début : on part du tout premier log (à défaut, ce mois-ci).
                c0 = conn.cursor()
                c0.execute('SELECT MIN("startTime") FROM "LiteLLM_SpendLogs"')
                first = (c0.fetchone() or [None])[0]
                start_date = first.date().replace(day=1) if first else today.replace(day=1)
                cur_start = now_local.replace(**_MIDNIGHT).replace(
                    year=start_date.year, month=start_date.month, day=1)
                prev_start = cur_start  # aucune période antérieure → pas de delta
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
        cur = conn.cursor()
        if bucket_kind == 'hour':
            bexpr = "EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::int"
        elif bucket_kind == 'month':
            bexpr = "date_trunc('month', ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::date"
        else:
            bexpr = "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s)::date"
        # Current period: per bucket + key (real tokens + prompt/generated split)
        cur.execute(
            f'SELECT {bexpr} AS b, api_key, SUM(prompt_tokens), SUM(completion_tokens) '
            'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s GROUP BY b, api_key',
            (LOCAL_TZ, cur_start_utc))
        agg = {}
        for b, api_key, prompt, comp in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'inconnu')
            a = agg.setdefault(u, {'tokens': 0, 'prompt': 0, 'completion': 0, 'spark': {}})
            tok = (prompt or 0) + (comp or 0)
            a['tokens'] += tok; a['prompt'] += prompt or 0; a['completion'] += comp or 0
            if tok:
                a['spark'][b] = a['spark'].get(b, 0) + tok
        # Previous period: total per key (for the delta) — real tokens.
        # « Depuis le début » n'a rien avant lui : on saute la requête, le delta
        # restera absent (None) plutôt que d'afficher un +∞ trompeur.
        cur.execute('SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
                    'FROM "LiteLLM_SpendLogs" '
                    'WHERE "startTime" >= %s AND "startTime" < %s GROUP BY api_key',
                    (prev_start_utc, cur_start_utc)) if period != 'all' else None
        prev = {}
        for api_key, toks in (cur.fetchall() if period != 'all' else []):
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'inconnu')
            prev[u] = prev.get(u, 0) + (toks or 0)
        items = sorted([(u, a) for u, a in agg.items() if a['tokens'] > 0],
                       key=lambda x: x[1]['tokens'], reverse=True)
        series = _series_for([u for u, _ in items])
        top = items[0][1]['tokens'] if items else 0
        rows = []
        for i, (u, a) in enumerate(items):
            pv = prev.get(u, 0)
            delta = ((a['tokens'] - pv) / pv * 100) if pv > 0 else None
            spark = [a['spark'].get(b, 0) for b in buckets]
            rows.append({
                'rank': i + 1, 'username': u, 'series': series[u], 'is_me': u == me,
                'tokens': a['tokens'], 'prompt': int(a['prompt']), 'completion': int(a['completion']),
                'delta': delta, 'bar_pct': (a['tokens'] / top * 100) if top else 0,
                'spark_pts': _spark_points(spark),
            })
        return {'period': period, 'rows': rows, 'active_count': len(rows)}
    except Exception:
        return empty
    finally:
        conn.close()

def user_hourly(username):
    """24 hourly points (real tokens consumed = prompt + generated) for today
    for the user, + total, hourly peak and number of active keys in the
    day. We show real tokens, not the weighted cost (input×0.1) which
    underestimates consumption by ~10× on prompt-heavy loads.
    """
    conn = _spend_conn()
    if not conn:
        return None
    empty = {'has_data': False, 'points': [{'hour': h, 'tokens': 0} for h in range(24)],
             'total': 0, 'peak_hour': 0, 'peak_val': 0, 'active_keys': 0}
    try:
        umap = _key_user_map(conn)
        my_keys = {tok for tok, u in umap.items() if u == username}
        if not my_keys:
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
        return {'has_data': total > 0,
                'points': [{'hour': h, 'tokens': round(by_hour[h])} for h in range(24)],
                'total': round(total), 'peak_hour': peak_hour,
                'peak_val': round(by_hour[peak_hour]), 'active_keys': len(active)}
    except Exception:
        return empty
    finally:
        conn.close()


# ── Usage par sidecar, pour l'administration ────────────────────────────────
# Rapatriees de la banniere « Apercu » le 28/08, ou elles n'avaient rien a
# faire : ce sont des agregats de consommation, comme le reste de ce module.

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
