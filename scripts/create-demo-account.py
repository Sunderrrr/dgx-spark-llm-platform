#!/usr/bin/env python3
"""Create (or reset) the `demo` demonstration account, used for the
screenshots published in the README.

Why a dedicated account: screenshots show the UI as a user sees it — they must
expose neither the operator's personal account nor a colleague's. This account
is an ordinary one (non-admin, no group, default quota), with memory enabled and
the UI in English.

The script runs INSIDE the portal container: it needs the portal's code and
database, and it creates the LiteLLM budget envelope through the same helper as
the admin route (`_sync_local_user_budget`) — never by writing into LiteLLM
directly.

    docker cp scripts/create-demo-account.py dgx-portal:/tmp/
    docker exec -u 10001 -e DEMO_PW="$(cat /root/shots/demo-credentials)" \
        dgx-portal python3 /tmp/create-demo-account.py

The password is never written in clear in the repo: it comes from the
environment and is kept outside the repo (e.g. /root/shots/demo-credentials).
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, '/app')
import app as portal                                     # noqa: E402
from werkzeug.security import generate_password_hash      # noqa: E402
from local_users import _sync_local_user_budget           # noqa: E402

USER = os.environ.get('DEMO_USER', 'demo')

if 'DEMO_PW' not in os.environ or len(os.environ['DEMO_PW']) < 12:
    sys.exit("DEMO_PW manquant (12 caractères minimum) — voir la docstring.")

with portal.app.app_context():
    db = portal.get_db()
    db.execute("DELETE FROM local_users WHERE username=?", (USER,))
    db.execute("DELETE FROM user_prefs WHERE username=?", (USER,))
    db.execute(
        "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name,"
        " max_budget, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)",
        (USER, generate_password_hash(os.environ['DEMO_PW']), 'Demo account', 0, None, None,
         datetime.now().isoformat()))
    # English language (the published screenshots are in English) and memory
    # enabled; `onboarded` skips the guided tour on first load.
    db.execute(
        "INSERT INTO user_prefs (username, avatar_id, theme_id, lang, onboarded, memory_enabled)"
        " VALUES (?,?,?,?,?,?)", (USER, 'avatar-01', 'neutral', 'en', 1, 1))
    db.commit()
    _sync_local_user_budget(USER, db.execute("SELECT * FROM local_users WHERE username=?",
                                             (USER,)).fetchone())
    print(f"  compte {USER} prêt (lang=en, thème neutre, mémoire activée, non admin)")
