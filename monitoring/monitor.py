#!/usr/bin/env python3
"""Sonde de santé des services « cœur » de CrOS — alerte email (hôte).

On sonde le cœur toujours-up de la plateforme et on envoie un email à
ADMIN_EMAIL quand un service tombe puis se rétablit. De la même façon que la
bascule de maintenance, l'envoi est un no-op si le SMTP n'est pas configuré.

Ce qui est sondé (toujours censé être up) :
  - vllm-runner          → daemon systemd (port 8001, répond 200/401)
  - traefik              → conteneur (docker inspect)
  - litellm              → conteneur (docker inspect)
  - litellm-postgres     → conteneur (docker inspect)
  - dgx-portal           → conteneur (docker inspect)
  - dgx-portal-frontend  → conteneur (docker inspect)

Ce qui N'est PAS sondé : vLLM (:8000) et les sidecars média (OCR/voix/musique/
image/ComfyUI/ASR) — ils sont *on-demand* (démarrés/arrêtés à la demande) ; les
alerter produirait des faux positifs. Leur état reste visible dans /health.

État « sticky » : un fichier d'état mémorise les services actuellement down,
pour n'envoyer QU'UNE alerte par incident (et un email de rétablissement).
Aucune donnée sensible n'y est écrite.

Usage :
  python3 monitor.py              # sonde + envoi selon transition
  python3 monitor.py --init       # ne fait que mémoriser l'état courant (aucun email)
  python3 monitor.py --dry-run    # sonde + affiche, n'envoie rien
  python3 monitor.py --list       # liste les services et leur état
  python3 monitor.py --state PATH # fichier d'état personnalisé
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

# Services sondés : (clé, type, cible, attendu). « expect » = codes HTTP qui
# comptent comme « up » pour un probe HTTP.
SERVICES = [
    ("vllm-runner", "http", "http://127.0.0.1:8001/status", (200, 401)),
    ("traefik", "container", "traefik", None),
    ("litellm", "container", "litellm", None),
    ("litellm-postgres", "container", "litellm-postgres", None),
    ("dgx-portal", "container", "dgx-portal", None),
    ("dgx-portal-frontend", "container", "dgx-portal-frontend", None),
]


def _load_env(path=ENV_FILE):
    """Lit KEY=VALUE d'un fichier .env (ignore les commentaires)."""
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
        # 401/403 (auth requise) = le service répond → up.
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


def probe():
    """Retourne {clé: {"up": bool, "detail": str}}."""
    state = {}
    for key, kind, target, expect in SERVICES:
        if kind == "http":
            try:
                detail = target
            except Exception:
                detail = target
            up = _http_up(target, expect)
        else:
            detail = target
            up = _container_up(target)
        state[key] = {"up": up, "detail": detail}
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

    cur = probe()
    down = {k: v for k, v in cur.items() if not v["up"]}

    if args.list:
        for key, v in cur.items():
            print(f"{'UP' if v['up'] else 'DOWN'}  {key}")
        return 0

    if args.dry_run:
        print("DOWN services:", ", ".join(down) if down else "none (all up)")
        return 0

    state = _load_state(args.state)
    prev_down = set(state.get("down", []))

    # --init : mémorise l'état sans envoyer (évite un burst au déploiement).
    if args.init:
        _save_state(args.state, {"down": sorted(down)})
        print(f"init: {sorted(down) if down else 'all up'}")
        return 0

    newly_down = sorted(set(down) - prev_down)
    recovered = sorted(prev_down - set(down))
    if newly_down:
        _send(f"[DGX platform] Alert — services down",
              {k: down[k] for k in newly_down})
        print(f"alert sent for: {newly_down}")
    # Email de rétablissement uniquement quand l'incident est résolu.
    if not down and recovered:
        _send("[DGX platform] Services back up",
              {k: cur[k] for k in recovered})
        print(f"recovery sent for: {recovered}")

    _save_state(args.state, {"down": sorted(down)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
