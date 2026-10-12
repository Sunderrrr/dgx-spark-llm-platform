"""SSRF guardrails of the MCP client.

This is the portal's most sensitive point: any authenticated user (not
only an admin) registers a URL the backend will then contact, from the
docker network hosting litellm (LITELLM_MASTER_KEY), vllm-runner and
postgres. These tests freeze the
behaviour of the blacklist.
"""

import socket
import unittest
from unittest import mock

import mcp_client


def _fake_dns(ip):
    """Forces the DNS resolution to `ip`, without touching the network."""
    return mock.patch.object(
        socket, 'getaddrinfo',
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))])


class ValidateUrlTest(unittest.TestCase):
    def test_rejects_plain_http(self):
        ok, err = mcp_client.validate_mcp_url('http://exemple.com/mcp')
        self.assertFalse(ok)
        self.assertIn('https', err)

    def test_rejects_exotic_schemes(self):
        for url in ('file:///etc/passwd', 'gopher://x/', 'ftp://x/'):
            self.assertFalse(mcp_client.validate_mcp_url(url)[0], url)

    def test_rejects_internal_service_names(self):
        for host in ('litellm', 'postgres', 'vllm-runner', 'dgx-portal',
                     'host.docker.internal', 'localhost'):
            ok, _ = mcp_client.validate_mcp_url(f'https://{host}/mcp')
            self.assertFalse(ok, host)

    def test_rejects_private_and_special_ips(self):
        # A public name resolving to an internal address must be refused: this is
        # the real attack case, not just a hardcoded IP.
        for ip in ('127.0.0.1', '10.0.0.5', '192.168.1.10', '172.16.0.9',
                   '169.254.169.254',           # cloud metadata
                   '100.64.0.1',                # CGNAT
                   '0.0.0.0'):
            with _fake_dns(ip):
                ok, _ = mcp_client.validate_mcp_url('https://innocent.example/mcp')
            self.assertFalse(ok, ip)

    def test_accepts_public_address(self):
        with _fake_dns('93.184.216.34'):
            ok, err = mcp_client.validate_mcp_url('https://exemple.com/mcp')
        self.assertTrue(ok, err)

    def test_unknown_host(self):
        with mock.patch.object(socket, 'getaddrinfo', side_effect=socket.gaierror):
            ok, _ = mcp_client.validate_mcp_url('https://nexiste-pas.example/mcp')
        self.assertFalse(ok)


class BlockedIpTest(unittest.TestCase):
    def test_classification(self):
        blocked = ['127.0.0.1', '::1', '10.0.0.1', '172.20.0.3', '192.168.0.1',
                   '169.254.1.1', 'fc00::1', '224.0.0.1', '100.127.255.254']
        allowed = ['8.8.8.8', '93.184.216.34', '2606:4700:4700::1111']
        for ip in blocked:
            self.assertTrue(mcp_client._is_blocked_ip(ip), ip)
        for ip in allowed:
            self.assertFalse(mcp_client._is_blocked_ip(ip), ip)

    def test_unreadable_ip_blocked(self):
        # In case of doubt we refuse, rather than let through.
        self.assertTrue(mcp_client._is_blocked_ip('pas-une-ip'))

    def test_ipv6_mappee_ipv4_interne(self):
        # ::ffff:169.254.169.254 bypasses a naive blacklist.
        self.assertTrue(mcp_client._is_blocked_ip('::ffff:169.254.169.254'))


class RedirectTest(unittest.TestCase):
    """A redirect is refused: `requests` would follow it without revalidating
    the destination, which would completely cancel the filter above."""

    def test_refuses_redirect(self):
        client = mcp_client.MCPClient('https://exemple.com/mcp')
        response = mock.Mock(status_code=307, is_redirect=True, headers={})
        sess = mock.Mock()
        sess.post.return_value = response
        with _fake_dns('93.184.216.34'), mock.patch.object(mcp_client.requests, 'Session',
                                                           return_value=sess):
            with self.assertRaises(mcp_client.MCPError):
                client.list_tools()

    def test_never_follows_redirects(self):
        client = mcp_client.MCPClient('https://exemple.com/mcp')
        response = mock.Mock(status_code=200, is_redirect=False,
                            headers={'Content-Type': 'application/json'})
        response.json.return_value = {'result': {'tools': []}}
        sess = mock.Mock()
        sess.post.return_value = response
        with _fake_dns('93.184.216.34'), mock.patch.object(
                mcp_client.requests, 'Session', return_value=sess):
            client.list_tools()
        self.assertFalse(sess.post.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()


class NegativeCacheTest(unittest.TestCase):
    """An unreachable server must be memorized as such. Without it, every chat
    message repaid its timeouts (up to 10 s), and a few dead servers were
    enough to monopolize the gunicorn threads."""

    def setUp(self):
        mcp_client._tools_cache.clear()

    def tearDown(self):
        mcp_client._tools_cache.clear()

    def test_failure_is_cached(self):
        calls = []

        class BrokenClient:
            def __init__(self, *a, **kw):
                calls.append(1)

            def initialize(self):
                raise mcp_client.MCPError("injoignable")

            def list_tools(self):
                return []

        with mock.patch.object(mcp_client, 'MCPClient', BrokenClient):
            self.assertEqual(mcp_client.list_tools_cached(42, 'https://x.test/', None), [])
            self.assertEqual(mcp_client.list_tools_cached(42, 'https://x.test/', None), [])
        self.assertEqual(len(calls), 1, "the second call should have been served by the cache")

    def test_invalidation_retries(self):
        class BrokenClient:
            def initialize(self):
                raise mcp_client.MCPError("injoignable")

            def list_tools(self):
                return []

        with mock.patch.object(mcp_client, 'MCPClient', lambda *a, **kw: BrokenClient()):
            mcp_client.list_tools_cached(7, 'https://x.test/', None)
        self.assertIn(7, mcp_client._tools_cache)
        mcp_client.invalidate_tools(7)
        self.assertNotIn(7, mcp_client._tools_cache)


class ResolvePinTest(unittest.TestCase):
    """The DNS resolution is done ONCE and the returned IP is the one pinned
    for the connection — this is what closes DNS-rebinding (TOCTOU)."""

    def test_returns_validated_public_ip(self):
        with _fake_dns('93.184.216.34'):
            ok, err, ip = mcp_client.resolve_validated_mcp_ip('https://exemple.com/mcp')
        self.assertTrue(ok, err)
        self.assertEqual(ip, '93.184.216.34')

    def test_refuses_private_ip_without_returning_ip(self):
        for ip_interne in ('127.0.0.1', '10.0.0.5', '169.254.169.254', '100.64.0.1'):
            with _fake_dns(ip_interne):
                ok, _, ip = mcp_client.resolve_validated_mcp_ip('https://innocent.example/mcp')
            self.assertFalse(ok, ip_interne)
            self.assertIsNone(ip, ip_interne)


class PinnedAdapterTest(unittest.TestCase):
    """The adapter connects to the validated IP but keeps the hostname for the
    SNI and the certificate validation (the cert stays checked)."""

    def test_pins_ip_and_keeps_hostname(self):
        adapter = mcp_client._PinnedIPHTTPSAdapter('93.184.216.34')
        prepared = mcp_client.requests.Request('POST', 'https://exemple.com:8443/mcp').prepare()
        capture = {}

        def fake_send(self_adapter, request, **kw):
            capture['url'] = request.url
            capture['host'] = request.headers.get('Host')
            capture['sni'] = adapter.poolmanager.connection_pool_kw.get('server_hostname')
            capture['assert'] = adapter.poolmanager.connection_pool_kw.get('assert_hostname')
            return mock.Mock(status_code=200)

        with mock.patch.object(mcp_client.requests.adapters.HTTPAdapter, 'send', fake_send):
            adapter.send(prepared)
        self.assertIn('93.184.216.34', capture['url'])
        self.assertNotIn('exemple.com', capture['url'])          # no more DNS re-resolution
        self.assertEqual(capture['host'], 'exemple.com:8443')    # Original host preserved
        self.assertEqual(capture['sni'], 'exemple.com')          # SNI = vrai hostname
        self.assertEqual(capture['assert'], 'exemple.com')       # cert checked on the hostname
