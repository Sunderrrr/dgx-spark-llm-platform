"""Minimal synchronous MCP ("Model Context Protocol") client.

The rest of the app is synchronous Flask/gunicorn that uses `requests`
everywhere (ldap_authenticate, _chat, runner_launch...). Rather than importing
the official `mcp` SDK (async-first: pydantic + anyio + httpx, to be wrapped
in asyncio.run() at every call), this module speaks MCP's
« Streamable HTTP » transport directly — plain JSON-RPC 2.0 over HTTP POST —
covering only what the Support chat needs: discovering and calling tools
(tools/list, tools/call). No resources/prompts/sampling/roots.

Every MCP server registration is done by an authenticated but non-admin
user (see /mcp in app.py): the backend must then issue outgoing HTTP
requests to a URL that user controls, from the same docker network as
litellm (LITELLM_MASTER_KEY), vllm-runner (can start/stop a model) and
postgres. _validate_url() is therefore a mandatory SSRF defence, not a
detail: it resolves the hostname and rejects any private/loopback/link-local
IP (including the cloud metadata address 169.254.169.254)/CGNAT, on top of
the known docker-compose service names.
It is called at registration AND right before every live request —
this does not eliminate a DNS-rebinding timed exactly between the resolution
and the connection (that would require pinning the IP via a custom transport
adapter), but it is a reasonable mitigation for an internal, authenticated
user base, not the wide anonymous internet.
"""

import ipaddress
import json
import socket
import time
import uuid
from urllib.parse import urlparse, urlunparse

import requests

_BLOCKED_HOSTNAMES = {
    'litellm', 'postgres', 'vllm-runner', 'dgx-portal', 'dgx-portal-frontend',
    'lldap.cronos.lan', 'host.docker.internal', 'localhost',
}


def _is_blocked_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Unreadable IP: refuse rather than let it through
    return (
        ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        # CGNAT (100.64.0.0/10): not covered by is_private on all Python
        # versions, checked explicitly.
        or ip in ipaddress.ip_network('100.64.0.0/10')
    )


def resolve_validated_mcp_ip(url):
    """Validates the URL and resolves the host **exactly once**. Returns
    (ok, error_message_or_None, ip_to_pin).

    The returned IP must be the one used for the connection (see
    `_PinnedIPHTTPSAdapter`): without it, `requests` re-resolves the name at
    connection time and a server can answer a public IP to this validation
    then `127.0.0.1`/an internal IP at the 2nd resolution (DNS-rebinding /
    TOCTOU), bypassing the filter.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL invalide.", None
    if parsed.scheme != 'https':
        return False, "L'URL doit être en https://.", None
    host = parsed.hostname or ''
    if not host:
        return False, "URL invalide (pas d'hôte).", None
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False, "Cet hôte n'est pas autorisé.", None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Nom d'hôte introuvable.", None
    pinned = None
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            return False, "Cette adresse pointe vers un réseau interne/privé, refusée.", None
        if pinned is None:
            pinned = ip_str
    if pinned is None:
        return False, "Nom d'hôte introuvable.", None
    return True, None, pinned


def validate_mcp_url(url):
    """Returns (ok, error_message_or_None). UX pre-check; the real connection
    goes through resolve_validated_mcp_ip() which pins the resolved IP."""
    ok, err, _ = resolve_validated_mcp_ip(url)
    return ok, err


class _PinnedIPHTTPSAdapter(requests.adapters.HTTPAdapter):
    """Forces the connection to an already validated IP, while keeping the
    original hostname for the SNI and the TLS certificate validation. This is
    what closes the DNS-rebinding window: the checked IP is exactly the one
    contacted, with no 2nd DNS resolution."""

    def __init__(self, pinned_ip, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        host = parsed.hostname or ''
        # SNI + cert validation on the real hostname despite connecting by IP.
        self.poolmanager.connection_pool_kw['server_hostname'] = host
        self.poolmanager.connection_pool_kw['assert_hostname'] = host
        ip = f"[{self._pinned_ip}]" if ':' in self._pinned_ip else self._pinned_ip
        netloc_ip = ip + (f":{parsed.port}" if parsed.port else '')
        request.headers['Host'] = parsed.netloc
        request.url = urlunparse(parsed._replace(netloc=netloc_ip))
        return super().send(request, **kwargs)


class MCPError(Exception):
    pass


class MCPClient:
    """Synchronous HTTP client for a remote MCP server (Streamable HTTP transport)."""

    # Short: a slow MCP server registered by a user can block a gunicorn thread
    # (few workers/threads) for the whole timeout duration, up to 4 times in the
    # support_chat() tool loop.
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

    def _post(self, **kwargs):
        """POST to the MCP server pinning the validated IP (anti-rebinding) and
        refusing redirects. Resolves+validates the host once only, right before
        connecting to that very IP."""
        ok, err, ip = resolve_validated_mcp_ip(self.url)
        if not ok:
            raise MCPError(err)
        session = requests.Session()
        session.mount('https://', _PinnedIPHTTPSAdapter(ip))
        try:
            return session.post(self.url, headers=self._headers(),
                                timeout=self.timeout, allow_redirects=False, **kwargs)
        finally:
            session.close()

    def _rpc(self, method, params=None):
        payload = {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': method,
                   'params': params or {}}
        # allow_redirects=False: requests follows redirects by default WITHOUT
        # revalidating the destination host — a server would pass the validation then
        # redirect to an internal IP and bypass it. We refuse every redirect rather
        # than follow it. The IP is also pinned (see _post) to close the
        # DNS-rebinding window.
        r = self._post(json=payload)
        # `Response.is_redirect` = presence of a `Location` AND status in
        # (301, 302, 303, 307, 308): the explicit test that followed replayed exactly
        # the same condition.
        if r.is_redirect:
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
        # Notification (no answer expected) — best-effort, some servers require it
        # before accepting tools/list. allow_redirects=False for the same reason as
        # in _rpc().
        try:
            self._post(json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
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


# process-local in-memory cache (not shared between gunicorn workers, which is
# acceptable: at worst one worker redoes a tools/list another already has
# cached — not a correctness issue, just a tiny latency economy).
_tools_cache = {}
_TOOLS_TTL = 120


def invalidate_tools(server_id):
    """Forgets a server's cached tools — to be called as soon as its URL, auth or
    filter changes, otherwise we would keep exposing the old configuration's
    tools to the model until the TTL expires."""
    _tools_cache.pop(server_id, None)


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
        # We cache the FAILURE too. Without it, an unreachable server would re-charge
        # its two timeouts (up to 10 s) on every chat message, since only success was
        # memorized: a few dead servers were enough to block a gunicorn thread for
        # minutes.
        _tools_cache[server_id] = (now, [])
        return []
    _tools_cache[server_id] = (now, tools)
    return tools
