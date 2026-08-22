"""Recherche web pour le playground : SearXNG cherche, crawl4ai lit.

Deux services distincts, volontairement :
  - **SearXNG** (méta-moteur auto-hébergé) traduit une question en liste de liens.
    crawl4ai ne sait pas chercher — il ne sait qu'extraire une URL qu'on lui donne.
  - **crawl4ai** ouvre les pages retenues et en rend un markdown propre.

Les deux vivent sur un réseau docker dédié (`web_net`), sans route vers litellm
ni postgres, et une unité systemd leur interdit d'ouvrir la moindre connexion
vers la machine hôte : crawl4ai pilote un navigateur sur des pages entièrement
contrôlées par des tiers, c'est le composant le plus exposé de la plateforme.

Ce module ajoute la dernière barrière, celle que le réseau ne peut pas poser :
aucune URL ne part au crawler sans que son hôte ait été résolu et vérifié public.
"""
import ipaddress
import os
import socket
import threading
from urllib.parse import urlparse

import requests

from mcp_client import _is_blocked_ip          # même politique que les serveurs MCP

SEARXNG_URL = os.environ.get('SEARXNG_URL', 'http://searxng:8080')
CRAWL4AI_URL = os.environ.get('CRAWL4AI_URL', 'http://crawl4ai:11235')
_TOKEN_FILE = os.environ.get('CRAWL4AI_TOKEN_FILE', '/run/secrets/crawl4ai_token')

# Bornes : ce qui part au modèle doit rester lisible et tenir dans le contexte.
# Réglages d'extraction, choisis sur mesure (voir tests/test_websearch.py) :
#  - `ignore_links` : sans lui, une page de doc sort avec 325 liens de navigation
#    noyant le texte. Avec, le contenu reste intact — y compris les blocs de code.
#  - `excluded_tags` : retire menus, pieds de page et barres latérales. Mesuré
#    -61 % de volume sur une page produit, -75 % sur une page d'actualité.
# L'élagage par pertinence (PruningContentFilter) a été ESSAYÉ puis écarté : il
# supprime les blocs de code — `TaskGroup` et `asyncio.gather` disparaissaient
# d'une page de documentation Python — sans rien gagner sur les murs de cookies.
_EXTRACTION = {
    'cache_mode': 'bypass',
    'excluded_tags': ['nav', 'footer', 'header', 'aside', 'script', 'style', 'form'],
    'markdown_generator': {
        'type': 'DefaultMarkdownGenerator',
        'params': {'options': {'ignore_links': True, 'ignore_images': True}},
    },
}

# Beaucoup de sites servent leur bandeau AVANT l'article : consentement,
# abonnement, fil de notifications. Mesuré : sur letelegramme.fr les vrais titres
# n'arrivent qu'à la 4ᵉ ligne, et sur tf1info.fr chaque titre est préfixé de
# « Nouvelle notification » — dont celui qu'on cherchait. Juger la page sur son
# DÉBUT revenait donc à jeter des pages qui contenaient la réponse ; on retire
# ces lignes-là, et on ne renonce que si le reste est vraiment vide.
#
# On filtre par LIGNE et jamais par longueur : une ligne courte peut être du
# code, et une page de documentation en est pleine.
LIGNES_DE_BANDEAU = (
    'continuer sans accepter', 'utilisons des cookies', 'accepter les cookies',
    'gérer mes choix', 'gerer mes choix', 'politique de confidentialité',
    'votre carte de paiement', 'votre abonnement', "n'a pas encore été validé",
    'abonnez-vous', 'accept cookies', 'privacy preferences', 'consent',
    'enable javascript', 'activez javascript', 'required part of this site',
)
# Préfixes parasites collés à des titres utiles : on retire le préfixe, pas la ligne.
PREFIXES_PARASITES = ('Vidéo Nouvelle notification', 'Nouvelle notification')
MIN_MOTS_UTILES = 60

MAX_RESULTATS = 8
MAX_PAGES = 4
MAX_CARS_PAGE = 6000
MAX_CARS_TOTAL = 20000
TIMEOUT_RECHERCHE = 20
TIMEOUT_LECTURE = 90

_token_cache = None
_token_lock = threading.Lock()


def _token():
    """Jeton d'API de crawl4ai, lu une fois depuis le fichier monté."""
    global _token_cache
    with _token_lock:
        if _token_cache is None:
            try:
                with open(_TOKEN_FILE, encoding='utf-8') as f:
                    _token_cache = f.read().strip()
            except OSError:
                _token_cache = ''
    return _token_cache


def disponible():
    """Les deux services répondent-ils ? Sert à n'exposer l'outil que s'il marche."""
    try:
        r = requests.get(f"{CRAWL4AI_URL}/health", timeout=3)
        if not r.ok:
            return False
        return requests.get(f"{SEARXNG_URL}/healthz", timeout=3).ok
    except Exception:
        return False


def url_publique(url):
    """(ok, erreur) — l'URL vise-t-elle bien une adresse publique ?

    Le réseau interdit déjà au crawler d'atteindre l'hôte, mais rien ne
    l'empêcherait de viser une autre machine du réseau local. On refuse donc
    toute URL dont l'hôte résout, même partiellement, vers du privé.
    """
    try:
        p = urlparse(url)
    except Exception:
        return False, "URL illisible."
    if p.scheme not in ('http', 'https'):
        return False, "Seuls http et https sont acceptés."
    host = p.hostname or ''
    if not host:
        return False, "URL sans hôte."
    # Une IP écrite en clair se vérifie directement, sans résolution.
    try:
        ipaddress.ip_address(host)
        return (False, "Adresse interne refusée.") if _is_blocked_ip(host) else (True, None)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Nom d'hôte introuvable."
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            return False, "Cet hôte pointe vers un réseau interne, refusé."
    return True, None


def nettoyer(texte):
    """Retire les lignes de bandeau et les préfixes parasites. Retourne le texte."""
    sorties = []
    for ligne in (texte or '').split('\n'):
        l = ligne.rstrip()
        bas = l.lower()
        if any(m in bas for m in LIGNES_DE_BANDEAU):
            continue
        for prefixe in PREFIXES_PARASITES:
            if l.lstrip('* ').startswith(prefixe):
                l = l.lstrip('* ')[len(prefixe):].lstrip()
                break
        sorties.append(l)
    # Les blancs multiples laissés par les lignes retirées n'apportent rien.
    propre, vide = [], False
    for l in sorties:
        if l.strip():
            propre.append(l); vide = False
        elif not vide:
            propre.append(''); vide = True
    return '\n'.join(propre).strip()


def _trop_pauvre(texte):
    """La page est-elle vide de substance une fois nettoyée ? (None si ça va.)"""
    mots = sum(1 for m in (texte or '').split() if len(m) > 3)
    if mots < MIN_MOTS_UTILES:
        return "Page sans contenu exploitable (chargée en JavaScript, ou accès refusé)."
    return None


def rechercher(question, nombre=6, langue='fr'):
    """Question → liens. Retourne (resultats, erreur)."""
    question = (question or '').strip()[:400]
    if not question:
        return [], "Question vide."
    nombre = max(1, min(int(nombre or 6), MAX_RESULTATS))
    try:
        r = requests.get(f"{SEARXNG_URL}/search", timeout=TIMEOUT_RECHERCHE,
                         params={'q': question, 'format': 'json', 'language': langue,
                                 'safesearch': 1})
        r.raise_for_status()
        brut = r.json().get('results') or []
    except Exception as e:
        return [], f"Moteur de recherche injoignable ({type(e).__name__})."
    out, vus = [], set()
    for item in brut:
        url = (item.get('url') or '').strip()
        if not url or url in vus:
            continue
        ok, _ = url_publique(url)
        if not ok:
            continue
        vus.add(url)
        out.append({'titre': (item.get('title') or '')[:200],
                    'url': url,
                    'extrait': (item.get('content') or '')[:400]})
        if len(out) >= nombre:
            break
    return out, None


def lire(urls, max_cars=MAX_CARS_PAGE):
    """URLs → contenu markdown. Retourne (pages, erreur)."""
    valides = []
    for u in (urls or [])[:MAX_PAGES]:
        ok, err = url_publique(u)
        valides.append((u, ok, err))
    a_lire = [u for u, ok, _ in valides if ok]
    resultats = {}
    if a_lire:
        try:
            r = requests.post(f"{CRAWL4AI_URL}/crawl", timeout=TIMEOUT_LECTURE,
                              headers={'Authorization': f'Bearer {_token()}'},
                              json={'urls': a_lire,
                                    'crawler_config': {'type': 'CrawlerRunConfig',
                                                       'params': _EXTRACTION}})
            r.raise_for_status()
            for item in (r.json().get('results') or []):
                md = item.get('markdown')
                if isinstance(md, dict):
                    md = md.get('fit_markdown') or md.get('raw_markdown') or ''
                resultats[item.get('url', '')] = {
                    'ok': bool(item.get('success')),
                    'markdown': (md or '')[:max_cars],
                    'titre': ((item.get('metadata') or {}).get('title') or '')[:200],
                }
        except Exception as e:
            return [], f"Lecteur de pages injoignable ({type(e).__name__})."
    pages, total = [], 0
    for u, ok, err in valides:
        if not ok:
            pages.append({'url': u, 'erreur': err})
            continue
        # crawl4ai peut normaliser l'URL (barre finale) : on retombe dessus.
        info = resultats.get(u) or next((v for k, v in resultats.items()
                                         if k.rstrip('/') == u.rstrip('/')), None)
        if not info or not info['ok']:
            pages.append({'url': u, 'erreur': "Page illisible."})
            continue
        propre = nettoyer(info['markdown'])
        souci = _trop_pauvre(propre)
        if souci:
            pages.append({'url': u, 'titre': info['titre'], 'erreur': souci})
            continue
        reste = max(0, MAX_CARS_TOTAL - total)
        texte = propre[:reste]
        total += len(texte)
        pages.append({'url': u, 'titre': info['titre'], 'contenu': texte})
    return pages, None
