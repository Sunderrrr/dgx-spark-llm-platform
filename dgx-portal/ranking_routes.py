"""Routes of the leaderboard (page /ranking) — the figures come from ranking.py.

Who may see what:
  - everyone signed in: the ranking, the activity heatmap (theirs or the
    platform's), the records, their own detail;
  - an admin: any account's detail, the budget used per account, the CSV.
Privacy: an account that chose to appear anonymously (`user_prefs.
ranking_anonymous`, Réglages) is shown to the OTHER users as « anonymous
account »: no name, no avatar, no detail. It keeps its own position, and the
admin — who sees consumption per account elsewhere anyway — sees the name.
Group figures are not masked: a group is not a person… except a group of
one, which is.
"""
import csv
import io

from flask import Blueprint, Response, abort, jsonify, request, session

import ranking
from auth import login_required
from db import get_db

bp = Blueprint('ranking', __name__)


def _anonymous_accounts():
    try:
        return {r['username'] for r in get_db().execute(
            "SELECT username FROM user_prefs WHERE ranking_anonymous = 1")}
    except Exception:                                            # noqa: BLE001
        return set()


def _is_admin():
    return bool(session.get('is_admin'))


def _mask(name, anonymous, me):
    """The name another user may see: hidden when the account asked for it."""
    return None if (name in anonymous and name != me and not _is_admin()) else name


@bp.route('/api/ranking')
@login_required
def api_ranking():
    period = request.args.get('period', 'day')
    period = period if period in ranking.PERIODS else 'day'
    # Validated like the period: an unknown metric falls back to the total
    # rather than answering an error to a page that opens.
    metric = request.args.get('metric', 'total')
    metric = metric if metric in ranking.RANKING_METRICS else 'total'
    by = request.args.get('by', 'account')
    model = (request.args.get('model') or '').strip()[:120] or None
    me = session['username']
    data = ranking.ranking_full(period, me=me, metric=metric, model=model, by=by)
    anonymous = _anonymous_accounts()
    # ONE read for every avatar of the page (not one per row).
    avatars = {name: av for name, av in get_db().execute(
        "SELECT username, avatar_id FROM user_prefs").fetchall()}
    admin = _is_admin()
    quotas = ranking.quotas() if admin and data['by'] == 'account' else {}
    for n, row in enumerate(data['rows']):
        name = row['username']
        if data['by'] == 'group':
            row['avatar_id'] = None
            # A group of one IS its member.
            if row['members'] == 1 and not admin and not row['is_me']:
                members = [u for u, gs in ranking._user_groups().items() if name in gs]
                if members and members[0] in anonymous:
                    row['username'] = None
                    row['is_anonymous'] = True
            continue
        shown = _mask(name, anonymous, me)
        row['is_anonymous'] = name in anonymous
        row['avatar_id'] = avatars.get(name) if shown else None
        if shown is None:
            row['username'] = None
            row['anon_id'] = f'anon-{n}'
        if admin:
            row['quota_pct'] = quotas.get(name)
    return jsonify({**data, 'is_admin': admin, 'me_anonymous': me in anonymous,
                    'period_label': ranking.PERIOD_LABELS[period], 'prev_label': ranking.PREV_LABELS[period]})


@bp.route('/api/ranking/activity')
@login_required
def api_ranking_activity():
    """Day-of-week × hour heatmap, for the viewer (`scope=me`) or the platform."""
    period = request.args.get('period', 'month')
    me = request.args.get('scope', 'all') == 'me'
    return jsonify(ranking.activity(period, session['username'] if me else None))


@bp.route('/api/ranking/records')
@login_required
def api_ranking_records():
    me = session['username']
    data = ranking.records(me)
    anonymous = _anonymous_accounts()
    for key in ('best_days', 'longest_streaks', 'current_streaks', 'firsts'):
        for r in data[key]:
            r['is_me'] = r['username'] == me
            r['username'] = _mask(r['username'], anonymous, me)
    return jsonify(data)


@bp.route('/api/ranking/user/<username>')
@login_required
def api_ranking_user(username):
    """One account's detail: the account itself, or an admin."""
    if username != session['username'] and not _is_admin():
        abort(404)
    return jsonify(ranking.user_detail(username, request.args.get('period', 'month')))


@bp.route('/api/ranking/privacy', methods=['POST'])
@login_required
def api_ranking_privacy():
    """Réglages: appear anonymously in the leaderboard (1) or not (0)."""
    value = 1 if request.form.get('anonymous') in ('1', 'true', 'on') else 0
    db = get_db()
    db.execute("INSERT INTO user_prefs (username, ranking_anonymous) VALUES (?, ?) "
               "ON CONFLICT(username) DO UPDATE SET ranking_anonymous = excluded.ranking_anonymous",
               (session['username'], value))
    db.commit()
    return jsonify({'ok': True, 'anonymous': bool(value)})


@bp.route('/api/ranking/export.csv')
@login_required
def api_ranking_export():
    """The ranking as a CSV file — admin only (it carries every name)."""
    if not _is_admin():
        abort(404)
    period = request.args.get('period', 'month')
    period = period if period in ranking.PERIODS else 'month'
    metric = request.args.get('metric', 'total')
    data = ranking.ranking_full(period, metric=metric,
                                model=(request.args.get('model') or '').strip()[:120] or None,
                                by=request.args.get('by', 'account'))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['rank', 'name', 'value', 'tokens', 'prompt', 'completion', 'requests',
                'share_pct', 'delta_pct', f"cost_{data['prices']['currency']}"])
    for r in data['rows']:
        w.writerow([r['rank'] or '', r['username'], r['value'], r['tokens'], r['prompt'], r['completion'],
                    r['requests'], '' if r['share_pct'] is None else round(r['share_pct'], 2),
                    '' if r['delta'] is None else round(r['delta'], 1), r['cost']])
    name = f"ranking-{data['by']}-{period}-{data['metric']}.csv"
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})
