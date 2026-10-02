"""Discord notifications: private messages to users who linked their account.

Extracted from app.py on 28/08, after db.py and config.py. Without those two,
the section was not extractable: it reads the `discord_links` table and the
persisted settings.

The bot sends a private message to each user who linked their account
(OAuth2 "identify") when an announcement goes out. Entirely optional: without
DISCORD_BOT_TOKEN, everything is inert.
"""
import sqlite3
import threading
import time

import requests

from config import DISCORD_API, DISCORD_BOT_TOKEN
# DB_PATH in addition to get_db: the broadcast runs in a THREAD, which has no
# Flask context and therefore opens its own connection (same reason as the
# image/video jobs — see the get_db docstring).
from db import DB_PATH, get_setting

# Distinct from the admin webhook above: here the *bot* sends a private message
# to each user who opted in by linking their Discord account. A DM needs a mutual
# guild with the bot and the user's DMs open, so per-user failures are expected
# and swallowed (best-effort).
def _discord_bot_headers():
    return {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

def _discord_send_dm(discord_id, content):
    """Open (or reuse) a DM channel with the user and post one message. Handles a
    single 429 retry. Returns True on success."""
    if not DISCORD_BOT_TOKEN:
        return False
    try:
        ch = requests.post(f"{DISCORD_API}/users/@me/channels",
                           headers=_discord_bot_headers(),
                           json={"recipient_id": str(discord_id)}, timeout=8)
        if not ch.ok:
            return False
        channel_id = ch.json().get('id')
        if not channel_id:
            return False
        body = {"content": content[:1900]}
        r = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                          headers=_discord_bot_headers(), json=body, timeout=8)
        if r.status_code == 429:
            try:
                time.sleep(min(float(r.json().get('retry_after', 1)), 5))
            except Exception:
                time.sleep(1)
            r = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                              headers=_discord_bot_headers(), json=body, timeout=8)
        return r.ok
    except Exception:
        return False

def discord_broadcast(content):
    """DM every linked user, in a background thread so the caller (a launch or an
    announcement) isn't blocked. Throttled to stay under Discord's rate limits."""
    if not DISCORD_BOT_TOKEN or not content:
        return
    if get_setting('discord_dm', '1') != '1':   # admin kill-switch
        return
    def _run():
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT discord_id FROM discord_links").fetchall()
            conn.close()
        except Exception:
            return
        for row in rows:
            _discord_send_dm(row['discord_id'], content)
            time.sleep(0.3)   # gentle: ~3 DMs/s, well under the limit
    threading.Thread(target=_run, daemon=True).start()

def _discord_announce(kind, a='', b=''):
    """Format an announcement for Discord (FR) and DM it to linked users. Mirrors
    the four announcement kinds produced by add_announcement()."""
    a = a or ''
    b = b or ''
    if kind == 'model_launch':
        txt = f"🤖 **Nouveau modèle actif : {a}**"
        if b:
            txt += f"\n_Il remplace {b}._"
    elif kind == 'model_add':
        txt = f"✨ **Nouveau modèle disponible : {a}**"
    elif kind == 'maintenance':
        txt = ("🛠️ **Maintenance en cours** — le service peut être interrompu un moment."
               if a == 'on' else
               "✅ **Fin de maintenance** — le service est rétabli.")
    elif kind == 'site':
        txt = f"📣 **{a}**"
        if b:
            txt += f"\n{b}"
    else:
        return
    txt += "\n\n— Cronos"
    discord_broadcast(txt)
