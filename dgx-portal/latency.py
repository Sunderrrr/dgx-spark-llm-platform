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

WINDOW = 10_000
_bp = Blueprint("latency", __name__)
_lock = threading.Lock()
_samples = []          # (timestamp, seconds, route)
_start = time.time()


def record(route, duration_s):
    with _lock:
        _samples.append((time.time(), duration_s, route))
        if len(_samples) > WINDOW:
            del _samples[:len(_samples) - WINDOW]


def latency_summary(window_s=None):
    """Percentiles over the window (or the last `fenetre_s` seconds)."""
    now = time.time()
    with _lock:
        taken = [e for e in _samples if window_s is None or now - e[0] <= window_s]
    if not taken:
        return {'n': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None,
                'slowest': None, 'window_min': 0}
    sort_key = sorted(e[1] for e in taken)
    def _p(q):
        return round(sort_key[min(len(sort_key) - 1, int(len(sort_key) * q))] * 1000)
    # The slowest ROUTE in the window is what an operator wants to read: a
    # number without a name sends them looking, a name sends them fixing.
    slow = {}
    for _, d, r in taken:
        slow[r] = max(slow.get(r, 0), d)
    return {
        'n': len(taken),
        'p50_ms': _p(0.50),
        'p95_ms': _p(0.95),
        'max_ms': round(sort_key[-1] * 1000),
        'slowest': max(slow.items(), key=lambda kv: kv[1])[0] if slow else None,
        'window_min': round((now - taken[0][0]) / 60),
    }


@_bp.route("/api/latency")
@login_required
def api_latency():
    # Login required: the response times are the platform's own telemetry, not
    # a public health check. Cheap anyway — one sort of a bounded list.
    return jsonify(latency_summary())
