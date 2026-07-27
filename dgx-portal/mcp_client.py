"""Client MCP ("Model Context Protocol") minimal et synchrone.

Le reste de l'app est du Flask/gunicorn synchrone qui utilise `requests`
partout (ldap_authenticate, _chat, runner_launch...). Plutôt que d'importer
le SDK officiel `mcp` (async-first : pydantic + anyio + httpx, à envelopper
dans asyncio.run() à chaque appel), ce module reparle directement le
transport « Streamable HTTP » de MCP — du JSON-RPC 2.0 simple sur HTTP POST —
en ne couvrant que ce dont le chat Support a besoin : découvrir et appeler des
outils (tools/list, tools/call). Pas de resources/prompts/sampling/roots.

Chaque enregistrement de serveur MCP est fait par un utilisateur authentifié
mais non admin (voir /mcp dans app.py) : le backend doit alors émettre des
requêtes HTTP sortantes vers une URL que cet utilisateur contrôle, depuis le
même réseau docker que litellm (LITELLM_MASTER_KEY), vllm-runner (peut
lancer/arrêter un modèle) et postgres. _validate_url() est donc une défense
SSRF obligatoire, pas un détail : elle résout le nom d'hôte et rejette toute
IP privée/loopback/link-local (dont l'adresse de métadonnées cloud
169.254.169.254)/CGNAT, en plus des noms de service docker-compose connus.
Elle est appelée à l'enregistrement ET juste avant chaque requête live —
cela n'élimine pas un DNS-rebinding minuté exactement entre la résolution et
la connexion (il faudrait épingler l'IP via un adapter de transport custom),
mais c'est une mitigation raisonnable pour une base d'utilisateurs interne et
authentifiée, pas le grand internet anonyme.
"""

import ipaddress
import json
import socket
import time
import uuid
from urllib.parse import urlparse

import requests

_BLOCKED_HOSTNAMES = {
    'litellm', 'postgres', 'vllm-runner', 'dgx-portal', 'dgx-portal-frontend',
    'lldap.cronos.lan', 'host.docker.internal', 'localhost',
}


def _is_blocked_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # IP illisible : on refuse plutôt que de laisser passer
    return (
        ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        # CGNAT (100.64.0.0/10) : pas couvert par is_private sur toutes les
        # versions de Python, vérifié explicitement.
        or ip in ipaddress.ip_network('100.64.0.0/10')
    )


def validate_mcp_url(url):
    """Retourne (ok, message_erreur_ou_None)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL invalide."
    if parsed.scheme != 'https':
        return False, "L'URL doit être en https://."
    host = parsed.hostname or ''
    if not host:
        return False, "URL invalide (pas d'hôte)."
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False, "Cet hôte n'est pas autorisé."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Nom d'hôte introuvable."
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            return False, "Cette adresse pointe vers un réseau interne/privé, refusée."
    return True, None


class MCPError(Exception):
    pass


class MCPClient:
    """Client HTTP synchrone pour un serveur MCP distant (transport Streamable HTTP)."""

    # Court : un serveur MCP lent enregistré par un utilisateur peut bloquer
    # un thread gunicorn (peu de workers/threads) pendant toute la durée du
    # timeout, jusqu'à 4 fois dans la boucle d'outils de support_chat().
    def __init__(self, url, auth_header=None, timeout=5):
        self.url = url
        self.auth_header = auth_header
        self.timeout = timeout
        self._session_id = None

    def _headers(self):
        h = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if self.auth_header:
            h['Authorization'] = self.auth_header
        if self._session_id:
            h['Mcp-Session-Id'] = self._session_id
        return h

    def _rpc(self, method, params=None):
        ok, err = validate_mcp_url(self.url)
        if not ok:
            raise MCPError(err)
        payload = {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': method,
                   'params': params or {}}
        # allow_redirects=False : requests suit les redirections par défaut
        # SANS revalider l'hôte de destination contre validate_mcp_url —
        # un serveur passerait la validation puis rediriger vers une IP
        # interne la contournerait entièrement. On refuse toute redirection
        # plutôt que de la suivre.
        r = requests.post(self.url, json=payload, headers=self._headers(),
                          timeout=self.timeout, allow_redirects=False)
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            raise MCPError("Le serveur MCP a répondu par une redirection, refusée.")
        if r.status_code >= 400:
            raise MCPError(f"Le serveur MCP a renvoyé une erreur ({r.status_code}).")
        sid = r.headers.get('Mcp-Session-Id')
        if sid:
            self._session_id = sid
        ctype = r.headers.get('Content-Type', '')
        if 'text/event-stream' in ctype:
            data = None
            for line in r.text.splitlines():
                if line.startswith('data:'):
                    try:
                        data = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    break
            if data is None:
                raise MCPError("Réponse SSE du serveur MCP illisible.")
        else:
            try:
                data = r.json()
            except Exception:
                raise MCPError("Réponse du serveur MCP illisible.")
        if 'error' in data:
            raise MCPError(str(data['error'].get('message', 'Erreur MCP.')))
        return data.get('result', {})

    def initialize(self):
        result = self._rpc('initialize', {
            'protocolVersion': '2025-06-18',
            'capabilities': {},
            'clientInfo': {'name': 'cronos-support', 'version': '1.0'},
        })
        # Notification (pas de réponse attendue) — best-effort, certains
        # serveurs l'exigent avant d'accepter tools/list. allow_redirects=False
        # pour la même raison que dans _rpc().
        try:
            requests.post(self.url, headers=self._headers(), timeout=self.timeout,
                          allow_redirects=False,
                          json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except Exception:
            pass
        return result

    def list_tools(self):
        result = self._rpc('tools/list')
        return result.get('tools', [])

    def call_tool(self, name, arguments):
        result = self._rpc('tools/call', {'name': name, 'arguments': arguments})
        parts = [c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text']
        text = '\n'.join(p for p in parts if p) or '(réponse vide du serveur MCP)'
        return text, not result.get('isError', False)


# Cache mémoire process-local (pas partagé entre workers gunicorn, ce qui est
# acceptable : au pire un worker refait un tools/list qu'un autre a déjà en
# cache — pas un problème de correction, juste une micro-économie de latence).
_tools_cache = {}
_TOOLS_TTL = 120


def list_tools_cached(server_id, url, auth_header):
    now = time.monotonic()
    cached = _tools_cache.get(server_id)
    if cached and now - cached[0] < _TOOLS_TTL:
        return cached[1]
    client = MCPClient(url, auth_header)
    try:
        client.initialize()
        tools = client.list_tools()
    except Exception:
        return []
    _tools_cache[server_id] = (now, tools)
    return tools
