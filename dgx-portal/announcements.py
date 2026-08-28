"""Annonces de la plateforme : enregistrement en base et diffusion Discord.

Extrait de app.py le 28/08, depuis le reliquat de la banniere « Helpers ».
Distinct de discord_notify.py, qui ne fait qu'ENVOYER : ici on decide QUOI
annoncer (changement de modele, maintenance, annonce de site) et on le persiste
pour que l'interface puisse le montrer aux utilisateurs qui ne l'ont pas vu.
"""
from datetime import datetime

from db import get_db
from discord_notify import _discord_announce

def add_announcement(kind, a='', b=''):
    """Publishes an announcement (a card shown when the site opens). kind ∈
    {'site', 'model_add', 'model_launch'}. `a`/`b` are free fields
    (e.g. model name / previous model) rendered client-side.
    """
    try:
        db = get_db()
        db.execute(
            "INSERT INTO announcements (kind, a, b, created_at) VALUES (?,?,?,?)",
            (kind, a or '', b or '', datetime.now().isoformat()))
        db.commit()
    except Exception:
        pass
    # Also DM the announcement to every user who linked their Discord account.
    try:
        _discord_announce(kind, a, b)
    except Exception:
        pass

def _announce_launch(new_name):
    """Announces the switch to a new active model. Publishes nothing if that model
    is already the last announced (relaunch / same model) → no duplicate. The
    "replaces X" comes from the last announcement, more reliable than get_running_models()
    at launch time (the old one is being killed, the new one not yet up).
    """
    last = get_db().execute(
        "SELECT a FROM announcements WHERE kind='model_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = last['a'] if last else ''
    if prev == new_name:
        return
    add_announcement('model_launch', new_name, prev)
