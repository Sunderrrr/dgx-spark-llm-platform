"""Acces a la base SQLite du portail.

Extrait de app.py le 28/08. C'est le NOYAU PARTAGE : sans lui, aucune autre
section du monolithe n'etait extractible, parce que presque toutes appellent
get_db() (103 appels) et qu'un module importe par app.py ne peut pas
reimporter app.py — cycle a l'import.

Ce module ne depend que de flask.g et de sqlite3 : il n'importe rien du
portail, donc tout le monde peut l'importer sans risque de cycle.
"""
import sqlite3

from flask import g

DB_PATH = '/app/data/portal.db'


def get_db():
    """Connexion SQLite liee au contexte d'application Flask.

    Une seule connexion par requete, refermee par close_db. Les traitements en
    FIL (generation d'image, video, jobs) n'ont pas de contexte Flask et
    ouvrent donc leur propre connexion avec sqlite3.connect(DB_PATH) — c'est
    voulu, pas un oubli.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """Enregistre par app.py via app.teardown_appcontext(close_db).

    Pas de decorateur ici : le decorateur exigerait l'objet `app`, donc un
    import de app.py, donc exactement le cycle que ce module existe pour eviter.
    """
    db = g.pop('db', None)
    if db:
        db.close()


# ── Reglages persistes (table `settings`) ────────────────────────────────────
# De simples enveloppes SQL sur get_db : leur place est dans le noyau, pas dans
# le monolithe, parce que plusieurs sections extraites en ont besoin.

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    db.commit()


def maintenance_active():
    return get_setting('maintenance_mode', '0') == '1'
