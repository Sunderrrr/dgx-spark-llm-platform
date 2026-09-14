"""Activite EN VOL des requetes, pour le panneau admin du portail.

**Pourquoi ce fichier existe.** LiteLLM n'ecrit sa ligne SpendLogs qu'a la FIN
d'une requete, et n'expose aucun endpoint de requetes en vol (mesure du
2026-09-14 : 518 routes publiees, aucune ne liste l'actif ; `/metrics` repond 404,
le callback Prometheus n'etant pas active). Le portail ne pouvait donc repondre
qu'a « qui a fini recemment », jamais a « qui genere la, maintenant » — alors que
le moteur, lui, voyait bien 2 a 4 sessions occupees. Mesures du meme jour :
**44 minutes sans la moindre ligne SpendLogs** pendant que deux sessions
travaillaient, une requete agentique durant **3 min 17 s** pour 22 281 tokens
d'entree.

Ce callback note l'ouverture au DEPART et la retire a la fin. Trois regles :

- une erreur ici ne doit **jamais** faire echouer une requete : tout est
  encapsule, l'incident part sur la sortie d'erreur et la requete continue ;
- l'ecriture se fait **hors de la boucle d'evenements** : sqlite est bloquant, et
  bloquer la boucle du proxy pour un confort d'affichage serait absurde ;
- le fichier est partage avec le portail, qui le monte en **lecture seule** : un
  seul ecrivain (ici), et rien de ce cote ne peut alterer le portail.

**Pourquoi un SQLite et pas la base LiteLLM** : l'image du proxy ne contient
aucun pilote PostgreSQL — ni `psycopg2`, ni `psycopg`, ni `asyncpg` (verifie) —
et `prisma` n'est pas utilisable directement. Le portail, lui, lit deja
SpendLogs en Postgres ; il lira ce fichier en plus.

Ce qui est stocke est volontairement BRUT (alias de cle, user_id, modele) : c'est
le portail qui resout un alias en nom de compte, avec la meme regle que pour
SpendLogs. Une seule logique de resolution, donc un seul endroit ou se tromper.
"""
import asyncio
import os
import sqlite3
import sys
import time

CHEMIN = os.environ.get('CRONOS_INFLIGHT_DB', '/run/cronos/inflight.db')
# Au-dela, la ligne est consideree orpheline : un client tue en plein vol ne
# declenche ni succes ni echec, donc personne ne viendrait la retirer.
PEREMPTION_S = float(os.environ.get('CRONOS_INFLIGHT_TTL', '7200'))

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:                                    # hors conteneur LiteLLM
    class CustomLogger:                              # (tests) : hooks inertes
        pass


def _ouvre():
    dossier = os.path.dirname(CHEMIN)
    if dossier:
        os.makedirs(dossier, exist_ok=True)
    conn = sqlite3.connect(CHEMIN, timeout=2)
    conn.execute('CREATE TABLE IF NOT EXISTS en_vol ('
                 'cle TEXT PRIMARY KEY, alias TEXT, user_id TEXT, '
                 'modele TEXT, debut REAL NOT NULL)')
    return conn


def _identite(kwargs):
    """(cle, alias, user_id, modele) tels que LiteLLM les donne pour une requete.

    La cle est l'identifiant d'appel de LiteLLM : c'est elle qui permet de retirer
    exactement la ligne ouverte, sans dependre du modele ni de l'alias.
    """
    params = (kwargs or {}).get('litellm_params') or {}
    meta = params.get('metadata') or {}
    cle = (kwargs.get('litellm_call_id') or meta.get('litellm_call_id')
           or meta.get('user_api_key') or '')
    return (str(cle),
            str(meta.get('user_api_key_alias') or ''),
            str(meta.get('user_api_key_user_id') or meta.get('user_id') or ''),
            str(kwargs.get('model') or meta.get('model') or ''))


def _enregistre(kwargs):
    """Note l'ouverture d'une requete. Retourne 1 si une ligne a ete ecrite."""
    cle, alias, user_id, modele = _identite(kwargs)
    if not cle:
        return 0
    conn = _ouvre()
    try:
        conn.execute('DELETE FROM en_vol WHERE debut < ?', (time.time() - PEREMPTION_S,))
        conn.execute('INSERT OR REPLACE INTO en_vol (cle, alias, user_id, modele, debut) '
                     'VALUES (?,?,?,?,?)', (cle, alias, user_id, modele, time.time()))
        conn.commit()
        return 1
    finally:
        conn.close()


def _retire(kwargs):
    """Retire la ligne d'une requete terminee, en succes comme en echec."""
    cle, _, _, _ = _identite(kwargs)
    if not cle:
        return 0
    conn = _ouvre()
    try:
        cur = conn.execute('DELETE FROM en_vol WHERE cle = ?', (cle,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


class ActiviteEnVol(CustomLogger):
    """Hooks LiteLLM : ouverture au depart, retrait a la fin (succes ou echec)."""

    async def async_log_pre_api_call(self, model, messages, kwargs):
        await self._hors_boucle(_enregistre, kwargs)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await self._hors_boucle(_retire, kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await self._hors_boucle(_retire, kwargs)

    @staticmethod
    async def _hors_boucle(fn, kwargs):
        try:
            await asyncio.to_thread(fn, kwargs)
        except Exception as e:                        # noqa: BLE001 — jamais fatal
            print('cronos_inflight: %s: %s' % (type(e).__name__, e), file=sys.stderr)


en_vol = ActiviteEnVol()
