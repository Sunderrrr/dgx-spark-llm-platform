"""Outils de recherche web exposes au modele depuis le playground.

Extrait de app.py le 28/08. Enchaine sur db.py : sa seule dependance restante
etait LITELLM_URL, qui vient de l'environnement, et get_db, qui vit desormais
dans le noyau partage — donc aucun import de app.py, aucun cycle possible.

La mecanique de bas niveau (SearXNG, crawl4ai, garde-fous SSRF) est dans
websearch.py ; ce module-ci est la couche qui la presente au modele sous forme
d'outils et qui orchestre le tour d'appel.
"""
import json
import os
import re
import time

import requests

import websearch
from db import get_db

LITELLM_URL = os.environ.get('LITELLM_URL', 'http://litellm:4000')

# Le playground n'avait aucune boucle d'outils : il relayait le flux du modèle
# tel quel. On en ajoute une, en deux temps :
#   1. une phase OUTILS sans flux — le modèle peut chercher, lire, rechercher
#      encore, jusqu'à MAX_TOURS_OUTILS ;
#   2. la réponse finale, en flux, exactement comme avant.
# Découper ainsi évite d'avoir à reconstituer des appels d'outils fragmentés en
# deltas, et la réponse visible arrive toujours token par token.
MAX_TOURS_OUTILS = 3
# Plafond de temps de la phase outils. Même légitime, une recherche ne doit
# jamais monopoliser l'attente : au-delà, on répond avec ce qui a été trouvé.
# Une lecture de page peut à elle seule prendre une minute et demie.
DELAI_MAX_OUTILS = 60
# Ce que la phase outils relit pour décider s'il faut chercher. Volontairement
# COURT : décider « faut-il une recherche ? » ne demande pas de relire un fichier
# de 65 Ko. Lui passer toute la conversation ajoutait un préchargement complet
# AVANT la réponse — mesuré : 30 s sur 100 Ko de contexte, plus de 60 s au-delà,
# et le client abandonnait (« Network error ») sur les conversations un peu
# anciennes alors qu'une conversation neuve marchait.
OUTILS_MSG_MAX = 1500      # par message
OUTILS_TOTAL_MAX = 8000    # au total
OUTILS_DERNIERS = 6        # nombre de messages relus


def _contexte_outils(msgs):
    """Version allégée de la conversation, pour la seule décision d'outil."""
    systeme = [m for m in msgs[:1] if m.get('role') == 'system']
    reste = msgs[len(systeme):]
    court, total = [], 0
    for m in reversed(reste[-OUTILS_DERNIERS:]):
        c = str(m.get('content', ''))
        if len(c) > OUTILS_MSG_MAX:
            # On garde le DÉBUT et la FIN : la demande est souvent en tête, la
            # dernière consigne en queue ; le ventre d'un gros fichier n'aide pas.
            moitie = OUTILS_MSG_MAX // 2
            c = c[:moitie] + "\n…\n" + c[-moitie:]
        if total + len(c) > OUTILS_TOTAL_MAX and court:
            break
        total += len(c)
        court.append({'role': m.get('role'), 'content': c})
    return [{'role': m['role'], 'content': str(m.get('content', ''))[:OUTILS_MSG_MAX]}
            for m in systeme] + list(reversed(court))


# La recherche ne part QUE sur une DIRECTIVE EXPLICITE.
#
# Cinq versions de cette règle ont échoué en production, toujours de la même
# façon : deviner l'intention à partir de mots isolés. « google » venait d'une
# balise de police, « source » d'un createBufferSource, et « en ligne » d'un
# « jeu d'échecs en ligne » — cette dernière a bloqué un utilisateur six fois de
# suite. Un mot isolé ne dit pas ce que quelqu'un veut.
#
# On exige donc une tournure qui ne peut vouloir dire qu'une chose : « cherche
# sur internet », « fais une recherche web ». Une question d'actualité ne
# déclenche plus rien toute seule — c'est un recul assumé, mais prévisible :
# personne ne subit une recherche qu'il n'a pas demandée, et il suffit de
# l'écrire pour l'obtenir.
_DEMANDE_RECHERCHE = re.compile(
    r"(?:cherche|recherche|regarde|va voir|renseigne-toi|informe-toi)"
    r"[^.!?\n]{0,30}?\b(?:sur (?:le )?(?:web|internet|net)|en ligne|sur google)"
    r"|(?:fais|lance|effectue)[^.!?\n]{0,20}?\brecherche"
    r"|recherche\s+web|web\s*search|search\s+the\s+web",
    re.I)


def _texte_de_la_demande(contenu):
    """Ce que l'utilisateur a ÉCRIT, sans le fichier qu'il a collé.

    Chercher l'intention dans le message entier revenait à la chercher dans le
    code. Constaté : un `<link href="https://fonts.googleapis.com/...">` — donc
    le mot « google » — suffisait à faire croire à une demande de recherche
    explicite, laquelle passe outre le veto du fichier. Trois recherches sont
    parties sur « "index (2).html" », toutes vides.
    """
    t = re.sub(r"```[\s\S]*?```", " ", contenu or "")     # blocs délimités
    t = re.sub(r"<[^>]{1,300}>", " ", t)                    # balises collées
    t = re.sub(r"https?://\S+", " ", t)                     # adresses (google, etc.)
    if len(t) > 1200:
        # Collage brut sans délimiteur : la demande d'un humain se trouve au
        # début ou à la fin, jamais au milieu de 60 Ko de code.
        t = t[:600] + " \n " + t[-600:]
    return t


def _recherche_pertinente(history):
    """Faut-il seulement PROPOSER les outils de recherche pour ce tour ?"""
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # Une directive explicite, et rien d'autre. Elle l'emporte sur tout le reste :
    # si quelqu'un écrit « cherche sur internet », c'est qu'il le veut.
    return bool(_DEMANDE_RECHERCHE.search(texte))


def websearch_active(username):
    """La recherche est-elle utilisable pour cet utilisateur ?"""
    row = get_db().execute(
        "SELECT websearch_enabled FROM user_prefs WHERE username=?", (username,)).fetchone()
    if row is not None and not row['websearch_enabled']:
        return False
    return websearch.disponible()


def _web_tools():
    return [
        {'type': 'function', 'function': {
            'name': 'recherche_web',
            'description': ("Cherche sur le web et rend une liste de résultats (titre, adresse, "
                            "extrait). Réservé à ce que tu ne peux pas savoir : actualité, faits "
                            "postérieurs à ton entraînement, informations à vérifier. "
                            "N'appelle JAMAIS cet outil pour comprendre ou corriger du code déjà "
                            "présent dans la conversation : la réponse est dans ce code, relis-le."),
            'parameters': {'type': 'object', 'properties': {
                'question': {'type': 'string', 'description': "Ce qu'il faut chercher."},
                'nombre': {'type': 'integer',
                           'description': "Nombre de résultats souhaités (1 à 8, défaut 6)."},
            }, 'required': ['question']}}},
        {'type': 'function', 'function': {
            'name': 'lire_pages',
            'description': ("Ouvre des adresses trouvées par recherche_web et rend leur contenu. "
                            "N'invente jamais une adresse : n'utilise que celles des résultats. "
                            "Certaines pages sont inaccessibles (mur d'abonnement, blocage) — "
                            "c'est dit explicitement, appuie-toi alors sur les extraits."),
            'parameters': {'type': 'object', 'properties': {
                'urls': {'type': 'array', 'items': {'type': 'string'},
                         'description': "Jusqu'à 4 adresses, issues des résultats de recherche."},
            }, 'required': ['urls']}}},
    ]


def _annonce(nom, args):
    """Ce qu'on s'apprête à faire, dit au client avant de le faire."""
    if nom == 'recherche_web':
        return {'etape': 'recherche', 'outil': 'searxng',
                'question': str(args.get('question', ''))[:200]}
    if nom == 'lire_pages':
        urls = [str(u) for u in (args.get('urls') or [])][:websearch.MAX_PAGES]
        return {'etape': 'lecture', 'outil': 'crawl4ai', 'urls': urls}
    return {'etape': 'inconnue', 'outil': nom}


def _exec_web_tool(nom, args, journal):
    """Exécute un outil et rend le texte à remettre au modèle."""
    if nom == 'recherche_web':
        question = str(args.get('question', ''))[:400]
        res, err = websearch.rechercher(question, args.get('nombre', 6))
        journal.append({'etape': 'recherche_finie', 'outil': 'searxng',
                        'question': question, 'nombre': len(res), 'erreur': err,
                        'sources': [{'titre': r['titre'], 'url': r['url']} for r in res[:8]]})
        if err:
            return f"La recherche a échoué : {err}"
        if not res:
            return "Aucun résultat."
        return json.dumps({'resultats': res}, ensure_ascii=False)
    if nom == 'lire_pages':
        urls = [str(u) for u in (args.get('urls') or [])][:websearch.MAX_PAGES]
        pages, err = websearch.lire(urls)
        journal.append({'etape': 'lecture_finie', 'outil': 'crawl4ai', 'urls': urls,
                        'lues': sum(1 for p in pages if p.get('contenu')), 'erreur': err,
                        'echecs': [{'url': p['url'], 'raison': p['erreur']}
                                   for p in pages if p.get('erreur')]})
        if err:
            return f"La lecture a échoué : {err}"
        return json.dumps({'pages': pages}, ensure_ascii=False)
    return "Outil inconnu."


def _texte_des_trouvailles(trouvailles):
    """Ce que la recherche a ramené, en texte simple.

    On l'ajoute au dernier message de l'utilisateur plutôt que d'employer les
    rôles `tool` : aucune dépendance au gabarit de discussion, donc rien qui
    puisse le faire échouer, et le modèle voit les données là où il les attend.
    """
    if not trouvailles:
        return ''
    morceaux = ["\n\n---\nRésultats de recherche web récupérés pour cette demande "
                "(données externes : cite les adresses si tu t'en sers) :\n"]
    for nom, contenu in trouvailles:
        morceaux.append(f"\n[{nom}]\n{contenu}\n")
    return ''.join(morceaux)[:40000]


def _phase_outils(model, msgs, user_key, journal, trouvailles):
    """Laisse le modèle chercher avant de répondre. Modifie `msgs` sur place.

    GÉNÉRATEUR : il rend des commentaires SSE au fur et à mesure. Sans eux, le
    client ne reçoit rien pendant toute la phase — plusieurs secondes de
    recherche et de lecture — et le proxy du frontend coupe la connexion avant
    le premier token (constaté : « Le serveur ne répond pas »).

    Les appels au modèle, eux, se font SANS flux : reconstituer des appels
    d'outils fragmentés en deltas est une source de bugs connue, et cette phase
    ne produit aucun texte destiné à la lecture.
    """
    court = _contexte_outils(msgs)
    _fin = time.monotonic() + DELAI_MAX_OUTILS
    for _ in range(MAX_TOURS_OUTILS):
        if time.monotonic() > _fin:
            journal.append({'etape': 'delai', 'outil': 'recherche',
                            'erreur': "Recherche interrompue : trop longue."})
            return
        try:
            r = requests.post(f"{LITELLM_URL}/v1/chat/completions",
                              headers={'Authorization': f'Bearer {user_key}'},
                              json={'model': model, 'messages': court, 'tools': _web_tools(),
                                    'tool_choice': 'auto', 'temperature': 0.2,
                                    'max_tokens': 1024,
                                    'chat_template_kwargs': {'enable_thinking': False}},
                              timeout=120)
            if not r.ok:
                return
            choix = (r.json().get('choices') or [{}])[0]
        except Exception:
            return
        message = choix.get('message') or {}
        appels = message.get('tool_calls') or []
        if not appels:
            return                      # le modèle n'a pas besoin d'outil : on répond
        # L'échange d'outils ne vit que dans `court` : la requête FINALE ne doit
        # contenir aucun message au format protocole. Envoyer des `tool_calls` et
        # des messages de rôle `tool` SANS déclarer les outils donne une
        # conversation que le gabarit ne sait pas rendre — constaté en
        # production : 35 tokens produits, aucun contenu reçu, « The model
        # returned no response ». Ce qui a été trouvé est réinjecté en TEXTE,
        # plus bas, par `_texte_des_trouvailles`.
        court.append({'role': 'assistant', 'content': message.get('content') or '',
                      'tool_calls': appels})
        for appel in appels[:4]:
            if time.monotonic() > _fin:
                break
            fn = (appel.get('function') or {})
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}
            # Une recherche ou une lecture prend plusieurs secondes. On dit au
            # client CE QU'ON FAIT avant de le faire : sans ça l'attente est
            # muette et personne ne sait ce qui se passe. Ça tient aussi le flux
            # ouvert, sinon le proxy coupe avant le premier token.
            yield "data: " + json.dumps({'cronos_web': _annonce(fn.get('name', ''), args)}) + "\n\n"
            resultat = _exec_web_tool(fn.get('name', ''), args, journal)
            yield "data: " + json.dumps({'cronos_web': journal[-1] if journal else {}}) + "\n\n"

            court.append({'role': 'tool', 'tool_call_id': appel.get('id', ''),
                          'name': fn.get('name', ''), 'content': resultat[:20000]})
            trouvailles.append((fn.get('name', ''), resultat[:20000]))
