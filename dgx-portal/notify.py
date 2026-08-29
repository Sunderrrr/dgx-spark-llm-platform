"""Notifications sortantes : courriel a l'administrateur et webhook Discord.

Extrait de app.py le 28/08. Ces fonctions vivaient sous la banniere « OCR »,
qui ne contient en realite aucun code OCR — c'est le genre de frontiere mal
placee qui rendait le monolithe difficile a decouper. Elles n'ont rien
d'OCR ni de Discord-DM (cf. discord_notify.py, qui envoie des messages PRIVES
aux utilisateurs ; ici c'est le webhook d'equipe et le mail admin).

Ne depend que de la configuration et de la bibliotheque standard.
"""
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

def notify_email(model_id, username, fullname, reason):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX platform] Demande modèle : {model_id}"
    msg['From'] = _sender()
    msg['To'] = ADMIN_EMAIL
    body = (
        f"Nouvelle demande de modèle\n\n"
        f"Utilisateur : {fullname} ({username})\n"
        f"Modèle      : {model_id}\n"
        f"Raison      : {reason or '—'}\n"
        f"Date        : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Dashboard admin : http://dgx.cronos.lan:5000/admin\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        print(f"[email] erreur : {e}")

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

def notify_budget_email(username, fullname, key_alias, current_budget, reason):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX platform] Demande de tokens : {username}"
    msg['From'] = _sender()
    msg['To'] = ADMIN_EMAIL
    budget_str = f"{current_budget:,.0f} tokens" if current_budget is not None else "—"
    body = (
        f"Nouvelle demande de tokens supplémentaires\n\n"
        f"Utilisateur   : {fullname} ({username})\n"
        f"Clé           : {key_alias}\n"
        f"Budget actuel : {budget_str}\n"
        f"Raison        : {reason or '—'}\n"
        f"Date          : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Dashboard admin : http://dgx.cronos.lan:5000/admin\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        print(f"[email] erreur : {e}")


# Courriel a UN utilisateur (et non a l'administrateur) : rapatrie de app.py
# le 28/08, il etait range avec le LDAP alors que c'est de l'envoi de mail.
def send_user_email(to_email, subject, body):
    """Sends a simple email to a user (notifications)."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]) or not to_email:
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = _sender()
    msg['To'] = to_email
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email user] erreur : {e}")
        return False


# Courriel à l'administrateur : bascule du mode maintenance (bouton Admin).
# Les destinataires sont les admins — on écrit à ADMIN_EMAIL (le compte opérateur).
def notify_maintenance_email(enabled, by_username, by_fullname):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX platform] Mode maintenance {'ACTIVÉ' if enabled else 'DÉSACTIVÉ'}"
    msg['From'] = _sender()
    msg['To'] = ADMIN_EMAIL
    body = (
        f"Le mode maintenance a été {'ACTIVÉ' if enabled else 'DÉSACTIVÉ'}.\n\n"
        f"Par    : {by_fullname or by_username or '—'} ({by_username})\n"
        f"Date   : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"{'Le portail bloque désormais les requêtes des non-admins.' if enabled else 'Le portail est de nouveau accessible à tous.'}\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
        return True
    except Exception as e:
        print(f"[email maintenance] erreur : {e}")
        return False


# Courriel à l'administrateur : un utilisateur demande qu'un modèle d'une
# catégorie (image/musique/vidéo/OCR/voix) soit lancé.
def notify_media_request_email(category, username, fullname):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX platform] Demande de modèle {category}"
    msg['From'] = _sender()
    msg['To'] = ADMIN_EMAIL
    body = (
        f"Un utilisateur demande le lancement d'un modèle « {category} ».\n\n"
        f"Utilisateur : {fullname} ({username})\n"
        f"Catégorie   : {category}\n"
        f"Date        : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Aucun modèle de cette catégorie n'est actuellement chargé.\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
        return True
    except Exception as e:
        print(f"[email média] erreur : {e}")
        return False
