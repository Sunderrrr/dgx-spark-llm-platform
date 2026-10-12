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
        self.filters = []
        conn = self

        class _Std:
            def paged_search(self, base, filter_, attributes=None, paged_size=None, generator=True):
                conn.filters.append(filter_)
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


class EntryStateTest(unittest.TestCase):

    def test_authentik_ak_active(self):
        self.assertEqual(directory.entry_state({'ak-active': ['TRUE']}), directory.ACTIVE)
        self.assertEqual(directory.entry_state({'ak-active': ['FALSE']}), directory.DISABLED)

    def test_active_directory_disabled_bit(self):
        self.assertEqual(directory.entry_state({'userAccountControl': ['514']}), directory.DISABLED)
        self.assertEqual(directory.entry_state({'userAccountControl': ['512']}), directory.ACTIVE)

    def test_389_and_openldap(self):
        self.assertEqual(directory.entry_state({'nsAccountLock': ['TRUE']}), directory.DISABLED)
        self.assertEqual(directory.entry_state({'pwdAccountLockedTime': ['20261011000000Z']}),
                         directory.DISABLED)

    def test_without_state_attribute_account_is_active(self):
        self.assertEqual(directory.entry_state({'cn': ['alice']}), directory.ACTIVE)


class ReadDirectoryTest(unittest.TestCase):

    def test_all_accounts_in_one_search(self):
        conn = _Conn([{'cn': ['Alice'], 'ak-active': ['TRUE']},
                      {'cn': ['bob'], 'ak-active': ['FALSE']}])
        with mock.patch.object(directory, 'ldap_config', return_value=CONFIG), \
             mock.patch.object(directory, '_connection', return_value=conn):
            read_result = directory.read_directory()
        self.assertEqual(read_result['states'], {'alice': 'active', 'bob': 'disabled'})
        self.assertEqual(read_result['groups'], {'alice': [], 'bob': []})
        self.assertEqual(conn.filters, ['(cn=*)'])

    def test_directory_unreachable(self):
        with mock.patch.object(directory, 'ldap_config', return_value=CONFIG), \
             mock.patch.object(directory, '_connection', side_effect=OSError('down')):
            self.assertIsNone(directory.read_directory())

    def test_directory_not_configured(self):
        with mock.patch.object(directory, 'ldap_config', return_value={**CONFIG, 'uri': ''}):
            self.assertIsNone(directory.read_directory())


class DirectoryStatesTest(unittest.TestCase):
    """The request path never waits on the directory (a bind costs seconds)."""

    def setUp(self):
        self.addCleanup(directory._state.update, t=0.0, states=None, running=False)

    def test_present_disabled_and_absent_from_snapshot(self):
        directory._state.update(t=time.time(), states={'alice': 'active', 'bob': 'disabled'},
                               running=False)
        with mock.patch.object(directory, 'read_directory') as read:
            states = directory.directory_states_for(['Alice', 'bob', 'carol'])
        self.assertEqual(states, {'Alice': 'active', 'bob': 'disabled', 'carol': 'absent'})
        read.assert_not_called()

    def test_without_snapshot_nothing_guessed_and_read_runs_in_background(self):
        finished = threading.Event()

        def slow():
            finished.wait(5)
            return {'states': {'alice': 'active'}, 'groups': {}}
        with mock.patch.object(directory, 'read_directory', side_effect=slow):
            t0 = time.perf_counter()
            self.assertIsNone(directory.directory_states_for(['alice']))
            self.assertLess(time.perf_counter() - t0, 0.5, "the request must not wait")
            finished.set()
            for _ in range(100):
                if not directory._state['running']:
                    break
                time.sleep(0.02)
        self.assertEqual(directory.directory_states_for(['alice']), {'alice': 'active'})

    def test_outage_keeps_previous_snapshot(self):
        directory._state.update(t=0.0, states={'alice': 'active'}, running=False)
        with mock.patch.object(directory, 'read_directory', return_value=None):
            directory.directory_states_for(['alice'])
            for _ in range(100):
                if not directory._state['running']:
                    break
                time.sleep(0.02)
        self.assertEqual(directory.directory_states_for(['alice']), {'alice': 'active'},
                         "an outage does not make everyone absent")


class GroupNamesTest(unittest.TestCase):

    def test_dn_and_plain_names(self):
        self.assertEqual(
            directory.group_names(['cn=platform-admins,ou=groups,dc=example,dc=org',
                                       'Famille', 'cn=Famille,ou=groups,dc=x', '']),
            ['platform-admins', 'Famille'])

    def test_non_cn_dn_ignored(self):
        self.assertEqual(directory.group_names(['ou=groups,dc=x']), [])


if __name__ == '__main__':
    unittest.main()
