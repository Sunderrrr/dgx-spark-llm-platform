"""Leaderboard (page /ranking): who consumes what, how, and when.

Moved out of stats.py on 2026-10-14 when it grew (« sur la partie classement,
fais tout »): the ranking itself (5 periods, 5 metrics, a model filter, by
account or by group), the activity heatmap, the records and streaks, one
account's detail, and the reference cost. Everything reads LiteLLM's
SpendLogs (real prompt/completion tokens per request); stats.py keeps the
connection and the key → account mapping, read through `stats.` so tests
patch them in one place.

Rules carried over from the original ranking (tests/test_ranking.py):
  - a row that is nobody's account (`unknown`, test-key leftovers) gets no
    rank, stays in the period TOTAL, and has no delta;
  - an unreadable accounts database re-ranks nobody (missing data is not
    proof of absence);
  - « depuis le début » has no previous period, so no delta (`has_prev`).
Privacy (`user_prefs.ranking_anonymous`) is applied by the ROUTE, which knows
who is looking: the figures here are the same for everyone.
"""
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import stats
from config import LOCAL_TZ
from db import get_db

PERIOD_LABELS = {'day': "Aujourd'hui", 'week': '7 derniers jours', 'month': '30 derniers jours',
                 'year': '12 derniers mois', 'all': 'Depuis le début'}
PREV_LABELS = {'day': 'hier', 'week': 'la semaine précédente', 'month': 'les 30 jours précédents',
               'year': 'les 12 mois précédents', 'all': ''}
PERIODS = tuple(PERIOD_LABELS)
# What a ranking can be based on. The first three are token counts; `requests`
# counts successful requests and `per_request` is their average size — the
# two readings that tell « many small questions » from « few huge documents »
# (measured 2026-09-14: one account sent 32 % of the tokens and generated
# 0,46 % of the output).
RANKING_METRICS = ('total', 'completion', 'prompt', 'requests', 'per_request')
GROUPINGS = ('account', 'group')
NO_GROUP = 'Sans groupe'

_SUCCESS = "COUNT(*) FILTER (WHERE status = 'success')"


def _metric_value(prompt, completion, metric, requests=0):
    """The figure we RANK on."""
    p, c, n = prompt or 0, completion or 0, requests or 0
    if metric == 'completion':
        return c
    if metric == 'prompt':
        return p
    if metric == 'requests':
        return n
    if metric == 'per_request':
        return (p + c) / n if n else 0
    return p + c


# Month arithmetic for « 12 derniers mois » and « depuis le début »: timedelta
# knows no months, and dateutil is not worth adding for so little.
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
    """The 1sts of the month from `start` to `end` included (sparkline keys)."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _utc(dt):
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _window(period, conn):
    """(current start, previous start, buckets, bucket kind) of a period, local time."""
    now_local = datetime.now(ZoneInfo(LOCAL_TZ))
    today = now_local.date()
    if period == 'week':
        return (now_local - timedelta(days=7), now_local - timedelta(days=14),
                [today - timedelta(days=i) for i in range(6, -1, -1)], 'day')
    if period == 'month':
        return (now_local - timedelta(days=30), now_local - timedelta(days=60),
                [today - timedelta(days=i) for i in range(29, -1, -1)], 'day')
    if period in ('year', 'all'):
        # MONTHLY buckets: 365 points would make an unreadable sparkline.
        if period == 'year':
            cur_start = now_local.replace(**_MIDNIGHT) + relativedelta_months(-11, day=1)
            prev_start = cur_start + relativedelta_months(-12)
        else:
            c0 = conn.cursor()
            c0.execute('SELECT MIN("startTime") FROM "LiteLLM_SpendLogs"')
            first = (c0.fetchone() or [None])[0]
            start_date = first.date().replace(day=1) if first else today.replace(day=1)
            cur_start = now_local.replace(**_MIDNIGHT).replace(
                year=start_date.year, month=start_date.month, day=1)
            prev_start = cur_start                   # no previous period → no delta
        return cur_start, prev_start, _month_buckets(cur_start.date(), today), 'month'
    cur_start = now_local.replace(**_MIDNIGHT)
    return cur_start, cur_start - timedelta(days=1), list(range(24)), 'hour'


_BUCKET_SQL = {
    'hour': "EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::int",
    'month': "date_trunc('month', ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::date",
    'day': "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s)::date",
}


def _user_groups():
    """account → set of groups (local group + directory groups), or {} when
    the accounts database is unreadable."""
    out = {}
    try:
        db = get_db()
        for r in db.execute("SELECT username, group_name FROM local_users WHERE group_name IS NOT NULL "
                            "AND group_name != ''"):
            out.setdefault(r['username'], set()).add(r['group_name'])
        for r in db.execute("SELECT username, group_name FROM user_directory_groups"):
            out.setdefault(r['username'], set()).add(r['group_name'])
    except Exception:                                            # noqa: BLE001
        return {}
    return out


def prices():
    """Reference price, per million tokens, set in Admin → Config (« Classement »).

    « Ce que ça aurait coûté »: the consumption valued at a commercial API's
    list price — a figure to make the platform's value concrete, never a bill.
    Defaults: a mainstream hosted model's public price on 2026-10-14."""
    from instance_config import setting

    def num(name, default):
        try:
            return max(0.0, float(str(setting(name) or default).replace(',', '.')))
        except ValueError:
            return default
    return {'input': num('ranking_price_input', 2.5), 'output': num('ranking_price_output', 10.0),
            'currency': (setting('ranking_price_currency') or 'USD').strip()[:8] or 'USD'}


def _cost(prompt, completion, p):
    return round((prompt or 0) / 1e6 * p['input'] + (completion or 0) / 1e6 * p['output'], 2)


def ranking_full(period='day', me=None, metric='total', model=None, by='account'):
    """The ranking of a period: rows (rank, value, share, delta, trend, models,
    reference cost…), the period total and average, and the models seen
    (for the filter). `model` restricts every figure to one model;
    `by='group'` ranks groups (an account counts in each of its groups)."""
    metric = metric if metric in RANKING_METRICS else 'total'
    by = by if by in GROUPINGS else 'account'
    price = prices()
    conn = stats._spend_conn()
    empty = {'period': period, 'metric': metric, 'rows': [], 'active_count': 0,
             'total': 0, 'avg': 0, 'has_prev': period != 'all', 'models': [], 'model': model,
             'by': by, 'cost_total': 0, 'prices': price}
    if not conn:
        return empty
    try:
        cur_start, prev_start, buckets, bucket_kind = _window(period, conn)
        cur_start_utc, prev_start_utc = _utc(cur_start), _utc(prev_start)
        umap = stats._key_user_map(conn)
        known = stats._known_accounts()
        model_sql = ' AND model_group = %s' if model else ''
        cur = conn.cursor()
        # Current period, per bucket, key AND model: the counters are kept
        # apart, which is what lets the ranking read the input, the output,
        # the requests or their average size.
        cur.execute(
            f'SELECT {_BUCKET_SQL[bucket_kind]} AS b, api_key, model_group, SUM(prompt_tokens), '
            f'SUM(completion_tokens), {_SUCCESS} FROM "LiteLLM_SpendLogs" '
            f'WHERE "startTime" >= %s{model_sql} GROUP BY b, api_key, model_group',
            (LOCAL_TZ, cur_start_utc, *([model] if model else [])))
        agg = {}
        for b, api_key, mdl, prompt, comp, n in cur.fetchall():
            if api_key in stats._NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'unknown')
            p, c, n = prompt or 0, comp or 0, n or 0
            a = agg.setdefault(u, {'prompt': 0, 'completion': 0, 'requests': 0, 'spark': {}, 'models': {}})
            a['prompt'] += p
            a['completion'] += c
            a['requests'] += n
            s = a['spark'].setdefault(b, [0, 0, 0])
            s[0] += p
            s[1] += c
            s[2] += n
            if mdl and (p or c):
                a['models'][mdl] = a['models'].get(mdl, 0) + p + c
        # Previous period, same metric and model: a delta compares what is
        # comparable. « Depuis le début » has nothing before it.
        prev = {}
        if period != 'all':
            cur.execute('SELECT api_key, SUM(COALESCE(prompt_tokens,0)), SUM(COALESCE(completion_tokens,0)), '
                        f'{_SUCCESS} FROM "LiteLLM_SpendLogs" '
                        f'WHERE "startTime" >= %s AND "startTime" < %s{model_sql} GROUP BY api_key',
                        (prev_start_utc, cur_start_utc, *([model] if model else [])))
            for api_key, p, c, n in cur.fetchall():
                if api_key in stats._NON_USER_KEYS:
                    continue
                u = umap.get(api_key, 'unknown')
                q = prev.setdefault(u, [0, 0, 0])
                q[0] += p or 0
                q[1] += c or 0
                q[2] += n or 0
        # Models seen over the period, for the filter (never filtered itself).
        cur.execute(f'SELECT model_group, {_SUCCESS}, SUM(COALESCE(prompt_tokens,0) + '
                    'COALESCE(completion_tokens,0)) FROM "LiteLLM_SpendLogs" '
                    'WHERE "startTime" >= %s AND model_group IS NOT NULL AND model_group != \'\' '
                    'GROUP BY model_group', (cur_start_utc,))
        models = sorted(({'model': m, 'requests': int(n or 0), 'tokens': int(t or 0)}
                         for m, n, t in cur.fetchall() if (n or 0) > 0),
                        key=lambda x: x['tokens'], reverse=True)

        def is_residue(u):
            """A row that is nobody's account — see stats._known_accounts."""
            return u == 'unknown' or (known is not None and u not in known)

        if by == 'group':
            agg, prev, members = _by_group(agg, prev, is_residue)
        else:
            members = {}

        for a in agg.values():
            a['value'] = _metric_value(a['prompt'], a['completion'], metric, a['requests'])
        total_tokens = {'prompt': sum(a['prompt'] for a in agg.values()),
                        'completion': sum(a['completion'] for a in agg.values()),
                        'requests': sum(a['requests'] for a in agg.values())}

        def line(u, a, rank):
            pv = prev.get(u)
            pv_value = _metric_value(pv[0], pv[1], metric, pv[2]) if pv else 0
            residue = by == 'account' and is_residue(u)
            tokens = a['prompt'] + a['completion']
            top = sorted(a['models'].items(), key=lambda x: x[1], reverse=True)[:3]
            return {
                'rank': rank, 'username': u, 'is_me': (me in members.get(u, ())) if by == 'group' else u == me,
                'is_group': by == 'group', 'members': len(members.get(u, ())) if by == 'group' else None,
                'is_unattributed': residue,
                'value': round(a['value'], 1) if metric == 'per_request' else int(a['value']),
                'tokens': int(tokens), 'prompt': int(a['prompt']), 'completion': int(a['completion']),
                'requests': int(a['requests']),
                'share_pct': (a['value'] / total * 100) if total and metric != 'per_request' else None,
                # No delta on an unattributed row: a leftover has no history.
                'delta': None if residue or not pv_value else (a['value'] - pv_value) / pv_value * 100,
                'trend': [_metric_value(*_spark(a, b)[:2], metric, _spark(a, b)[2]) for b in buckets],
                'models': [{'model': m, 'share_pct': v / tokens * 100 if tokens else 0} for m, v in top],
                'cost': _cost(a['prompt'], a['completion'], price),
            }

        actives = [(u, a) for u, a in agg.items() if a['value'] > 0 and not (by == 'account' and is_residue(u))]
        others = [(u, a) for u, a in agg.items() if a['value'] > 0 and by == 'account' and is_residue(u)]
        actives.sort(key=lambda x: x[1]['value'], reverse=True)
        others.sort(key=lambda x: x[1]['value'], reverse=True)
        if metric == 'per_request':
            # An average does not add up: the platform's figure is the
            # average over ALL its requests, not a sum of averages.
            total = _metric_value(total_tokens['prompt'], total_tokens['completion'], metric,
                                  total_tokens['requests'])
            avg = total
        else:
            total = sum(a['value'] for _, a in actives + others)
            total_active = sum(a['value'] for _, a in actives)
            avg = total_active / len(actives) if actives else 0
        rows = [line(u, a, i + 1) for i, (u, a) in enumerate(actives)]
        rows += [line(u, a, None) for u, a in others]
        return {'period': period, 'metric': metric, 'rows': rows, 'by': by, 'model': model,
                'active_count': len(actives), 'total': round(total, 1), 'avg': round(avg, 1),
                'has_prev': period != 'all', 'models': models, 'prices': price,
                'cost_total': _cost(total_tokens['prompt'], total_tokens['completion'], price)}
    except Exception:                                            # noqa: BLE001
        return empty
    finally:
        conn.close()


def _spark(a, b):
    return a['spark'].get(b, (0, 0, 0))


def _by_group(agg, prev, is_residue):
    """Accounts folded into their groups; an account in several groups counts
    in each. Leftovers belong to no group."""
    groups = _user_groups()
    out, out_prev, members = {}, {}, {}
    for u, a in agg.items():
        if is_residue(u):
            continue
        for g in groups.get(u) or {NO_GROUP}:
            t = out.setdefault(g, {'prompt': 0, 'completion': 0, 'requests': 0, 'spark': {}, 'models': {}})
            members.setdefault(g, set()).add(u)
            for k in ('prompt', 'completion', 'requests'):
                t[k] += a[k]
            for b, v in a['spark'].items():
                s = t['spark'].setdefault(b, [0, 0, 0])
                for i in range(3):
                    s[i] += v[i]
            for m, v in a['models'].items():
                t['models'][m] = t['models'].get(m, 0) + v
    for u, q in prev.items():
        if is_residue(u):
            continue
        for g in groups.get(u) or {NO_GROUP}:
            t = out_prev.setdefault(g, [0, 0, 0])
            for i in range(3):
                t[i] += q[i]
    return out, out_prev, members


# ── Activity: when the platform works ───────────────────────────────────────
# A cell is always ONE real moment. The first version folded every Monday of
# the period into one « Monday 4 h » cell: over 12 months that was 52 moments
# averaged into a slot that never happened (operator, 2026-10-14). So:
#   - week  → the 7 real dates × 24 hours (one cell = one hour of one day);
#   - month, year → one cell per day (laid out as a calendar by the UI).

ACTIVITY_DAYS = {'week': 7, 'month': 30, 'year': 365}


def activity(period='month', username=None):
    """Successful requests and tokens over the period, per real hour (week) or
    per real day (month, year). `username` restricts it to one account."""
    period = period if period in ACTIVITY_DAYS else 'month'
    days = ACTIVITY_DAYS[period]
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    hourly = period == 'week'
    out = {'period': period, 'mode': 'hours' if hourly else 'days',
           'max_requests': 0, 'total_requests': 0}
    if hourly:
        grid = {d: [{'requests': 0, 'tokens': 0} for _ in range(24)] for d in dates}
    else:
        grid = {d: {'requests': 0, 'tokens': 0} for d in dates}
    conn = stats._spend_conn()
    try:
        if conn:
            umap = stats._key_user_map(conn)
            since = _utc(datetime.combine(dates[0], datetime.min.time(), ZoneInfo(LOCAL_TZ)))
            local = "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s)"
            cur = conn.cursor()
            cur.execute(f'SELECT ({local})::date, EXTRACT(HOUR FROM {local})::int, api_key, COUNT(*), '
                        'SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
                        'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s AND status = \'success\' '
                        'GROUP BY 1, 2, api_key', (LOCAL_TZ, LOCAL_TZ, since))
            for d, hour, api_key, n, tokens in cur.fetchall():
                if api_key in stats._NON_USER_KEYS or d not in grid:
                    continue
                if username is not None and umap.get(api_key) != username:
                    continue
                c = grid[d][int(hour)] if hourly else grid[d]
                c['requests'] += int(n or 0)
                c['tokens'] += int(tokens or 0)
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        if conn:
            conn.close()
    if hourly:
        out['rows'] = [{'day': d.isoformat(), 'cells': grid[d]} for d in dates]
        flat = [c['requests'] for d in dates for c in grid[d]]
    else:
        out['days'] = [{'day': d.isoformat(), **grid[d]} for d in dates]
        flat = [grid[d]['requests'] for d in dates]
    out['max_requests'] = max(flat) if flat else 0
    out['total_requests'] = sum(flat)
    return out


# ── Records and streaks ─────────────────────────────────────────────────────

_RECORDS_CACHE = {'at': 0.0, 'data': None}
_RECORDS_TTL = 120.0


def _daily_history():
    """{account: {local day: [tokens, requests]}} and the first use of each
    model {model: (first time, account)}, over all time (successes only).
    Cached: it scans the whole history and records move slowly."""
    now = time.time()
    if _RECORDS_CACHE['data'] is not None and now - _RECORDS_CACHE['at'] < _RECORDS_TTL:
        return _RECORDS_CACHE['data']
    conn = stats._spend_conn()
    if not conn:
        return {}, {}
    try:
        umap = stats._key_user_map(conn)
        known = stats._known_accounts()
        cur = conn.cursor()
        cur.execute('SELECT ((\"startTime\" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date AS d, api_key, '
                    'model_group, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)), COUNT(*), '
                    'MIN("startTime") FROM "LiteLLM_SpendLogs" WHERE status = \'success\' '
                    'GROUP BY d, api_key, model_group', (LOCAL_TZ,))
        days, firsts = {}, {}
        for d, api_key, mdl, tokens, n, first in cur.fetchall():
            if api_key in stats._NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'unknown')
            if u == 'unknown' or (known is not None and u not in known):
                continue
            v = days.setdefault(u, {}).setdefault(d, [0, 0])
            v[0] += int(tokens or 0)
            v[1] += int(n or 0)
            if mdl and (mdl not in firsts or first < firsts[mdl][0]):
                firsts[mdl] = (first, u)
        data = (days, firsts)
        _RECORDS_CACHE.update(at=now, data=data)
        return data
    except Exception:                                            # noqa: BLE001
        return {}, {}
    finally:
        conn.close()


def _streaks(day_set, today):
    """(longest run of consecutive active days, current run — alive if it
    reaches today or yesterday)."""
    longest = run = 0
    prev = None
    for d in sorted(day_set):
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d
    current = run if prev is not None and (today - prev).days <= 1 else 0
    return longest, current


def records(me=None):
    """Platform records (biggest days, longest and current streaks, first to
    use each model) and the viewer's own."""
    days, firsts = _daily_history()
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    best_days, longest, current = [], [], []
    mine = None
    for u, per_day in days.items():
        d, (tokens, n) = max(per_day.items(), key=lambda x: x[1][0])
        best_days.append({'username': u, 'day': d.isoformat(), 'tokens': tokens, 'requests': n})
        lg, cr = _streaks(per_day.keys(), today)
        longest.append({'username': u, 'days': lg})
        if cr:
            current.append({'username': u, 'days': cr})
        if u == me:
            mine = {'best_day': {'day': d.isoformat(), 'tokens': tokens},
                    'longest_streak': lg, 'current_streak': cr, 'active_days': len(per_day),
                    'first_on': sorted(m for m, (_t, who) in firsts.items() if who == me)}
    top = lambda rows, key: sorted(rows, key=lambda r: r[key], reverse=True)[:3]   # noqa: E731
    return {
        'best_days': top(best_days, 'tokens'),
        'longest_streaks': top(longest, 'days'),
        'current_streaks': top(current, 'days'),
        'firsts': [{'model': m, 'username': who, 'day': t.date().isoformat()}
                   for m, (t, who) in sorted(firsts.items(), key=lambda x: x[1][0], reverse=True)[:8]],
        'me': mine,
    }


# ── One account in detail (the account itself, or an admin) ────────────────

def user_detail(username, period='month'):
    """Per model and per day, over the period, for ONE account."""
    days = ACTIVITY_DAYS.get(period, 30)
    price = prices()
    out = {'username': username, 'period': period if period in ACTIVITY_DAYS else 'month',
           'models': [], 'daily': [], 'prompt': 0, 'completion': 0, 'requests': 0, 'cost': 0}
    conn = stats._spend_conn()
    if not conn:
        return out
    try:
        umap = stats._key_user_map(conn)
        keys = [k for k, u in umap.items() if u == username]
        if not keys:
            return out
        since = _utc(datetime.now(ZoneInfo(LOCAL_TZ)) - timedelta(days=days))
        cur = conn.cursor()
        cur.execute('SELECT ((\"startTime\" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date AS d, model_group, '
                    'SUM(COALESCE(prompt_tokens,0)), SUM(COALESCE(completion_tokens,0)), COUNT(*) '
                    'FROM "LiteLLM_SpendLogs" WHERE api_key = ANY(%s) AND "startTime" >= %s '
                    'AND status = \'success\' GROUP BY d, model_group', (LOCAL_TZ, keys, since))
        per_model, per_day = {}, {}
        for d, mdl, p, c, n in cur.fetchall():
            m = per_model.setdefault(mdl or '?', [0, 0, 0])
            day = per_day.setdefault(d, [0, 0, 0])
            for acc in (m, day):
                acc[0] += int(p or 0)
                acc[1] += int(c or 0)
                acc[2] += int(n or 0)
        out['models'] = sorted(({'model': k, 'prompt': v[0], 'completion': v[1], 'requests': v[2]}
                                for k, v in per_model.items()),
                               key=lambda x: x['prompt'] + x['completion'], reverse=True)
        start = datetime.now(ZoneInfo(LOCAL_TZ)).date() - timedelta(days=days - 1)
        out['daily'] = [{'day': (start + timedelta(days=i)).isoformat(),
                         'tokens': sum(per_day.get(start + timedelta(days=i), (0, 0))[:2]),
                         'requests': per_day.get(start + timedelta(days=i), (0, 0, 0))[2]}
                        for i in range(days)] if days <= 31 else []
        out['prompt'] = sum(v[0] for v in per_model.values())
        out['completion'] = sum(v[1] for v in per_model.values())
        out['requests'] = sum(v[2] for v in per_model.values())
        out['cost'] = _cost(out['prompt'], out['completion'], price)
        out['currency'] = price['currency']
        return out
    except Exception:                                            # noqa: BLE001
        return out
    finally:
        conn.close()


# ── Quotas (admin column) ───────────────────────────────────────────────────

_QUOTA_CACHE = {'at': 0.0, 'data': {}}


def quotas():
    """{account: percent of its LiteLLM budget used} — one /user/list call,
    cached 30 s (the admin page refreshes; LiteLLM is not free to ask)."""
    now = time.time()
    if now - _QUOTA_CACHE['at'] < 30:
        return _QUOTA_CACHE['data']
    out = {}
    for u in stats.admin_get_user_consumption():
        if u.get('unlimited') or not u.get('max_budget'):
            out[u['username']] = None
        else:
            out[u['username']] = round(u['spend'] / u['max_budget'] * 100, 1)
    _QUOTA_CACHE.update(at=now, data=out)
    return out
