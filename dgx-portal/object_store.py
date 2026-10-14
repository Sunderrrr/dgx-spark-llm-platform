"""S3-compatible object storage, configured in Admin → Config.

One client for everything that goes to the bucket: the generated files (when
Admin → Stockage puts them there, see media_store.py) and the backups (the
host's scripts/backup.sh hands its archive to backup_upload.py, so the
credentials never leave the portal).

Keys live under the configured prefix: `<prefix>/media/<kind>/<name>` and
`<prefix>/backups/<archive>`. Path-style addressing: Garage and MinIO expect
it, AWS accepts it.
"""
import threading
import time

from instance_config import s3_config

_lock = threading.Lock()
_cache = {'sig': None, 'client': None}


class NotConfigured(RuntimeError):
    """S3 is used while Admin → Config has no complete S3 settings."""


def configured():
    return s3_config()['enabled']


def client():
    """The boto3 client for the CURRENT settings (rebuilt when they change)."""
    c = s3_config()
    if not c['enabled']:
        raise NotConfigured("Stockage S3 non configuré (Admin → Config).")
    sig = (c['endpoint'], c['region'], c['access_key'], c['secret_key'])
    with _lock:
        if _cache['sig'] != sig:
            import boto3
            from botocore.config import Config
            _cache['client'] = boto3.client(
                's3', endpoint_url=c['endpoint'], region_name=c['region'] or 'us-east-1',
                aws_access_key_id=c['access_key'], aws_secret_access_key=c['secret_key'],
                config=Config(s3={'addressing_style': 'path'}, connect_timeout=5,
                              read_timeout=60, retries={'max_attempts': 3}))
            _cache['sig'] = sig
        return _cache['client']


def bucket():
    return s3_config()['bucket']


_MISSING = ('404', 'NoSuchKey', 'NotFound')
# Transient answers worth another try. Measured on the operator's Garage behind
# its proxy (2026-10-14): HEAD answered 403 — then 400 — on 8 of 30 calls in a
# burst, on objects that exist and that the same keys read fine a second
# later, and the nightly upload failed its verification on exactly that.
# botocore retries throttling and 5xx, never a 403.
_TRANSIENT_WAIT_S = (0.5, 1.5, 3)


def _call(fn, **kwargs):
    """Runs an S3 call; a missing object raises at once, a transient refusal
    is retried (4 attempts in ~5 s) before it is reported."""
    from botocore.exceptions import ClientError
    for wait in (*_TRANSIENT_WAIT_S, None):
        try:
            return fn(**kwargs)
        except ClientError as exc:
            err = exc.response.get('Error', {})
            status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if err.get('Code') in _MISSING or status == 404 or wait is None:
                raise
            if status not in (400, 403, 408, 429) and not (status or 0) >= 500:
                raise
            time.sleep(wait)


def key(*parts):
    """`<prefix>/<parts…>`, without empty segments."""
    prefix = s3_config()['prefix']
    return '/'.join(p.strip('/') for p in (prefix, *parts) if p and p.strip('/'))


def put_bytes(k, data, content_type='application/octet-stream'):
    _call(client().put_object, Bucket=bucket(), Key=k, Body=data, ContentType=content_type)


def put_stream(k, stream):
    """Uploads a non-seekable stream (multipart when large)."""
    client().upload_fileobj(stream, bucket(), k)


def head(k):
    """The object's size as {'ContentLength': n}, or None when it does not exist.

    Read with a ONE-BYTE ranged GET, not a HEAD: behind the operator's proxy
    HEAD answered 400/403 on a good share of the calls (a bare 400 on the
    first try, then 200), while GET never failed in the same measurements
    (2026-10-14). The image page probes up to four extensions, and each
    retried HEAD cost seconds — the image arrived late or as a 404. The
    answer's Content-Range carries the full size."""
    from botocore.exceptions import ClientError
    try:
        r = _call(client().get_object, Bucket=bucket(), Key=k, Range='bytes=0-0')
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code in _MISSING:
            return None
        if code == 'InvalidRange':                   # an empty object has no byte 0
            return {'ContentLength': 0}
        raise
    r['Body'].close()
    total = (r.get('ContentRange') or '').rsplit('/', 1)[-1]
    return {'ContentLength': int(total) if total.isdigit() else r['ContentLength']}


def get(k, byte_range=None):
    """get_object's answer (Body, ContentLength, ContentRange…), or None."""
    from botocore.exceptions import ClientError
    args = {'Bucket': bucket(), 'Key': k}
    if byte_range:
        args['Range'] = byte_range
    try:
        return _call(client().get_object, **args)
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise


def delete(k):
    _call(client().delete_object, Bucket=bucket(), Key=k)


def list_keys(prefix):
    """[(key, size, last_modified)] under `prefix`, every page."""
    out = []
    pages = client().get_paginator('list_objects_v2').paginate(Bucket=bucket(), Prefix=prefix.rstrip('/') + '/')
    for page in pages:
        for o in page.get('Contents', []) or []:
            out.append((o['Key'], o['Size'], o['LastModified']))
    return out


def test():
    """Admin → Config « Tester »: write, read back and delete a probe object."""
    if not configured():
        return False, "Endpoint, bucket et clés S3 sont requis."
    probe = key('.dgx-probe')
    try:
        put_bytes(probe, b'ok', 'text/plain')
        meta = head(probe)
        delete(probe)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"Le bucket refuse l'accès : {exc}"[:300]
    if not meta:
        return False, "Objet écrit mais introuvable à la relecture : vérifie le bucket."
    c = s3_config()
    return True, f"Bucket « {c['bucket']} » accessible en lecture et en écriture."
