#!/usr/bin/env python3
"""Health probe for the « core » services of CrOS — email alert (host).

We probe the always-up core of the platform and send an email to ADMIN_EMAIL
when a service falls and then recovers. Like the maintenance failover, sending
is a no-op if SMTP is not configured.

What is probed (always supposed to be up):
  - vllm-runner          → systemd daemon (port 8001, answers 200/401)
  - traefik              → container (docker inspect)
  - litellm              → container (docker inspect)
  - litellm-postgres     → container (docker inspect)
  - dgx-portal           → container (docker inspect)
  - dgx-portal-frontend  → container (docker inspect)

What is NOT probed: vLLM (:8000) and the media sidecars (OCR/voice/music/
image/ComfyUI/ASR) — they are *on-demand* (started/stopped on request);
alerting on them would produce false positives. Their state stays visible in
/health.

« sticky » state: a state file records the services currently down, so that
only ONE alert per incident is sent (and a recovery email). No sensitive data
is written there.

Usage:
  python3 monitor.py              # probe + send on transition
  python3 monitor.py --init       # only memorize the current state (no email)
  python3 monitor.py --dry-run    # probe + display, send nothing
  python3 monitor.py --list       # list the services and their state
  python3 monitor.py --state PATH # custom state file
"""
import argparse
import html
import json
import os
import socket
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".env")
STATE = "/var/lib/cronos-monitor/state.json"
APP_NAME = "DGX platform"

# Probed services: (key, type, target, expected). « expect » = HTTP codes that
# count as « up » for an HTTP probe.
SERVICES = [
    ("vllm-runner", "http", "http://127.0.0.1:8001/status", (200, 401)),
    ("traefik", "container", "traefik", None),
    ("litellm", "container", "litellm", None),
    ("litellm-postgres", "container", "litellm-postgres", None),
    ("dgx-portal", "container", "dgx-portal", None),
    ("dgx-portal-frontend", "container", "dgx-portal-frontend", None),
]

# Nightly backup: a dump too old = incident (retention/maintenance).
BACKUP_DIR = "/var/backups/cronos"
BACKUP_MAX_AGE_H = 26  # cronos-backup tourne à 03:00 → < 26 h = toujours frais

# Data integrity: on 04/09/2026 the contents of portal.db were reset and nobody
# noticed for three days. We keep a WATERMARK of the data volume seen and alert
# if the counter drops below 40 % — a user deleting their own conversations can
# trigger a false positive (one email), a silent reset costs three days.
PORTAL_DB = "/var/lib/docker/volumes/ai-platform_portal_data/_data/portal.db"
# Kept for documentation: this is the REAL path of the database, but the unit
# cannot open it (0700 uid 10001 volume, monitor without CAP_DAC_OVERRIDE).
# Any read must go through COMPTEURS_CODE, below.
DATA_DROP_RATIO = 0.4   # alerte sous 40 % du filigrane
DATA_FLOOR = 20         # jamais d'alerte tant que le filigrane est < 20

# The counters are read INSIDE the portal container, not on this path: since
# the unit was hardened (empty CapabilityBoundingSet) the monitor no longer has
# CAP_DAC_OVERRIDE, and the volume belongs to uid 10001 in 0700. The probe then
# returned « db illisible » AND `up=True` — so no more alerts at all, which was
# precisely the failure it exists to catch (silent reset of 04/09/2026,
# unnoticed for three days). The container, on the other hand, owns the
# database.
COMPTEURS_CODE = (
    "import sqlite3;c=sqlite3.connect('file:/app/data/portal.db?mode=ro',uri=True);"
    "print(c.execute('SELECT COUNT(*) FROM conversations').fetchone()[0],"
    "sum(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]"
    " for t in ('image_jobs','music_jobs','video_jobs')))"
)


def _data_intact(watermark):
    """(up, detail, counters): conversations + media jobs vs watermark."""
    import subprocess
    try:
        r = subprocess.run(["docker", "exec", "dgx-portal", "python3", "-c", COMPTEURS_CODE],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "").strip()[:200] or f"docker exec rc={r.returncode}")
        conv, jobs = (int(x) for x in r.stdout.split())
    except Exception as exc:
        # Unreadable: it is the container probes' job to report it.
        return True, f"db illisible ({exc})", {"conversations": None, "jobs": None}
    wm_conv = max(int(watermark.get("conversations") or 0), conv)
    wm_jobs = max(int(watermark.get("jobs") or 0), jobs)
    pertes = []
    if wm_conv >= DATA_FLOOR and conv < wm_conv * DATA_DROP_RATIO:
        pertes.append(f"conversations {conv} << filigrane {wm_conv}")
    if wm_jobs >= DATA_FLOOR and jobs < wm_jobs * DATA_DROP_RATIO:
        pertes.append(f"jobs média {jobs} << filigrane {wm_jobs}")
    detail = ("; ".join(pertes) if pertes
              else f"conversations={conv}, jobs={jobs}")
    return (not pertes), detail, {"conversations": conv, "jobs": jobs}


def _backup_fresh():
    """(up, detail, info): the most recent portal dump must be < 26 h old.

    `info` (name, age, number of dumps) is copied into the sticky state so the
    PORTAL can display it: it is the only channel readable by the container,
    where `/var/backups/cronos` is 0700 root and the dumps 0600 — that is on
    purpose, they hold the whole database. The monitor itself runs as root.
    """
    import glob
    import time
    try:
        files = glob.glob(os.path.join(BACKUP_DIR, "portal-*.db"))
    except Exception:
        files = []
    if not files:
        return False, "no portal backup found", {"latest": None, "age_hours": None, "count": 0}
    latest = max(files, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(latest)) / 3600
    info = {"latest": os.path.basename(latest), "age_hours": round(age_h, 1),
            "count": len(files)}
    if age_h > BACKUP_MAX_AGE_H:
        return False, f"last backup {age_h:.0f}h old (> {BACKUP_MAX_AGE_H}h)", info
    return True, os.path.basename(latest), info


def _load_env(path=ENV_FILE):
    """Read KEY=VALUE from a .env file (comments ignored)."""
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _http_up(url, expect):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status in expect
    except urllib.error.HTTPError as exc:
        # 401/403 (auth required) = the service answers → up.
        return exc.code in expect
    except Exception:
        return False


def _container_up(name):
    import subprocess
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def probe(watermark=None):
    """Return {key: {"up": bool, "detail": str}}."""
    state = {}
    for key, kind, target, expect in SERVICES:
        # `detail` was `target` in BOTH branches of a try/except that wrapped
        # only one assignment: it protected nothing and ran at every probe
        # (every 5 min).
        detail = target
        up = _http_up(target, expect) if kind == "http" else _container_up(target)
        state[key] = {"up": up, "detail": detail}
    # Nightly backup: it must be fresh, otherwise it is an incident — we go
    # through the same sticky mechanism (1 alert per incident, recovery email on
    # return).
    bu, bdetail, binfo = _backup_fresh()
    state["backup"] = {"up": bu, "detail": bdetail, **binfo}
    # Data integrity: the watermark (stored in the state) feeds the massive
    # drop detection — same sticky mechanism as the services.
    du, ddetail, compteurs = _data_intact(watermark or {})
    state["donnees"] = {"up": du, "detail": ddetail, **compteurs}
    return state


def _render_html(down):
    rows = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee;">'
        f'{html.escape(k)}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#dc2626;">'
        f'<b>DOWN</b> ({html.escape(v.get("detail") or "")})</td></tr>'
        for k, v in down.items())
    return f"""<!doctype html><html lang="en"><body style="margin:0;background:#f6f7f9;">
<div style="max-width:600px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<div style="background:#0f172a;padding:20px 24px;">
<span style="color:#ffffff;font-size:16px;font-weight:700;">{APP_NAME}</span>
<span style="color:#9ca3af;font-size:14px;margin-left:8px;">Service alert</span>
</div>
<div style="padding:24px;">
<h2 style="margin:0 0 8px;font-size:18px;color:#111827;">One or more services are down</h2>
<p style="margin:0 0 16px;font-size:14px;color:#374151;">The monitoring probe could not reach the services below.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;">{rows}</table>
</div>
<div style="background:#f3f4f6;padding:14px 24px;font-size:12px;color:#9ca3af;">
CrOS · {APP_NAME} — automated monitoring message
</div>
</div></body></html>"""


def _send(subject, down):
    env = _load_env()
    host, user, passwd = env.get("SMTP_HOST"), env.get("SMTP_USER"), env.get("SMTP_PASSWORD")
    to = env.get("ADMIN_EMAIL")
    if not (host and user and passwd and to):
        print("SMTP not configured — no email sent.")
        return False
    port = int(env.get("SMTP_PORT", "587"))
    sender = env.get("SMTP_FROM") or f'{APP_NAME} <{user}>'
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    text = (f"{APP_NAME} — services down:\n\n"
            + "\n".join(f"- {k}: {v.get('detail')}" for k, v in down.items())
            + "\n\nCheck the Admin dashboard.")
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(_render_html(down), "html"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, passwd)
            smtp.sendmail(user, [to], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"sendmail error: {exc}")
        return False


def _load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(state, fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    ap.add_argument("--state", default=STATE)
    args = ap.parse_args()

    if args.test_email:
        ok = _send("[DGX platform] Monitor SMTP test",
                   {"monitor": {"up": False, "detail": "smtp configuration works"}})
        print("test email sent" if ok else "test email FAILED")
        return 0 if ok else 1

    state = _load_state(args.state)
    prev_down = set(state.get("down", []))
    wm = state.get("data_watermark") or {}

    cur = probe(wm)
    down = {k: v for k, v in cur.items() if not v["up"]}
    # Watermark: never decreasing (max between seen and stored) — a failed read
    # (container stopped) therefore does not reset the reference.
    d = cur.get("donnees", {})
    wm = {
        "conversations": max(int(wm.get("conversations") or 0),
                             int(d.get("conversations") or 0)),
        "jobs": max(int(wm.get("jobs") or 0), int(d.get("jobs") or 0)),
    }

    if args.list:
        for key, v in cur.items():
            print(f"{'UP' if v['up'] else 'DOWN'}  {key}")
        return 0

    if args.dry_run:
        print("DOWN services:", ", ".join(down) if down else "none (all up)")
        return 0

    # --init: memorize the state without sending (avoids a burst at deploy time).
    if args.init:
        _save_state(args.state, {"down": sorted(down), "data_watermark": wm,
                                 "backup": cur.get("backup") or {}})
        print(f"init: {sorted(down) if down else 'all up'}")
        return 0

    newly_down = sorted(set(down) - prev_down)
    recovered = sorted(prev_down - set(down))
    if newly_down:
        _send(f"[DGX platform] Alert — services down",
              {k: down[k] for k in newly_down})
        print(f"alert sent for: {newly_down}")
    # Recovery email only when the incident is resolved.
    if not down and recovered:
        _send("[DGX platform] Services back up",
              {k: cur[k] for k in recovered})
        print(f"recovery sent for: {recovered}")

    # The state written is rebuilt here, not copied from `cur`: we keep only the
    # sticky (`down`) and the watermark in it. `backup` is added explicitly
    # because it is the ONLY channel through which the portal can show the
    # freshness of the dump — `/var/backups/cronos` is 0700 root, the container
    # sees nothing there.
    _save_state(args.state, {"down": sorted(down), "data_watermark": wm,
                             "backup": cur.get("backup") or {}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
