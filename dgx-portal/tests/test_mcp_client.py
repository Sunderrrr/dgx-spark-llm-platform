"""Garde-fous SSRF du client MCP.

C'est le point le plus sensible du portail : n'importe quel utilisateur
authentifié (pas seulement un admin) enregistre une URL que le backend ira
ensuite contacter, depuis le réseau docker qui héberge litellm
(LITELLM_MASTER_KEY), vllm-runner et postgres. Ces tests figent le
comportement de la liste noire.
"""

import ipaddress
import socket
import unittest
from unittest import mock

import mcp_client


def _fake_dns(ip):
    """Force la résolution DNS vers `ip`, sans toucher au réseau."""
    return mock.patch.object(
        socket, 'getaddrinfo',
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))])


class ValidateUrlTest(unittest.TestCase):
    def test_rejette_le_http_en_clair(self):
        ok, err = mcp_client.validate_mcp_url('http://exemple.com/mcp')
        self.assertFalse(ok)
        self.assertIn('https', err)

    def test_rejette_les_schemas_exotiques(self):
        for url in ('file:///etc/passwd', 'gopher://x/', 'ftp://x/'):
            self.assertFalse(mcp_client.validate_mcp_url(url)[0], url)

    def test_rejette_les_noms_de_services_internes(self):
        for host in ('litellm', 'postgres', 'vllm-runner', 'dgx-portal',
                     'host.docker.internal', 'localhost'):
            ok, _ = mcp_client.validate_mcp_url(f'https://{host}/mcp')
            self.assertFalse(ok, host)

    def test_rejette_les_ip_privees_et_speciales(self):
        # Un nom public qui résout vers une adresse interne doit être refusé :
        # c'est le cas d'attaque réel, pas seulement l'IP écrite en dur.
        for ip in ('127.0.0.1', '10.0.0.5', '192.168.1.10', '172.16.0.9',
                   '169.254.169.254',           # métadonnées cloud
                   '100.64.0.1',                # CGNAT
                   '0.0.0.0'):
            with _fake_dns(ip):
                ok, _ = mcp_client.validate_mcp_url('https://innocent.example/mcp')
            self.assertFalse(ok, ip)

    def test_accepte_une_adresse_publique(self):
        with _fake_dns('93.184.216.34'):
            ok, err = mcp_client.validate_mcp_url('https://exemple.com/mcp')
        self.assertTrue(ok, err)

    def test_hote_introuvable(self):
        with mock.patch.object(socket, 'getaddrinfo', side_effect=socket.gaierror):
            ok, _ = mcp_client.validate_mcp_url('https://nexiste-pas.example/mcp')
        self.assertFalse(ok)


class BlockedIpTest(unittest.TestCase):
    def test_classification(self):
        bloques = ['127.0.0.1', '::1', '10.0.0.1', '172.20.0.3', '192.168.0.1',
                   '169.254.1.1', 'fc00::1', '224.0.0.1', '100.127.255.254']
        permis = ['8.8.8.8', '93.184.216.34', '2606:4700:4700::1111']
        for ip in bloques:
            self.assertTrue(mcp_client._is_blocked_ip(ip), ip)
        for ip in permis:
            self.assertFalse(mcp_client._is_blocked_ip(ip), ip)

    def test_ip_illisible_est_bloquee(self):
        # En cas de doute on refuse, plutôt que de laisser passer.
        self.assertTrue(mcp_client._is_blocked_ip('pas-une-ip'))

    def test_ipv6_mappee_ipv4_interne(self):
        # ::ffff:169.254.169.254 contourne une liste noire naïve.
        self.assertTrue(mcp_client._is_blocked_ip('::ffff:169.254.169.254'))


class RedirectTest(unittest.TestCase):
    """Une redirection est refusée : `requests` la suivrait sans revalider la
    destination, ce qui annulerait complètement le filtre ci-dessus."""

    def test_refuse_une_redirection(self):
        client = mcp_client.MCPClient('https://exemple.com/mcp')
        reponse = mock.Mock(status_code=307, is_redirect=True, headers={})
        with _fake_dns('93.184.216.34'), mock.patch.object(mcp_client.requests, 'post',
                                                            return_value=reponse):
            with self.assertRaises(mcp_client.MCPError):
                client.list_tools()

    def test_ne_suit_jamais_les_redirections(self):
        client = mcp_client.MCPClient('https://exemple.com/mcp')
        reponse = mock.Mock(status_code=200, is_redirect=False,
                            headers={'Content-Type': 'application/json'})
        reponse.json.return_value = {'result': {'tools': []}}
        with _fake_dns('93.184.216.34'), mock.patch.object(
                mcp_client.requests, 'post', return_value=reponse) as post:
            client.list_tools()
        self.assertFalse(post.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()


class NegativeCacheTest(unittest.TestCase):
    """Un serveur injoignable doit être mémorisé comme tel. Sans ça, chaque
    message de chat repayait ses timeouts (jusqu'à 10 s), et quelques serveurs
    morts suffisaient à monopoliser les threads gunicorn."""

    def setUp(self):
        mcp_client._tools_cache.clear()

    def tearDown(self):
        mcp_client._tools_cache.clear()

    def test_l_echec_est_mis_en_cache(self):
        appels = []

        class ClientCasse:
            def __init__(self, *a, **kw):
                appels.append(1)

            def initialize(self):
                raise mcp_client.MCPError("injoignable")

            def list_tools(self):
                return []

        with mock.patch.object(mcp_client, 'MCPClient', ClientCasse):
            self.assertEqual(mcp_client.list_tools_cached(42, 'https://x.test/', None), [])
            self.assertEqual(mcp_client.list_tools_cached(42, 'https://x.test/', None), [])
        self.assertEqual(len(appels), 1, "le second appel aurait dû être servi par le cache")

    def test_invalidation_reessaie(self):
        class ClientCasse:
            def initialize(self):
                raise mcp_client.MCPError("injoignable")

            def list_tools(self):
                return []

        with mock.patch.object(mcp_client, 'MCPClient', lambda *a, **kw: ClientCasse()):
            mcp_client.list_tools_cached(7, 'https://x.test/', None)
        self.assertIn(7, mcp_client._tools_cache)
        mcp_client.invalidate_tools(7)
        self.assertNotIn(7, mcp_client._tools_cache)
