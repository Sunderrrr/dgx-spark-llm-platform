"""How long the API takes — measured, not felt.

Why a metric and not a feeling: the operator tunes this platform's speed
regularly (the /api/home probe parallelisation of 2026-10-07 dropped it from
832 ms to 55 ms) but nothing TOLD whether a regression had crept back. This
keeps a rolling window of request durations and reports the percentiles the
operator actually asks about: « how slow is it right now » (p50), « how slow
does it get » (p95), and the worst of the window.

Design notes:
- in-memory ring, no database write per request: a 1 s dashboard poll must
  not cost a row (same rule as the model counters);
- only routes under /api and /admin are measured — static assets and pages
  would drown the signal;
- the window is bounded (10 000 samples ≈ a busy day of API calls) and the
  report says its own span, so a quiet box never shows a confident average
  computed over four requests.
"""
import threading
import time

from flask import Blueprint, jsonify

from auth import login_required

FENETRE = 10_000
_bp = Blueprint("latence", __name__)
_verrou = threading.Lock()
_echantillons = []          # (timestamp, seconds, route)
_debut = time.time()


def enregistrer(route, duree_s):
    with _verrou:
        _echantillons.append((time.time(), duree_s, route))
        if len(_echantillons) > FENETRE:
            del _echantillons[:len(_echantillons) - FENETRE]


def latence_resume(fenetre_s=None):
    """Percentiles over the window (or the last `fenetre_s` seconds)."""
    now = time.time()
    with _verrou:
        pris = [e for e in _echantillons if fenetre_s is None or now - e[0] <= fenetre_s]
    if not pris:
        return {'n': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None,
                'plus_lent': None, 'fenetre_min': 0}
    tri = sorted(e[1] for e in pris)
    def _p(q):
        return round(tri[min(len(tri) - 1, int(len(tri) * q))] * 1000)
    # The slowest ROUTE in the window is what an operator wants to read: a
    # number without a name sends them looking, a name sends them fixing.
    lents = {}
    for _, d, r in pris:
        lents[r] = max(lents.get(r, 0), d)
    return {
        'n': len(pris),
        'p50_ms': _p(0.50),
        'p95_ms': _p(0.95),
        'max_ms': round(tri[-1] * 1000),
        'plus_lent': max(lents.items(), key=lambda kv: kv[1])[0] if lents else None,
        'fenetre_min': round((now - pris[0][0]) / 60),
    }


@_bp.route("/api/latence")
@login_required
def api_latence():
    # Login required: the response times are the platform's own telemetry, not
    # a public health check. Cheap anyway — one sort of a bounded list.
    return jsonify(latence_resume())
