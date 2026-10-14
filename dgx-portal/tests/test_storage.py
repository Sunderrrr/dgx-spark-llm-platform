"""Storage: generated files on the volume or on S3, their migration, the
orphan sweep, the backups' upload and retention, and Admin → Stockage.

S3 is an in-memory fake with the boto3 calls the code uses: no network, and
the same answers (404 as ClientError, ranged reads, paginated listings).
"""
import io
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from botocore.exceptions import ClientError
from werkzeug.security import generate_password_hash

import app as portal
import backups
import instance_config
import media_store
import object_store

ADMIN = 'ztest-stockadm'
USER = 'ztest-stockusr'


class FakeBody:
    def __init__(self, data):
        self.data = data

    def close(self):
        pass

    def iter_chunks(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeS3:
    """The subset of the boto3 S3 client the portal calls."""

    def __init__(self):
        self.objects = {}                    # key -> (bytes, last_modified)
        self.fail_put = False

    def _missing(self):
        return ClientError({'Error': {'Code': '404'}}, 'HeadObject')

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if self.fail_put:
            raise ClientError({'Error': {'Code': '503'}}, 'PutObject')
        self.objects[Key] = (bytes(Body), datetime.now(timezone.utc))

    def upload_fileobj(self, stream, Bucket, Key):
        self.put_object(Bucket, Key, stream.read())

    def upload_file(self, path, Bucket, Key, ExtraArgs=None):
        with open(path, 'rb') as f:
            self.put_object(Bucket, Key, f.read())

    def download_fileobj(self, Bucket, Key, out):
        out.write(self.objects[Key][0])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing()
        return {'ContentLength': len(self.objects[Key][0])}

    def get_object(self, Bucket, Key, Range=None):
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        data = self.objects[Key][0]
        if Range:
            a, b = Range[len('bytes='):].split('-')
            a = int(a or 0)
            b = int(b) if b else len(data) - 1
            part = data[a:b + 1]
            return {'Body': FakeBody(part), 'ContentLength': len(part),
                    'ContentRange': f'bytes {a}-{a + len(part) - 1}/{len(data)}'}
        return {'Body': FakeBody(data), 'ContentLength': len(data)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        fake = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {'Contents': [{'Key': k, 'Size': len(v[0]), 'LastModified': v[1]}
                                    for k, v in sorted(fake.objects.items()) if k.startswith(Prefix)]}
        return Pages()


class StorageBase(unittest.TestCase):

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.root = tempfile.mkdtemp()
        self.s3 = FakeS3()
        self.configured = True
        for target, value in ((media_store, 'ROOT'), (media_store, 'STATUS_FILE'), (media_store, 'LOCK_FILE')):
            p = patch.object(target, value, {'ROOT': self.root,
                                             'STATUS_FILE': os.path.join(self.root, '.st.json'),
                                             'LOCK_FILE': os.path.join(self.root, '.lock')}[value])
            p.start()
            self.addCleanup(p.stop)
        for name, fn in (('client', lambda: self.s3), ('configured', lambda: self.configured),
                         ('bucket', lambda: 'tb')):
            p = patch.object(object_store, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self._clean)
        self._clean()

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM settings WHERE key IN ('media_storage','backup_destination',"
                       "'backup_keep_local','backup_keep_s3')")
            db.execute("DELETE FROM image_jobs WHERE username LIKE 'ztest-%'")
            db.execute("DELETE FROM voice_jobs WHERE username LIKE 'ztest-%'")
            db.execute("DELETE FROM local_users WHERE username LIKE 'ztest-%'")
            db.commit()

    def _setting(self, key, value):
        with portal.app.app_context():
            portal.set_setting(key, value)

    def _local(self, kind, name, data=b'x', age_days=0):
        os.makedirs(os.path.join(self.root, kind), exist_ok=True)
        path = os.path.join(self.root, kind, name)
        with open(path, 'wb') as f:
            f.write(data)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(path, (t, t))
        return path


class MediaStoreTest(StorageBase):

    def test_local_by_default_and_served(self):
        with portal.app.test_request_context('/'):
            self.assertEqual(media_store.save('voice_audio', 'a1.mp3', b'audio'), 'local')
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'voice_audio', 'a1.mp3')))
            r = media_store.serve('voice_audio', 'a1.mp3', 'audio/mpeg')
            r.direct_passthrough = False
            self.assertEqual(r.get_data(), b'audio')
            r.close()
        self.assertEqual(self.s3.objects, {})

    def test_s3_when_chosen_and_read_back_with_ranges(self):
        self._setting('media_storage', 's3')
        with portal.app.test_request_context('/'):
            self.assertEqual(media_store.save('video_files', 'v1.mp4', b'0123456789', 'video/mp4'), 's3')
        self.assertIn('media/video_files/v1.mp4', self.s3.objects)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'video_files', 'v1.mp4')))
        self.assertTrue(media_store.exists('video_files', 'v1.mp4'))
        with portal.app.test_request_context('/', headers={'Range': 'bytes=2-5'}):
            r = media_store.serve('video_files', 'v1.mp4', 'video/mp4')
            self.assertEqual(r.status_code, 206)
            self.assertEqual(r.headers['Content-Range'], 'bytes 2-5/10')
            self.assertEqual(b''.join(r.response), b'2345')
        media_store.delete('video_files', 'v1.mp4')
        self.assertEqual(self.s3.objects, {})

    def test_s3_chosen_but_not_configured_stays_local(self):
        self._setting('media_storage', 's3')
        self.configured = False
        self.assertEqual(media_store.location(), 'local')

    def test_s3_failure_keeps_the_file_locally(self):
        self._setting('media_storage', 's3')
        self.s3.fail_put = True
        self.assertEqual(media_store.save('ocr_images', 'o1.png', b'png'), 'local')
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'ocr_images', 'o1.png')))

    def test_names_cannot_leave_their_folder(self):
        for bad in ('../portal.db', 'a/b.png', '', '.hidden'):
            with self.assertRaises(ValueError):
                media_store.local_path('image_files', bad)
        with self.assertRaises(ValueError):
            media_store.local_path('../etc', 'x.png')

    def _wait_migration(self):
        for _ in range(100):
            if not media_store.migration_status().get('running'):
                return media_store.migration_status()
            time.sleep(0.05)
        self.fail('migration did not finish')

    def test_migration_both_ways(self):
        self._local('image_files', 'p1_0.png', b'img')
        self._local('voice_audio', 'a1.mp3', b'snd')
        ok, err = media_store.start_migration('s3')
        self.assertTrue(ok, err)
        st = self._wait_migration()
        self.assertEqual((st['total'], st['done'], st['failed']), (2, 2, 0))
        self.assertEqual(sorted(self.s3.objects), ['media/image_files/p1_0.png', 'media/voice_audio/a1.mp3'])
        self.assertFalse(os.listdir(os.path.join(self.root, 'image_files')))
        ok, err = media_store.start_migration('local')
        self.assertTrue(ok, err)
        st = self._wait_migration()
        self.assertEqual((st['done'], st['failed']), (2, 0))
        self.assertEqual(self.s3.objects, {})
        with open(os.path.join(self.root, 'image_files', 'p1_0.png'), 'rb') as f:
            self.assertEqual(f.read(), b'img')

    def test_one_migration_at_a_time(self):
        import fcntl
        fd = os.open(media_store.LOCK_FILE, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            ok, err = media_store.start_migration('s3')
            self.assertFalse(ok)
            self.assertIn('déjà', err)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_migration_needs_s3(self):
        self.configured = False
        ok, err = media_store.start_migration('s3')
        self.assertFalse(ok)
        self.assertIn('S3', err)

    def test_sweep_removes_old_orphans_only(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, created_at) "
                       "VALUES (?, 'keepme', 'p', 'done', ?)", (USER, datetime.now().isoformat()))
            db.commit()
        kept = self._local('image_files', 'keepme_0.png', age_days=30)
        orphan = self._local('image_files', 'goneid_1.webp', age_days=30)
        recent = self._local('image_files', 'newid_0.png')
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.s3.objects['media/voice_audio/zzz.mp3'] = (b'a', old)
        out = media_store.sweep(db_path=os.path.join(os.path.dirname(media_store.DB_PATH), 'portal.db'))
        self.assertEqual((out['local'], out['s3']), (1, 1))
        self.assertTrue(os.path.exists(kept))
        self.assertTrue(os.path.exists(recent), "a file younger than the grace period must stay")
        self.assertFalse(os.path.exists(orphan))
        self.assertEqual(self.s3.objects, {})

    def test_image_route_serves_from_s3(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, created_at) "
                       "VALUES (?, 'abc123', 'p', 'done', ?)", (USER, datetime.now().isoformat()))
            db.commit()
        self.s3.objects['media/image_files/abc123_0.webp'] = (b'webp-bytes', datetime.now(timezone.utc))
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = USER
            s['auth_at'] = int(time.time())
        r = c.get('/image/file/abc123/0')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'image/webp')
        self.assertEqual(r.data, b'webp-bytes')
        self.assertEqual(c.get('/image/file/abc123/1').status_code, 404)


class TransientS3Test(StorageBase):
    """A burst of refusals on objects that exist must not fail a backup."""

    def test_transient_refusal_is_retried_missing_is_not(self):
        self.s3.objects['backups/a.tar.gz'] = (b'abc', datetime.now(timezone.utc))
        real = self.s3.get_object
        calls = []

        def flaky(**kw):
            calls.append(kw['Key'])
            if len(calls) <= 2:
                raise ClientError({'Error': {'Code': '403'},
                                   'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject')
            return real(**kw)
        with patch.object(self.s3, 'get_object', flaky), \
             patch.object(object_store, '_TRANSIENT_WAIT_S', (0, 0, 0)):
            self.assertEqual(object_store.head('backups/a.tar.gz')['ContentLength'], 3)
            self.assertEqual(len(calls), 3)
            calls.clear()
            self.assertIsNone(object_store.head('backups/missing.tar.gz'))
            # 2 refusals then the real 404: a missing object is not hammered
            self.assertEqual(len(calls), 3)

    def test_existence_never_uses_head(self):
        """HEAD is unreliable behind the proxy: existence and size come from
        a one-byte ranged GET."""
        self.s3.objects['media/image_files/x_0.png'] = (b'0123456789', datetime.now(timezone.utc))

        def no_head(**kw):
            raise AssertionError("HEAD must not be used")
        with patch.object(self.s3, 'head_object', no_head):
            self.assertEqual(object_store.head('media/image_files/x_0.png')['ContentLength'], 10)
            self.assertIsNone(object_store.head('media/image_files/nope.png'))
            self.assertTrue(media_store.exists('image_files', 'x_0.png'))

    def test_persistent_refusal_is_reported(self):
        def denied(**kw):
            raise ClientError({'Error': {'Code': 'AccessDenied'},
                               'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject')
        with patch.object(self.s3, 'get_object', denied), \
             patch.object(object_store, '_TRANSIENT_WAIT_S', (0, 0, 0)):
            with self.assertRaises(ClientError):
                object_store.head('backups/a.tar.gz')


class BackupsTest(StorageBase):

    def test_settings_defaults_and_bounds(self):
        cfg = backups.settings()
        self.assertEqual((cfg['destination'], cfg['keep_local'], cfg['keep_s3']), ('local', 7, 7))
        self._setting('backup_keep_s3', 99999)
        self._setting('backup_destination', 'nowhere')
        cfg = backups.settings()
        self.assertEqual((cfg['destination'], cfg['keep_s3']), ('local', backups.MAX_KEEP))

    def test_upload_verifies_and_prunes(self):
        for day in range(1, 6):
            self.s3.objects[f'backups/dgx-2026-10-0{day}_0300.tar.gz'] = (b'old', datetime.now(timezone.utc))
        out = backups.upload('dgx-2026-10-14_0300.tar.gz', 3, io.BytesIO(b'archive'))
        self.assertEqual(out['size'], 7)
        self.assertEqual(sorted(self.s3.objects), ['backups/dgx-2026-10-04_0300.tar.gz',
                                                   'backups/dgx-2026-10-05_0300.tar.gz',
                                                   'backups/dgx-2026-10-14_0300.tar.gz'])
        self.assertEqual(len(out['purged']), 3)

    def test_upload_refuses_odd_names(self):
        for bad in ('../x.tar.gz', 'dump.sql'):
            with self.assertRaises(ValueError):
                backups.upload(bad, 3, io.BytesIO(b'x'))


class AdminStorageTest(StorageBase):

    def setUp(self):
        super().setUp()
        with portal.app.app_context():
            db = portal.get_db()
            for name, adm in ((ADMIN, 1), (USER, 0)):
                db.execute("INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                           "enabled, created_at) VALUES (?,?,?,?,1,?)",
                           (name, generate_password_hash('x' * 12), name, adm, time.time()))
            db.commit()

    def _client(self, username, is_admin):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['is_admin'] = is_admin
        return c

    def _post(self, c, url, data):
        return c.post(url, data=data, headers={'X-CSRFToken': 'tok'})

    def test_admin_only(self):
        c = self._client(USER, False)
        # A non-admin GET is redirected (like every admin page), never served.
        self.assertIn(c.get('/admin/storage').status_code, (302, 403))
        for url, data in (('/admin/storage', {'media_storage': 's3'}),
                          ('/admin/storage/migrate', {'target': 's3'})):
            self.assertIn(self._post(c, url, data).status_code, (302, 403), url)
        self.assertEqual(media_store.location(), 'local', "a refused POST changed nothing")
        self.assertFalse(media_store.migration_status().get('running'))

    def test_state_and_save(self):
        c = self._client(ADMIN, True)
        d = c.get('/admin/storage').get_json()
        self.assertTrue(d['ok'])
        self.assertEqual(set(d['media_usage']['local']), set(media_store.KINDS))
        r = self._post(c, '/admin/storage', {'media_storage': 's3', 'backup_destination': 'both',
                                             'backup_keep_local': '3', 'backup_keep_s3': '30'})
        self.assertEqual(r.status_code, 200, r.get_json())
        cfg = backups.settings()
        self.assertEqual((cfg['destination'], cfg['keep_local'], cfg['keep_s3']), ('both', 3, 30))
        self.assertEqual(media_store.location(), 's3')

    def test_s3_choices_need_s3(self):
        self.configured = False
        c = self._client(ADMIN, True)
        for field, value in (('media_storage', 's3'), ('backup_destination', 's3'),
                             ('backup_destination', 'both')):
            r = self._post(c, '/admin/storage', {field: value})
            self.assertEqual(r.status_code, 400, (field, value))
        self.assertEqual(self._post(c, '/admin/storage', {'backup_destination': 'local'}).status_code, 200)

    def test_retention_is_validated(self):
        c = self._client(ADMIN, True)
        for bad in ('0', '-1', str(backups.MAX_KEEP + 1), 'beaucoup'):
            self.assertEqual(self._post(c, '/admin/storage', {'backup_keep_local': bad}).status_code, 400, bad)

    def test_config_test_writes_and_reads_a_probe(self):
        c = self._client(ADMIN, True)
        with patch.object(instance_config, 's3_config', return_value={
                'endpoint': 'https://s3.example.org', 'region': 'us-east-1', 'bucket': 'tb', 'prefix': '',
                'access_key': 'a', 'secret_key': 's', 'enabled': True}):
            d = self._post(c, '/admin/config/test/s3', {}).get_json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(self.s3.objects, {}, "the probe object must be removed")

    def test_s3_settings_are_complete_or_disabled(self):
        with portal.app.app_context():
            for k in ('s3_endpoint', 's3_bucket', 's3_access_key'):
                instance_config.set_value(k, 'v')
            try:
                self.assertFalse(instance_config.s3_config()['enabled'])
                instance_config.set_value('s3_secret_key', 'v')
                self.assertTrue(instance_config.s3_config()['enabled'])
                self.assertEqual(next(r for r in instance_config.config_state()
                                      if r['name'] == 's3_secret_key')['value'], '',
                                 "a secret is never read back")
            finally:
                for k in ('s3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key'):
                    instance_config.set_value(k, instance_config.CLEAR)


if __name__ == '__main__':
    unittest.main()
