"""LDAP/SSO accounts: their state and groups as the directory gives them.

The portal owns no « enabled » flag for a directory account: the Users list
showed « — ». directory.py asks the directory; these tests lock how its
answer is read, and that an unreachable directory is NEVER read as « every
account is gone » (missing data is not proof of absence).
"""
import threading
import time
import unittest
from unittest import mock

import directory


class _Conn:
    """ldap3 Connection stand-in: a paged search over fixed entries."""

    def __init__(self, entrees):
        self._entrees = entrees
        self.filtres = []
        conn = self

        class _Std:
            def paged_search(self, base, filtre, attributes=None, paged_size=None, generator=True):
                conn.filtres.append(filtre)
                for e in conn._entrees:
                    yield {'type': 'searchResEntry', 'attributes': e}

        class _Ext:
            standard = _Std()

        self.extend = _Ext()

    def unbind(self):
        pass


CONFIG = {'uri': 'ldap://annuaire.example:389', 'base': 'dc=example,dc=org',
          'users_dn': 'ou=users', 'login_attr': 'cn',
          'bind_dn': 'cn=svc,dc=example,dc=org', 'bind_pw': 'x'}


class EtatEntreeTest(unittest.TestCase):

    def test_authentik_ak_active(self):
        self.assertEqual(directory.etat_entree({'ak-active': ['TRUE']}), directory.ACTIF)
        self.assertEqual(directory.etat_entree({'ak-active': ['FALSE']}), directory.DESACTIVE)

    def test_active_directory_bit_desactive(self):
        self.assertEqual(directory.etat_entree({'userAccountControl': ['514']}), directory.DESACTIVE)
        self.assertEqual(directory.etat_entree({'userAccountControl': ['512']}), directory.ACTIF)

    def test_389_et_openldap(self):
        self.assertEqual(directory.etat_entree({'nsAccountLock': ['TRUE']}), directory.DESACTIVE)
        self.assertEqual(directory.etat_entree({'pwdAccountLockedTime': ['20261011000000Z']}),
                         directory.DESACTIVE)

    def test_sans_attribut_d_etat_le_compte_est_actif(self):
        self.assertEqual(directory.etat_entree({'cn': ['alice']}), directory.ACTIF)


class LireAnnuaireTest(unittest.TestCase):

    def test_tous_les_comptes_en_une_recherche(self):
        conn = _Conn([{'cn': ['Alice'], 'ak-active': ['TRUE']},
                      {'cn': ['bob'], 'ak-active': ['FALSE']}])
        with mock.patch.object(directory, 'ldap_config', return_value=CONFIG), \
             mock.patch.object(directory, '_connexion', return_value=conn):
            self.assertEqual(directory.lire_annuaire(), {'alice': 'actif', 'bob': 'desactive'})
        self.assertEqual(conn.filtres, ['(cn=*)'])

    def test_annuaire_injoignable(self):
        with mock.patch.object(directory, 'ldap_config', return_value=CONFIG), \
             mock.patch.object(directory, '_connexion', side_effect=OSError('down')):
            self.assertIsNone(directory.lire_annuaire())

    def test_annuaire_non_configure(self):
        with mock.patch.object(directory, 'ldap_config', return_value={**CONFIG, 'uri': ''}):
            self.assertIsNone(directory.lire_annuaire())


class EtatsAnnuaireTest(unittest.TestCase):
    """The request path never waits on the directory (a bind costs seconds)."""

    def setUp(self):
        self.addCleanup(directory._etat.update, t=0.0, etats=None, en_cours=False)

    def test_present_desactive_et_absent_depuis_le_cliche(self):
        directory._etat.update(t=time.time(), etats={'alice': 'actif', 'bob': 'desactive'},
                               en_cours=False)
        with mock.patch.object(directory, 'lire_annuaire') as lire:
            etats = directory.etats_annuaire(['Alice', 'bob', 'carol'])
        self.assertEqual(etats, {'Alice': 'actif', 'bob': 'desactive', 'carol': 'absent'})
        lire.assert_not_called()

    def test_sans_cliche_rien_n_est_devine_et_la_lecture_part_en_fond(self):
        fini = threading.Event()

        def lente():
            fini.wait(5)
            return {'alice': 'actif'}
        with mock.patch.object(directory, 'lire_annuaire', side_effect=lente):
            t0 = time.perf_counter()
            self.assertIsNone(directory.etats_annuaire(['alice']))
            self.assertLess(time.perf_counter() - t0, 0.5, "the request must not wait")
            fini.set()
            for _ in range(100):
                if not directory._etat['en_cours']:
                    break
                time.sleep(0.02)
        self.assertEqual(directory.etats_annuaire(['alice']), {'alice': 'actif'})

    def test_une_panne_garde_le_cliche_precedent(self):
        directory._etat.update(t=0.0, etats={'alice': 'actif'}, en_cours=False)
        with mock.patch.object(directory, 'lire_annuaire', return_value=None):
            directory.etats_annuaire(['alice'])
            for _ in range(100):
                if not directory._etat['en_cours']:
                    break
                time.sleep(0.02)
        self.assertEqual(directory.etats_annuaire(['alice']), {'alice': 'actif'},
                         "an outage does not make everyone absent")


class NomsDeGroupesTest(unittest.TestCase):

    def test_dn_et_noms_simples(self):
        self.assertEqual(
            directory.noms_de_groupes(['cn=platform-admins,ou=groups,dc=example,dc=org',
                                       'Famille', 'cn=Famille,ou=groups,dc=x', '']),
            ['platform-admins', 'Famille'])

    def test_un_dn_qui_n_est_pas_un_cn_est_ignore(self):
        self.assertEqual(directory.noms_de_groupes(['ou=groups,dc=x']), [])


if __name__ == '__main__':
    unittest.main()
