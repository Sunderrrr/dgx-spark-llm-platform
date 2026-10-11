"""Web search for the playground: SearXNG searches, crawl4ai reads.

Two distinct services, deliberately:
  - **SearXNG** (self-hosted meta-engine) turns a question into a list of links.
    crawl4ai cannot search — it can only extract a URL it is given.
  - **crawl4ai** opens the selected pages and renders clean markdown.

Both live on a dedicated docker network (`web_net`), with no route to litellm
nor postgres, and a systemd unit forbids them from opening the least
connection to the host machine: crawl4ai drives a browser over pages entirely
controlled by third parties, it is the platform's most exposed component.

This module adds the last barrier, the one the network cannot set up:
no URL goes to the crawler without its host having been resolved and checked public.
"""
import ipaddress
import os
import socket
import threading
from urllib.parse import urlparse

import requests

from mcp_client import _is_blocked_ip          # same policy as the MCP servers

SEARXNG_URL = os.environ.get('SEARXNG_URL', 'http://searxng:8080')
CRAWL4AI_URL = os.environ.get('CRAWL4AI_URL', 'http://crawl4ai:11235')
_TOKEN_FILE = os.environ.get('CRAWL4AI_TOKEN_FILE', '/run/secrets/crawl4ai_token')

# Bounds: what goes to the model must stay readable and fit in the context.
# Extraction settings, chosen by measurement (see tests/test_websearch.py):
#  - `ignore_links`: without it, a doc page comes out with 325 navigation links
#    drowning the text. With it, the content stays intact — code blocks included.
#  - `excluded_tags`: removes menus, footers and sidebars. Measured -61 % of
#    volume on a product page, -75 % on a news page.
# Relevance pruning (PruningContentFilter) was TRIED then dropped: it removes
# code blocks — `TaskGroup` and `asyncio.gather` vanished from a Python
# documentation page — with no gain on cookie walls.
_EXTRACTION = {
    'cache_mode': 'bypass',
    'excluded_tags': ['nav', 'footer', 'header', 'aside', 'script', 'style', 'form'],
    'markdown_generator': {
        'type': 'DefaultMarkdownGenerator',
        'params': {'options': {'ignore_links': True, 'ignore_images': True}},
    },
}

# Many sites serve their banner BEFORE the article: consent, subscription,
# notification feed. Measured: on letelegramme.fr the real headlines only come
# at the 4th line, and on tf1info.fr every headline is prefixed with
# « Nouvelle notification » — including the one we were looking for. Judging
# the page by its START therefore meant discarding pages that held the answer;
# we remove those lines, and only give up if the rest is really empty.
#
# We filter by LINE and never by length: a short line can be code, and a
# documentation page is full of them.
BANNER_LINES = (
    'continuer sans accepter', 'utilisons des cookies', 'accepter les cookies',
    'gérer mes choix', 'gerer mes choix', 'politique de confidentialité',
    'votre carte de paiement', 'votre abonnement', "n'a pas encore été validé",
    'abonnez-vous', 'accept cookies', 'privacy preferences', 'consent',
    'enable javascript', 'activez javascript', 'required part of this site',
)
# Parasite prefixes stuck to useful headings: we remove the prefix, not the line.
NOISE_PREFIXES = ('Vidéo Nouvelle notification', 'Nouvelle notification')
MIN_USEFUL_WORDS = 60

MAX_RESULTS = 8
MAX_PAGES = 4
MAX_CARS_PAGE = 6000
MAX_CARS_TOTAL = 20000
SEARCH_TIMEOUT = 20
READ_TIMEOUT = 90

_token_cache = None
_token_lock = threading.Lock()


def _token():
    """crawl4ai API token, read once from the mounted file."""
    global _token_cache
    with _token_lock:
        if _token_cache is None:
            try:
                with open(_TOKEN_FILE, encoding='utf-8') as f:
                    _token_cache = f.read().strip()
            except OSError:
                _token_cache = ''
    return _token_cache


def available():
    """Do both services answer? Used to expose the tool only when it works."""
    try:
        r = requests.get(f"{CRAWL4AI_URL}/health", timeout=3)
        if not r.ok:
            return False
        return requests.get(f"{SEARXNG_URL}/healthz", timeout=3).ok
    except Exception:
        return False


def public_url(url):
    """(ok, error) — does the URL really target a public address?

    The network already forbids the crawler from reaching the host, but
    nothing would stop it from targeting another machine on the local
    network. We refuse any URL whose host resolves, even partly, to private.
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
    # A literal IP is checked directly, without resolution.
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


def internal_target(url):
    """Does the URL target an internal address, with CERTAINTY?

    Used for the AFTER-read check, on the final URL rendered by the crawler
    (`redirected_url`): `url_publique` only sees the seeds, so a public page
    redirecting to `http://<service interne>` did go fetch its content there —
    crawl4ai validates the seeds « before fetching », and nothing after
    (checked in its `api.py`).

    Deliberate difference from `url_publique`: here IGNORANCE does not refuse.
    This URL comes from the crawler, not the user; an unexpected scheme or a
    name that does not resolve on our side proves nothing, and refusing would
    make legitimate pages disappear. We only reject what we know internal.
    """
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = p.hostname or ''
    if p.scheme not in ('http', 'https') or not host:
        return False
    try:
        ipaddress.ip_address(host)
        return _is_blocked_ip(host)
    except ValueError:
        pass
    try:
        return any(_is_blocked_ip(i[4][0]) for i in socket.getaddrinfo(host, None))
    except socket.gaierror:
        return False


def clean(text):
    """Removes banner lines and parasite prefixes. Returns the text."""
    outputs = []
    for line in (text or '').split('\n'):
        l = line.rstrip()
        low = l.lower()
        if any(m in low for m in BANNER_LINES):
            continue
        for prefix in NOISE_PREFIXES:
            if l.lstrip('* ').startswith(prefix):
                l = l.lstrip('* ')[len(prefix):].lstrip()
                break
        outputs.append(l)
    # The multiple blanks left by removed lines add nothing.
    clean_value, empty = [], False
    for l in outputs:
        if l.strip():
            clean_value.append(l); empty = False
        elif not empty:
            clean_value.append(''); empty = True
    return '\n'.join(clean_value).strip()


def _too_poor(text):
    """Is the page empty of substance once cleaned? (None if fine.)"""
    words = sum(1 for m in (text or '').split() if len(m) > 3)
    if words < MIN_USEFUL_WORDS:
        return "Page sans contenu exploitable (chargée en JavaScript, ou accès refusé)."
    return None


def search(question, count_value=6, language='fr'):
    """Question → links. Returns (results, error)."""
    question = (question or '').strip()[:400]
    if not question:
        return [], "Question vide."
    count_value = max(1, min(int(count_value or 6), MAX_RESULTS))
    try:
        r = requests.get(f"{SEARXNG_URL}/search", timeout=SEARCH_TIMEOUT,
                         params={'q': question, 'format': 'json', 'language': language,
                                 'safesearch': 1})
        r.raise_for_status()
        raw = r.json().get('results') or []
    except Exception as e:
        return [], f"Moteur de recherche injoignable ({type(e).__name__})."
    out, seen = [], set()
    for item in raw:
        url = (item.get('url') or '').strip()
        if not url or url in seen:
            continue
        ok, _ = public_url(url)
        if not ok:
            continue
        seen.add(url)
        out.append({'titre': (item.get('title') or '')[:200],
                    'url': url,
                    'extrait': (item.get('content') or '')[:400]})
        if len(out) >= count_value:
            break
    return out, None


def read(urls, max_cars=MAX_CARS_PAGE):
    """URLs → markdown content. Returns (pages, error)."""
    valid_items = []
    for u in (urls or [])[:MAX_PAGES]:
        ok, err = public_url(u)
        valid_items.append((u, ok, err))
    to_read = [u for u, ok, _ in valid_items if ok]
    results = {}
    if to_read:
        try:
            r = requests.post(f"{CRAWL4AI_URL}/crawl", timeout=READ_TIMEOUT,
                              headers={'Authorization': f'Bearer {_token()}'},
                              json={'urls': to_read,
                                    'crawler_config': {'type': 'CrawlerRunConfig',
                                                       'params': _EXTRACTION}})
            r.raise_for_status()
            for item in (r.json().get('results') or []):
                md = item.get('markdown')
                if isinstance(md, dict):
                    md = md.get('fit_markdown') or md.get('raw_markdown') or ''
                # AFTER-read check: the seed was public, but the
                # crawler may have followed a redirect to an internal address.
                # The content of an internal service has no business in the
                # model's context — we drop the whole page (the markdown
                # is not even kept).
                final_text = item.get('redirected_url')
                if isinstance(final_text, str) and internal_target(final_text):
                    results[item.get('url', '')] = {
                        'ok': False,
                        'erreur': "Redirection vers une adresse interne, page refusée.",
                    }
                    continue
                results[item.get('url', '')] = {
                    'ok': bool(item.get('success')),
                    'markdown': (md or '')[:max_cars],
                    'titre': ((item.get('metadata') or {}).get('title') or '')[:200],
                }
        except Exception as e:
            return [], f"Lecteur de pages injoignable ({type(e).__name__})."
    pages, total = [], 0
    for u, ok, err in valid_items:
        if not ok:
            pages.append({'url': u, 'erreur': err})
            continue
        # crawl4ai may normalize the URL (trailing slash): we fall back on it.
        info = results.get(u) or next((v for k, v in results.items()
                                         if k.rstrip('/') == u.rstrip('/')), None)
        if not info or not info['ok']:
            # `results` may carry a reason (refusal after redirect): losing it
            # would display « Page illisible. » for a page that read fine.
            pages.append({'url': u, 'erreur': (info or {}).get('erreur') or "Page illisible."})
            continue
        clean_value = clean(info['markdown'])
        issue = _too_poor(clean_value)
        if issue:
            pages.append({'url': u, 'titre': info['titre'], 'erreur': issue})
            continue
        remaining = max(0, MAX_CARS_TOTAL - total)
        text = clean_value[:remaining]
        total += len(text)
        pages.append({'url': u, 'titre': info['titre'], 'contenu': text})
    return pages, None
