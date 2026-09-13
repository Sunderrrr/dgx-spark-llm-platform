#!/usr/bin/env python3
"""Crée (ou réinitialise) le compte de démonstration `demo`, utilisé pour les
captures d'écran publiées dans le README.

Pourquoi un compte dédié : les captures d'écran montrent l'interface telle qu'un
utilisateur la voit — elles ne doivent exposer ni le compte personnel de
l'exploitant, ni celui d'un collègue. Ce compte est un compte ordinaire
(non administrateur, aucun groupe, quota par défaut), avec la mémoire activée et
l'interface en anglais.

Le script s'exécute DANS le conteneur du portail : il a besoin du code et de la
base du portail, et il crée l'enveloppe de budget LiteLLM par le même helper que
la route d'administration (`_sync_local_user_budget`) — jamais en écrivant
directement dans LiteLLM.

    docker cp scripts/create-demo-account.py dgx-portal:/tmp/
    docker exec -u 10001 -e DEMO_PW="$(cat /root/shots/demo-credentials)" \
        dgx-portal python3 /tmp/create-demo-account.py

Le mot de passe n'est jamais écrit en clair dans le dépôt : il vient de
l'environnement et se conserve hors dépôt (ex. /root/shots/demo-credentials).
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
    # Langue anglaise (les captures publiées le sont en anglais) et mémoire
    # activée ; `onboarded` évite la visite guidée au premier chargement.
    db.execute(
        "INSERT INTO user_prefs (username, avatar_id, theme_id, lang, onboarded, memory_enabled)"
        " VALUES (?,?,?,?,?,?)", (USER, 'avatar-01', 'neutral', 'en', 1, 1))
    db.commit()
    _sync_local_user_budget(USER, db.execute("SELECT * FROM local_users WHERE username=?",
                                             (USER,)).fetchone())
    print(f"  compte {USER} prêt (lang=en, thème neutre, mémoire activée, non admin)")
