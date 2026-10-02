"""WebAuthn / passkeys — 2nd factor by security key (YubiKey, 1Password key,
OS passkey), not TOTP.

Enabled per user from the settings (gear): each account registers a
passkey and can require its presence at login (local/LDAP). Challenges are
one-time, stored in database and bounded in time.

Very deliberate scope: the 2nd factor applies to local and LDAP logins
(product choice). SSO/Authentik is NOT given the step-up, and the passkey
is bound to the origin (WEBAUTHN_ORIGIN) — a key registered on the public
domain does not work from another origin (e.g. the LAN).

Depends on the core (db, auth, config) only via get_db / _apply_session /
config: never the `app` object, to stay importable without a cycle.
"""
import base64
import hashlib
import json
import secrets
import time

from flask import Blueprint, jsonify, request, session

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from auth import (_apply_session, _login_fail, _login_locked, _login_reset,
                   est_bloque, login_required)
from config import WEBAUTHN_ORIGIN, WEBAUTHN_REQUIRE_UV, WEBAUTHN_RP_ID, WEBAUTHN_RP_NAME
from db import get_db, log_audit
from local_users import (GESTION_LDAP, GESTION_PORTAIL, GESTION_SSO, _local_user_auth,
                          gestion_mot_de_passe, passkey_possible)

bp = Blueprint("webauthn", __name__)

WEBAUTHN_PENDING_TTL = 5 * 60  # seconds — a challenge only lives 5 min
_UV = (UserVerificationRequirement.REQUIRED if WEBAUTHN_REQUIRE_UV
       else UserVerificationRequirement.PREFERRED)


# ── base64url helpers ───────────────────────────────────────────────────────
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    # Base64url (optional padding) -> bytes. The missing pad is restored.
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── Persistence ─────────────────────────────────────────────────────────────
def _webauthn_enabled(username: str) -> bool:
    """Does the account require the passkey at login (local/LDAP)?"""
    row = get_db().execute(
        "SELECT enabled FROM user_security WHERE username=?", (username,)).fetchone()
    return bool(row and row["enabled"])


def _set_enabled(username: str, enabled: bool) -> None:
    now = time.time()
    get_db().execute(
        "INSERT INTO user_security (username, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (username, 1 if enabled else 0, now, now))
    get_db().commit()


def _stored_credentials(username: str):
    return get_db().execute(
        "SELECT id, credential_id, sign_count, transports, label, created_at "
        "FROM webauthn_credentials WHERE username=? ORDER BY created_at DESC",
        (username,)).fetchall()


def _credential_row(username: str, credential_id_b64: str):
    return get_db().execute(
        "SELECT public_key, sign_count FROM webauthn_credentials "
        "WHERE username=? AND credential_id=?",
        (username, credential_id_b64)).fetchone()


def _pending_insert(nonce, username, kind, challenge, fullname=None, is_admin=None, source=None):
    db = get_db()
    now = time.time()
    # We keep only ONE challenge in flight per (account, type): a new one
    # invalidates the old one (anti-replay, and bounds the table).
    db.execute("DELETE FROM pending_webauthn WHERE username=? AND kind=?", (username, kind))
    db.execute(
        "INSERT INTO pending_webauthn (nonce, username, kind, challenge, fullname, is_admin, source, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (nonce, username, kind, challenge, fullname, is_admin, source, now, now + WEBAUTHN_PENDING_TTL))
    db.commit()


def _pending_get(nonce, kind=None):
    # `nonce` comes from a JSON body whose TYPE is not guaranteed: a list or
    # an object raised a sqlite3.ProgrammingError as a 500 on a PUBLIC route
    # (audit of 2026-10-02). Treating a non-string nonce as absent is exactly
    # what the flow already knows how to do (challenge not found/expired).
    if not isinstance(nonce, str) or not nonce:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM pending_webauthn WHERE nonce=?", (nonce,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < time.time():
        db.execute("DELETE FROM pending_webauthn WHERE nonce=?", (nonce,))
        db.commit()
        return None
    if kind and row["kind"] != kind:
        return None
    return row


def _pending_clear(nonce) -> None:
    if not isinstance(nonce, str) or not nonce:
        return
    db = get_db()
    db.execute("DELETE FROM pending_webauthn WHERE nonce=?", (nonce,))
    db.commit()


def _verify_password(username: str, password: str, gestion: str) -> bool:
    # `gestion` is PROVIDED by the caller: `_verify_password_locked` already
    # computed it (two SELECTs) for its own SSO test, and recomputing it here
    # doubled those reads at every sensitive re-verification.
    """Password re-verification, against the source that HOLDS it.

    `login()` tries local THEN the directory, and that order is deliberate
    there: a directory account with a leftover local row must be able to log
    in with its directory password. Here the question is another — we
    confirm a sensitive action — and `gestion_mot_de_passe` already tells who rules.

    Querying the directory for an account whose password the PORTAL holds
    was not merely useless: measured on 2026-09-17 on
    `/api/security/register/begin`, a wrong password cost **13 s** of
    gunicorn worker when the directory is mute — and the refusal is
    precisely the FREQUENT case. With four workers, four simultaneous
    typos thus immobilized the portal. A `portail` account no longer
    consults the directory, an `annuaire` account no longer consults the
    local one, and `inconnu` (nothing is authoritative) keeps `login()`'s order.
    """
    if gestion == GESTION_PORTAIL:
        return _local_user_auth(username, password)[0]
    from auth import ldap_authenticate
    if gestion == GESTION_LDAP:
        return ldap_authenticate(username, password)[0]
    if _local_user_auth(username, password)[0]:
        return True
    return ldap_authenticate(username, password)[0]


def _verify_password_locked(username: str, password: str):
    """Re-verification PROTECTED by the login lock — (ok, error_response).

    Without this guard, a hijacked session (cookie + CSRF token live in the
    same session) allowed trying passwords at network speed via
    /api/security/*: the /login counter was never incremented, so the 6
    failures / 15 min lock never triggered, and a found password gave the
    account's REUSABLE password (plus the possibility to unregister its
    passkeys).

    The counter is the ACCOUNT's (`user:<nom>`), shared with /login: the
    attempts made from the Settings also lock the login page, and vice
    versa. Failures are thus bounded globally, whatever the path
    taken.
    """
    ukey = f"user:{username}"
    # SSO account: no password the portal can check. Saying it BEFORE the
    # failure counter is deliberate: otherwise an SSO user trying their
    # identity password (the only one they have) accumulates failures on
    # `user:<nom>` — the counter being shared with /login, they ended up
    # locking themselves out of the login page, for an action impossible
    # from the start.
    gestion = gestion_mot_de_passe(username)['gestion']
    if gestion == GESTION_SSO:
        return False, ({"error": "Compte SSO : aucun mot de passe n'est géré par le "
                                 "portail, la vérification est impossible."}, 400)
    wait = _login_locked(ukey)
    if wait:
        return False, ({"error": f"Trop de tentatives. Réessaie dans {wait // 60 + 1} min."}, 429)
    if _verify_password(username, password, gestion):
        _login_reset(ukey)
        return True, None
    _login_fail(ukey)
    # 400 and NOT 401: the session is perfectly valid, it is the
    # CONFIRMATION that is wrong. The client interprets a 401 as « session
    # expirée » and sends the user back to /login (`authFetch`): mistyping the
    # password while adding a key thus LOGGED THEM OUT, instead of showing
    # « Mot de passe incorrect. » — observed in browser testing.
    return False, ({"error": "Mot de passe incorrect."}, 400)


# ── Registration flow (adding a key) ───────────────────────────────────────
def start_registration(username: str):
    existing = _stored_credentials(username)
    exclude = [
        PublicKeyCredentialDescriptor(id=_b64d(c["credential_id"]),
                                      type=PublicKeyCredentialType.PUBLIC_KEY)
        for c in existing
    ]
    options = generate_registration_options(
        rp_id=WEBAUTHN_RP_ID,
        rp_name=WEBAUTHN_RP_NAME,
        user_name=username,
        # user_id must be stable and unique per account: hash of the username.
        user_id=hashlib.sha256(username.encode()).digest(),
        timeout=60000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=_UV),
        exclude_credentials=exclude,
    )
    nonce = secrets.token_urlsafe(24)
    _pending_insert(nonce, username, "register", options.challenge)
    return {"publicKey": json.loads(options_to_json(options)), "nonce": nonce}


def finish_registration(username: str, credential, nonce: str, label: str):
    pend = _pending_get(nonce, "register")
    if not pend:
        return {"error": "Demande d'enregistrement expirée ou invalide."}, 400
    if pend["username"] != username:
        return {"error": "Compte incohérent."}, 400
    try:
        ver = verify_registration_response(
            credential=credential,
            expected_challenge=pend["challenge"],
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=WEBAUTHN_ORIGIN,
            require_user_presence=True,
            require_user_verification=WEBAUTHN_REQUIRE_UV,
        )
    except Exception:
        _pending_clear(nonce)
        return {"error": "Clé refusée : la vérification a échoué."}, 400

    cred_id = _b64url(ver.credential_id)
    # `transports` is not returned by the lib: it comes from the client's
    # response (Navigator.credentials.create → response.transports, when available).
    transports_arr = (credential.get("response") or {}).get("transports") or []
    transports = json.dumps(transports_arr)
    db = get_db()
    # The DELETE is SCOPED per account (audit of 2026-10-02): `credential_id`
    # is globally unique, and a client-supplied identifier was enough to take
    # away ANOTHER account's row. The victim lost more than their key:
    # `user_security.enabled` stayed at 1 with no registered key, so every
    # login ended in « aucune passkey » — a definitive lockout until admin
    # intervention. The two other accesses to this table were already scoped;
    # this one was the only one that was not.
    db.execute("DELETE FROM webauthn_credentials WHERE username=? AND credential_id=?",
               (username, cred_id))
    db.execute(
        "INSERT INTO webauthn_credentials "
        "(username, credential_id, public_key, sign_count, transports, label, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (username, cred_id, ver.credential_public_key, ver.sign_count,
         transports, label or "Clé de sécurité", time.time()))
    _set_enabled(username, True)  # register the 1st key => 2FA enabled
    _pending_clear(nonce)
    db.commit()
    return {"ok": True}


# ── Authentication flow (login step 2) ─────────────────────────────────────
def start_login(username: str, fullname: str, is_admin: bool, source: str):
    """Generates the challenge for the 2nd step (after a valid password / LDAP)."""
    creds = _stored_credentials(username)
    # transports is a mere browser-side hint; we deliberately omit it (the
    # lib requires enums and not strings, and the hint is optional).
    allow = [
        PublicKeyCredentialDescriptor(id=_b64d(c["credential_id"]),
                                      type=PublicKeyCredentialType.PUBLIC_KEY)
        for c in creds
    ]
    options = generate_authentication_options(
        rp_id=WEBAUTHN_RP_ID,
        timeout=60000,
        allow_credentials=allow,
        user_verification=_UV,
    )
    nonce = secrets.token_urlsafe(24)
    _pending_insert(nonce, username, "login", options.challenge,
                    fullname, 1 if is_admin else 0, source)
    return {"publicKey": json.loads(options_to_json(options)), "nonce": nonce}


def finish_login(nonce: str, credential):
    pend = _pending_get(nonce, "login")
    if not pend:
        return {"error": "Demande de connexion expirée ou invalide."}, 400
    username = pend["username"]
    # id of the key as verified (we re-derive it from the response).
    try:
        cred_id = _b64url(base64.urlsafe_b64decode(credential.get("id", "") + "=="))
    except Exception:
        return {"error": "Clé invalide."}, 400
    row = _credential_row(username, cred_id)
    if not row:
        # The challenge is SINGLE-USE: the two other exits consume it, not this
        # one — it thus stayed valid until its TTL (5 min) after a first failed
        # attempt. No replay possible for all that (`sign_count` checked and
        # updated), but an open challenge must not survive the failure that used
        # it.
        _pending_clear(nonce)
        return {"error": "Clé inconnue pour ce compte."}, 401
    try:
        ver = verify_authentication_response(
            credential=credential,
            expected_challenge=pend["challenge"],
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=WEBAUTHN_ORIGIN,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=WEBAUTHN_REQUIRE_UV,
        )
    except Exception:
        _pending_clear(nonce)
        return {"error": "Vérification de la clé échouée."}, 401
    # A block placed DURING the few minutes of the challenge must fail the
    # second step: the passkey proves identity, it is not an authorization.
    # The app.py check happened before the challenge, this one closes the
    # window between the two.
    if est_bloque(username):
        _pending_clear(nonce)
        log_audit(username, 'login.refuse', 'compte bloqué pendant le défi passkey')
        return {"error": "Accès révoqué pour ce compte."}, 403
    db = get_db()
    db.execute("UPDATE webauthn_credentials SET sign_count=? WHERE username=? AND credential_id=?",
               (ver.new_sign_count, username, cred_id))
    _pending_clear(nonce)
    db.commit()
    _apply_session(username, pend["fullname"] or username, bool(pend["is_admin"]), via_sso=False)
    return {"ok": True}


# ── Routes ────────────────────────────────────────────────────────────────────
@bp.route("/api/security")
@login_required
def api_security():
    username = session["username"]
    creds = _stored_credentials(username)
    gestion = gestion_mot_de_passe(username)
    return jsonify({
        "enabled": _webauthn_enabled(username),
        # The UI must be able to SAY where the password comes from, and thus
        # whether a change form (or adding a passkey) makes sense here.
        # A single read of the source (two SELECTs) for both fields:
        # `passkey_possible` merely re-reads `gestion_mot_de_passe`.
        "password_managed_by": gestion["gestion"],
        "passkey_possible": gestion["passkey"],
        "credentials": [{
            "id": c["id"],
            "credential_id": c["credential_id"],
            "label": c["label"],
            "created_at": c["created_at"],
        } for c in creds],
    })


@bp.route("/api/security/register/begin", methods=["POST"])
@login_required
def security_register_begin():
    # Password re-verification BEFORE creating anything.
    #
    # Without it, a simple stolen cookie was enough to register THEIR key:
    # the challenge is obtained with the cookie alone (`/api/csrf` is public
    # and returns the current session's token), `finish_registration` then
    # enables 2FA (`_set_enabled(username, True)`) with the thief's key as the
    # only one, and the victim ends up in an account where their correct
    # password no longer suffices and where they cannot produce any assertion
    # — recovery by an admin only, and destructive. `security_remove` and
    # `security_toggle` already required this password; adding a key did not,
    # while it is the operation that CREATES the lockout condition.
    #
    # Deliberate side effect: the « local + LDAP » scope of 2FA, until now
    # only displayed by the UI (`passkey_possible`), is finally enforced by
    # the server — an SSO account receives 400 « Compte SSO ».
    data = request.get_json(silent=True) or {}
    motdepasse = data.get("password") or ""
    if not motdepasse:
        # Missing field = incomplete request, not a wrong password: answering
        # 401 here would consume a try of the lock shared with /login for a
        # request that attempted nothing.
        return jsonify({"error": "Mot de passe requis pour ajouter une clé."}), 400
    ok, err = _verify_password_locked(session["username"], motdepasse)
    if not ok:
        return jsonify(err[0]), err[1]
    return jsonify(start_registration(session["username"]))


@bp.route("/api/security/register/finish", methods=["POST"])
@login_required
def security_register_finish():
    data = request.get_json(silent=True) or {}
    credential = data.get("credential")
    nonce = data.get("nonce")
    label = data.get("label", "")
    if not credential or not nonce:
        return jsonify({"error": "Réponse de clé manquante."}), 400
    res = finish_registration(session["username"], credential, nonce, label)
    if isinstance(res, tuple):
        return jsonify(res[0]), res[1]
    return jsonify(res)


@bp.route("/api/security/remove", methods=["POST"])
@login_required
def security_remove():
    username = session["username"]
    data = request.get_json(silent=True) or {}
    cred_id = data.get("credential_id")
    password = data.get("password", "")
    if not cred_id or not password:
        return jsonify({"error": "Champs manquants."}), 400
    ok, err = _verify_password_locked(username, password)
    if not ok:
        return jsonify(err[0]), err[1]
    db = get_db()
    db.execute("DELETE FROM webauthn_credentials WHERE username=? AND credential_id=?",
               (username, cred_id))
    remaining = db.execute("SELECT COUNT(*) c FROM webauthn_credentials WHERE username=?",
                           (username,)).fetchone()["c"]
    if remaining == 0:
        db.execute("UPDATE user_security SET enabled=0, updated_at=? WHERE username=?",
                   (time.time(), username))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/security/toggle", methods=["POST"])
@login_required
def security_toggle():
    username = session["username"]
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    password = data.get("password", "")
    if not password:
        return jsonify({"error": "Mot de passe requis."}), 400
    ok, err = _verify_password_locked(username, password)
    if not ok:
        return jsonify(err[0]), err[1]
    if enabled:
        n = get_db().execute("SELECT COUNT(*) c FROM webauthn_credentials WHERE username=?",
                             (username,)).fetchone()["c"]
        if n == 0:
            return jsonify({"error": "Enregistre d'abord une clé de sécurité."}), 400
    _set_enabled(username, enabled)
    return jsonify({"ok": True})


@bp.route("/api/security/verify-login", methods=["POST"])
def security_verify_login():
    """Final step of the 2FA login. NO @login_required: the user is not
    authenticated yet (they just provided password / LDAP)."""
    data = request.get_json(silent=True) or {}
    nonce = data.get("nonce")
    credential = data.get("credential")
    if not nonce or not credential:
        return jsonify({"error": "Réponse de clé manquante."}), 400
    res = finish_login(nonce, credential)
    if isinstance(res, tuple):
        return jsonify(res[0]), res[1]
    return jsonify(res)
