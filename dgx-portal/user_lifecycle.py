"""Cycle de vie d'un compte : tout ce qu'il faut défaire quand il s'en va.

Trois choses se jouent ici, et AUCUNE n'était faite avant le 2026-09-13 :

1. L'ACCÈS. Supprimer un compte local ne faisait qu'un `DELETE` dans
   `local_users`. Ses clés API restaient valides — LiteLLM les valide
   lui-même, le portail n'est pas dans le chemin de l'API — et sa session
   navigateur survivait jusqu'à 12 h. Un partant gardait donc l'accès à
   l'API *et* au portail.
2. L'ENVELOPPE. L'objet utilisateur LiteLLM (plafond + dépense cumulée)
   survivait : un compte recréé sous le même nom héritait de la dépense du
   précédent, et pouvait donc démarrer au-dessus de son quota.
3. LES DONNÉES. Mémoire, conversations, partages, préférences, passkeys et
   jobs média restaient en base, rattachés à un nom qu'un collègue pouvait
   reprendre — la mémoire étant indexée par nom de compte, le nouveau venu
   héritait littéralement des souvenirs de l'ancien.

Le journal d'audit (`audit_log`) n'est PAS purgé : c'est la trace des actions
d'administration, et elle doit survivre au compte qu'elle concerne. Les
fichiers média générés (image/musique/voix) ne sont pas supprimés ici non
plus : la purge des orphelins du script de sauvegarde s'en charge, avec sa
grâce de 7 jours — supprimer les jobs suffit à les rendre orphelins.
"""
import sqlite3
import threading

from config import SMTP_HOST
from db import get_db, log_audit

# Tables rattachées à un compte, purgées à sa suppression. `local_users` n'y
# est pas : la route de suppression l'efface explicitement, par identifiant,
# ce qui est la seule façon sûre de viser la bonne ligne.
#
# `blocked_users` n'y est pas non plus, et c'est un CHOIX : purger efface des
# DONNÉES, bloquer décide d'un ACCÈS. Les confondre faisait qu'une purge —
# le geste normal au départ d'un salarié d'annuaire, qui n'a pas de ligne
# locale — DÉBLOQUAIT le compte en silence : il redevenait connectable, avec
# accès plateforme et GPU. La route promet pourtant d'effacer « sans toucher à
# son accès », et la doc dit que l'offboarding reste le blocage.
TABLES_PURGEES = (
    'announcement_state',
    'api_keys',
    'budget_grants',
    'budget_requests',
    'conversation_shares',
    'conversations',
    'discord_links',
    'image_jobs',
    'inflight_requests',
    'mcp_servers',
    'media_request_cooldown',
    'memory_aliases',
    'memory_edges',
    'memory_nodes',
    'model_requests',
    'music_jobs',
    'notifications',
    'ocr_jobs',
    'pending_actions',
    'pending_webauthn',
    'previews',
    'skills',
    'support_feedback',
    'support_thread',
    'user_prefs',
    'user_security',
    'user_sessions',
    'user_sources',
    'video_jobs',
    'voice_jobs',
    'webauthn_credentials',
)

_JOBS_MEDIA = ('image_jobs', 'music_jobs', 'ocr_jobs', 'video_jobs', 'voice_jobs')


def _compte(table, username, colonne='username'):
    try:
        return get_db().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {colonne}=?", (username,)).fetchone()[0]
    except sqlite3.Error:
        # Table absente d'une base antérieure : rien à compter, rien à purger.
        return 0


def compter_donnees(username):
    """Résumé de ce qui sera perdu — affiché à l'admin avant qu'il confirme."""
    cles = _compte('api_keys', username)
    return {
        'sessions': _compte('user_sessions', username),
        'keys': cles,
        'memory_facts': _compte('memory_nodes', username),
        'memory_links': _compte('memory_edges', username),
        'conversations': _compte('conversations', username),
        'shares': _compte('conversation_shares', username),
        'media_jobs': sum(_compte(t, username) for t in _JOBS_MEDIA),
        'preferences': _compte('user_prefs', username),
        'passkeys': _compte('webauthn_credentials', username),
    }


def _revoquer_cles(username, revoke_litellm_key, log):
    """Révoque les clés API du compte côté LiteLLM, puis oublie leurs lignes.

    La valeur en clair vient de `api_keys` (le portail la stocke pour pouvoir
    la révoquer : c'est le seul endroit d'où on peut la retrouver). Une clé
    que LiteLLM refuse de supprimer est signalée mais n'interrompt pas le
    reste : mieux vaut un compte sans portail qu'un compte avec une clé
    vivante, et le rapport le dit.
    """
    db = get_db()
    valeurs = [r['key_value'] for r in db.execute(
        "SELECT key_value FROM api_keys WHERE username=?", (username,)).fetchall()]
    reussies = echouees = 0
    for valeur in valeurs:
        try:
            if revoke_litellm_key(valeur):
                reussies += 1
            else:
                echouees += 1
        except Exception as exc:                                 # noqa: BLE001
            log(f'[user_lifecycle] révocation de clé impossible : {exc}')
            echouees += 1
    # Les lignes locales disparaissent dans tous les cas : une clé qu'on n'a
    # pas pu révoquer resterait de toute façon invisible et ingérable ensuite.
    db.execute("DELETE FROM api_keys WHERE username=?", (username,))
    db.commit()
    return reussies, echouees, len(valeurs)


def purger_donnees(username):
    """Efface toutes les données rattachées au compte. Renvoie le décompte."""
    db = get_db()
    total = 0
    for table in TABLES_PURGEES:
        try:
            total += db.execute(
                f"DELETE FROM {table} WHERE username=?", (username,)).rowcount
        except sqlite3.Error:
            continue
    # Tentatives de connexion : clés composites ('ip|user' et 'user:user').
    # Sans ça, un compte recréé sous le même nom naîtrait déjà verrouillé.
    try:
        total += db.execute(
            "DELETE FROM login_attempts WHERE key=? OR key LIKE ?",
            (f"user:{username}", f"%|{username}")).rowcount
    except sqlite3.Error:
        pass
    db.commit()
    return total


def deprovisionner_compte(username, par, purge=True, action='user.delete'):
    """Retire tout accès du compte, puis (purge=True) ses données.

    L'ORDRE compte : les clés d'abord — LiteLLM refuse de supprimer un
    utilisateur qui en porte encore selon les versions —, l'enveloppe
    ensuite, la base du portail en dernier. Si une étape échoue, on préfère
    laisser un compte sans clés qu'un compte sans ligne locale mais avec des
    clés vivantes.
    """
    from auth import _revoke_user_sessions                    # import tardif : auth importe déjà ce module
    from litellm_client import delete_litellm_user, revoke_litellm_key

    rapport = compter_donnees(username)
    cles_ok, cles_ko, cles_total = _revoquer_cles(username, revoke_litellm_key, print)
    rapport['keys_revoked'] = cles_ok
    rapport['keys_failed'] = cles_ko
    rapport['keys'] = cles_total

    sessions = _revoke_user_sessions(username)
    rapport['sessions'] = sessions

    rapport['litellm_user'] = bool(delete_litellm_user(username))
    rapport['lignes_purgees'] = purger_donnees(username) if purge else 0

    detail = (f"{username} — {cles_ok}/{cles_total} clé(s) révoquée(s)"
              + (f", {cles_ko} ÉCHEC(S)" if cles_ko else '')
              + f", {sessions} session(s), enveloppe LiteLLM "
              + ('supprimée' if rapport['litellm_user'] else 'NON supprimée')
              + (f", {rapport['lignes_purgees']} ligne(s) de données purgée(s)"
                 if purge else ', données CONSERVÉES'))
    log_audit(par, action, detail)
    return rapport


def _envoyer_notification_mot_de_passe(username, par_admin=None):
    """Le travail réel de la notification (adresse, puis email).

    Séparé de `prevenir_mot_de_passe_change` pour être testable sans fil : c'est
    ici que l'annuaire est interrogé, et c'est cette partie qui peut être lente.
    """
    try:
        from auth import ldap_lookup_email
        from notify import send_user_email
        destinataire = ldap_lookup_email(username)
        if not destinataire:
            # Compte local sans équivalent dans l'annuaire : aucune adresse
            # connue, et on n'en invente pas une.
            print(f'[user_lifecycle] pas d’adresse connue pour {username}, '
                  'notification non envoyée')
            return False
        auteur = ('Un administrateur a réinitialisé' if par_admin
                  else 'Ton mot de passe a été changé')
        return bool(send_user_email(
            destinataire,
            'Mot de passe modifié — DGX platform',
            f"{auteur} le mot de passe du compte « {username} » sur la plateforme DGX.\n\n"
            "Si tu n'es pas à l'origine de ce changement, contacte un administrateur "
            "immédiatement : toutes les autres sessions du compte ont été fermées, mais "
            "l'accès à l'API doit être vérifié."))
    except Exception as exc:                                     # noqa: BLE001
        print(f'[user_lifecycle] notification de mot de passe non envoyée : {exc}')
        return False


def prevenir_mot_de_passe_change(username, par_admin=None):
    """Prévient l'intéressé que son mot de passe vient de changer, HORS du chemin.

    C'est ce qui donne sa valeur au changement en autonomie : quelqu'un dont le
    compte est pris reste aveugle tant que personne ne lui dit que le mot de
    passe a bougé. Best-effort assumé — sans SMTP configuré (ou sans adresse
    connue pour un compte purement local), il ne se passe rien et le changement
    aboutit quand même : une notification ne doit jamais faire échouer l'action
    qu'elle rapporte.

    Elle ne doit pas la RETARDER non plus. Mesuré le 2026-09-16 : la réponse à
    `POST /api/account/password` mettait **3,2 s**, parce que l'adresse se
    cherche par un bind LDAP (`ldap_lookup_email`) et que l'annuaire ne répond
    pas — alors que le changement, lui, était déjà écrit en base. Le clic restait
    donc suspendu sur un travail qui ne le concerne pas. La notification part
    maintenant dans un fil démon : elle ne peut ni échouer bruyamment, ni faire
    attendre. Renvoie False seulement quand il n'y a rien à faire (pas de SMTP).
    """
    if not SMTP_HOST:
        return False
    threading.Thread(target=_envoyer_notification_mot_de_passe,
                     args=(username, par_admin),
                     name='notif-mot-de-passe', daemon=True).start()
    return True
