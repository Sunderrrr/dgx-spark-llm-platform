"""Notifications sortantes : courriel (HTML, anglais) et webhook Discord.

Extrait de app.py le 28/08. Ces fonctions vivaient sous la banniere « OCR »,
qui ne contient en realite aucun code OCR — c'est le genre de frontiere mal
placee qui rendait le monolithe difficile a decouper. Elles n'ont rien
d'OCR ni de Discord-DM (cf. discord_notify.py, qui envoie des messages PRIVES
aux utilisateurs ; ici c'est le webhook d'equipe et le mail admin).

Ne depend que de la configuration et de la bibliotheque standard.
"""
import html
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from config import (ADMIN_EMAIL, DISCORD_WH, SMTP_FROM, SMTP_HOST, SMTP_PASS,
                    SMTP_PORT, SMTP_USER)

# Nom d'application affiché par le client mail (gmail indique « DGX platform »).
# L'adresse d'expédition reste le compte SMTP authentifié (no-reply@cronos.website).
APP_NAME = "DGX platform"


def _sender():
    """Adresse « From » complète : `<APP_NAME> <<compte SMTP>>`."""
    addr = SMTP_USER
    m = re.search(r"<([^>]+)>", SMTP_FROM or "")
    if m:
        addr = m.group(1)
    return f"{APP_NAME} <{addr}>"


# ── Gabarit d'email (HTML + texte alternatif) ───────────────────────────────
# Un seul rendu pour toutes les notifications : bandeau brandé, tableau de
# détail, pied de page. Le contenu des notifications admin est en anglais
# (choix produit) ; `send_user_email` garde le contenu fourni par l'appelant.

def _esc(value):
    value = "" if value is None else str(value)
    return html.escape(value)


def _rows_html(rows):
    cells = []
    for label, value in rows:
        cells.append(
            "<tr>"
            f'<td style="padding:7px 18px 7px 0;color:#6b7280;font-size:13px;'
            f'font-weight:600;white-space:nowrap;vertical-align:top;">{_esc(label)}</td>'
            f'<td style="padding:7px 0;color:#111827;font-size:14px;vertical-align:top;">'
            f'{_esc(value)}</td>'
            "</tr>")
    return "".join(cells)


def _render_html(subject, heading, rows, body, footnote):
    rows_html = _rows_html(rows) if rows else ""
    body_html = (
        f'<p style="margin:16px 0 0;font-size:14px;line-height:1.6;color:#374151;">'
        f'{_esc(body).replace(chr(10), "<br>")}</p>') if body else ""
    footnote_html = (
        f'<p style="margin:18px 0 0;font-size:12px;color:#9ca3af;line-height:1.5;">'
        f'{_esc(footnote)}</p>') if footnote else ""
    return f"""<!doctype html>
<html lang="en"><body style="margin:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);">
<tr><td style="padding:22px 30px;background:#0f172a;">
 <span style="font-size:17px;font-weight:700;color:#ffffff;letter-spacing:.2px;">DGX&nbsp;<span style="color:#4ade80;">platform</span></span>
</td></tr>
<tr><td style="padding:26px 30px;">
 <h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a;">{_esc(heading)}</h1>
 <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;">{rows_html}</table>
 {body_html}
</td></tr>
<tr><td style="padding:16px 30px;background:#f9fafb;border-top:1px solid #eef0f2;color:#9ca3af;font-size:12px;">
 Cronos&nbsp;·&nbsp;DGX platform&nbsp;·&nbsp;Automated message&nbsp;·&nbsp;{datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')}
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _email_parts(subject, heading, rows, body, footnote):
    text = [heading, ""]
    if rows:
        text.extend(f"{label}: {value if value is not None else '—'}"
                    for label, value in rows)
    if body:
        text.append(body)
    if footnote:
        text.extend(["", footnote])
    text.extend(["", "— Cronos · DGX platform"])
    return "\n".join(text), _render_html(subject, heading, rows, body, footnote)


def _send(to_email, subject, heading, rows=None, body=None, footnote=None):
    """Envoie un email HTML + texte alternatif à `to_email`. Retourne True/False."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]) or not to_email:
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = _sender()
    msg['To'] = to_email
    text, html_body = _email_parts(subject, heading, rows, body, footnote)
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email] erreur : {e}")
        return False


# ── Discord (inchangé) ──────────────────────────────────────────────────────

def notify_discord(model_id, username, fullname, reason):
    if not DISCORD_WH:
        return
    payload = {"embeds": [{
        "title": "🤖 Nouvelle demande de modèle — DGX Spark",
        "color": 0x76B900,
        "fields": [
            {"name": "Utilisateur", "value": f"{fullname} (`{username}`)", "inline": True},
            {"name": "Modèle", "value": f"`{model_id}`", "inline": True},
            {"name": "Raison", "value": reason or "—"},
        ],
        "footer": {"text": "DGX Portal"},
        "timestamp": datetime.utcnow().isoformat()
    }]}
    try:
        requests.post(DISCORD_WH, json=payload, timeout=5)
    except Exception:
        pass


def notify_budget_discord(username, fullname, key_alias, current_budget, reason):
    if not DISCORD_WH:
        return
    payload = {"embeds": [{
        "title": "🔋 Demande de tokens supplémentaires — DGX Spark",
        "color": 0xF0A500,
        "fields": [
            {"name": "Utilisateur", "value": f"{fullname} (`{username}`)", "inline": True},
            {"name": "Clé", "value": f"`{key_alias}`", "inline": True},
            {"name": "Budget actuel", "value": f"{current_budget:,.0f} tokens" if current_budget is not None else "—", "inline": True},
            {"name": "Raison", "value": reason or "—"},
        ],
        "footer": {"text": "DGX Portal"},
        "timestamp": datetime.utcnow().isoformat()
    }]}
    try:
        requests.post(DISCORD_WH, json=payload, timeout=5)
    except Exception:
        pass


# ── Emails ──────────────────────────────────────────────────────────────────

def notify_email(model_id, username, fullname, reason):
    """Email admin : demande d'ajout d'un modèle."""
    return _send(
        ADMIN_EMAIL,
        f"[DGX platform] New model request — {model_id}",
        "New model request",
        rows=[("User", f"{fullname or username} ({username})"),
              ("Model", model_id),
              ("Reason", reason or "—")],
        body="A user is asking for this model to be added to the platform.",
        footnote="Open the Admin dashboard to review and approve the request.",
    )


def notify_budget_email(username, fullname, key_alias, current_budget, reason):
    """Email admin : demande d'augmentation de budget (tokens)."""
    budget_str = f"{current_budget:,.0f} tokens" if current_budget is not None else "—"
    return _send(
        ADMIN_EMAIL,
        f"[DGX platform] Token increase request — {username}",
        "Token increase request",
        rows=[("User", f"{fullname} ({username})"),
              ("Key", key_alias),
              ("Current budget", budget_str),
              ("Reason", reason or "—")],
        body="A user is asking for more tokens on their API key.",
        footnote="Open the Admin dashboard to review and approve the request.",
    )


def send_user_email(to_email, subject, body):
    """Email à UN utilisateur — contenu fourni par l'appelant (souvent en
    français, le portail étant FR-first) ; on applique le même gabarit HTML."""
    return _send(to_email, subject, subject, body=body)


def notify_maintenance_email(enabled, by_username, by_fullname):
    """Email admin : bascule du mode maintenance."""
    state = "enabled" if enabled else "disabled"
    heading = "Maintenance mode enabled" if enabled else "Maintenance mode disabled"
    return _send(
        ADMIN_EMAIL,
        f"[DGX platform] Maintenance {state}",
        heading,
        rows=[("By", f"{by_fullname or by_username or '—'} ({by_username})")],
        body=("The portal is now blocking requests from non-admin accounts."
              if enabled else "The portal is accessible to everyone again."),
    )


def notify_media_request_email(category, username, fullname):
    """Email admin : demande de lancement d'un modèle d'une catégorie média."""
    return _send(
        ADMIN_EMAIL,
        f"[DGX platform] {category} model requested",
        f"{category} model requested",
        rows=[("User", f"{fullname or username} ({username})"),
              ("Category", category)],
        body="No model of this category is currently loaded. Launch one to enable it.",
    )
