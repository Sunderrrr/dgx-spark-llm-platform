"""WebAuthn / passkeys — 2e facteur par cle de securite (YubiKey, cle 1Password,
passkey OS), pas de TOTP.

Activee par utilisateur depuis les reglages (roue crantee) : chaque compte
enregistre une passkey et peut exiger sa presence au login (local/LDAP). Les
defis sont one-time, stockes en base et bornes dans le temps.

Scope tres volontaire : le 2e facteur s'applique aux connexions local et LDAP
(choix produit). Le SSO/Authentik n'est PAS dote du step-up, et la passkey est
liee a l'origine (WEBAUTHN_ORIGIN) — une cle enregistree sur le domaine public
ne fonctionne pas depuis une autre origine (ex. le LAN).

Ne depend du noyau (db, auth, config) que via get_db / _apply_session / config :
jamais de l'objet `app`, pour rester importable sans cycle.
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
from local_users import GESTION_SSO, _local_user_auth, gestion_mot_de_passe, passkey_possible

bp = Blueprint("webauthn", __name__)

WEBAUTHN_PENDING_TTL = 5 * 60  # secondes — un defi ne vit que 5 min
_UV = (UserVerificationRequirement.REQUIRED if WEBAUTHN_REQUIRE_UV
       else UserVerificationRequirement.PREFERRED)


# ── Aide base64url ───────────────────────────────────────────────────────────
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    # Base64url (padding optionnel) -> bytes. Le pad manquant est rehabilité.
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── Persistance ──────────────────────────────────────────────────────────────
def _webauthn_enabled(username: str) -> bool:
    """Le compte exige-t-il la passkey au login (local/LDAP) ?"""
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
    # On ne garde qu'UN defi en cours par (compte, type) : un nouveau
    # invalide l'ancien (anti-rejeu, et borne la table).
    db.execute("DELETE FROM pending_webauthn WHERE username=? AND kind=?", (username, kind))
    db.execute(
        "INSERT INTO pending_webauthn (nonce, username, kind, challenge, fullname, is_admin, source, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (nonce, username, kind, challenge, fullname, is_admin, source, now, now + WEBAUTHN_PENDING_TTL))
    db.commit()


def _pending_get(nonce, kind=None):
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
    db = get_db()
    db.execute("DELETE FROM pending_webauthn WHERE nonce=?", (nonce,))
    db.commit()


def _verify_password(username: str, password: str) -> bool:
    """Re-verification par mot de passe (local puis LDAP) — meme ordre que login()."""
    ok, _, _ = _local_user_auth(username, password)
    if ok:
        return True
    from auth import ldap_authenticate
    ok2, _, _ = ldap_authenticate(username, password)
    return ok2


def _verify_password_locked(username: str, password: str):
    """Re-verification PROTEGEE par le verrou de login — (ok, reponse_erreur).

    Sans ce garde-fou, une session detournee (cookie + jeton CSRF vivent dans la
    meme session) permettait d'essayer des mots de passe a la vitesse du reseau
    via /api/security/* : le compteur de /login n'etait jamais incremente, donc
    le verrou 6 echecs / 15 min ne se declenchait jamais, et un mot de passe
    trouve donnait le mot de passe REUTILISABLE du compte (plus la possibilite
    de desenregistrer ses passkeys).

    Le compteur est celui du COMPTE (`user:<nom>`), partage avec /login : les
    tentatives faites depuis les Reglages verrouillent aussi la page de
    connexion, et inversement. Les echecs sont donc bornes globalement, quel
    que soit le chemin emprunte.
    """
    ukey = f"user:{username}"
    # Compte SSO : aucun mot de passe que le portail puisse vérifier. Le dire
    # AVANT le compteur d'échecs est délibéré : sinon un utilisateur SSO qui
    # essaie son mot de passe d'identité (le seul qu'il ait) accumule des échecs
    # sur `user:<nom>` — le compteur étant partagé avec /login, il finissait par
    # se verrouiller lui-même la page de connexion, pour une action impossible
    # dès le départ.
    if gestion_mot_de_passe(username)['gestion'] == GESTION_SSO:
        return False, ({"error": "Compte SSO : aucun mot de passe n'est géré par le "
                                 "portail, la vérification est impossible."}, 400)
    wait = _login_locked(ukey)
    if wait:
        return False, ({"error": f"Trop de tentatives. Réessaie dans {wait // 60 + 1} min."}, 429)
    if _verify_password(username, password):
        _login_reset(ukey)
        return True, None
    _login_fail(ukey)
    return False, ({"error": "Mot de passe incorrect."}, 401)


# ── Flux d'enregistrement (ajout d'une cle) ───────────────────────────────────
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
        # user_id doit etre stable et unique par compte : hash du username.
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
    # `transports` n'est pas renvoyé par la lib : il vient de la réponse du
    # client (Navigator.credentials.create → response.transports, si dispo).
    transports_arr = (credential.get("response") or {}).get("transports") or []
    transports = json.dumps(transports_arr)
    db = get_db()
    db.execute("DELETE FROM webauthn_credentials WHERE credential_id=?", (cred_id,))
    db.execute(
        "INSERT INTO webauthn_credentials "
        "(username, credential_id, public_key, sign_count, transports, label, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (username, cred_id, ver.credential_public_key, ver.sign_count,
         transports, label or "Clé de sécurité", time.time()))
    _set_enabled(username, True)  # enregistrer la 1re cle => 2FA activee
    _pending_clear(nonce)
    db.commit()
    return {"ok": True}


# ── Flux d'authentification (etape 2 du login) ───────────────────────────────
def start_login(username: str, fullname: str, is_admin: bool, source: str):
    """Genere le defi pour la 2e etape (apres un mot de passe / LDAP valide)."""
    creds = _stored_credentials(username)
    # transports est un simple indice côté navigateur ; on l'omet volontairement
    # (la lib exige des enums et non des chaînes, et l'indice est optionnel).
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
    # id de la cle telle que verifiee (on le re-derive de la reponse).
    try:
        cred_id = _b64url(base64.urlsafe_b64decode(credential.get("id", "") + "=="))
    except Exception:
        return {"error": "Clé invalide."}, 400
    row = _credential_row(username, cred_id)
    if not row:
        # Le défi est à USAGE UNIQUE : les deux autres sorties le consomment,
        # pas celle-ci — il restait donc valide jusqu'à son TTL (5 min) après une
        # première tentative infructueuse. Pas de rejeu possible pour autant
        # (`sign_count` vérifié et mis à jour), mais un défi ouvert ne doit pas
        # survivre à l'échec qui l'a utilisé.
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
    # Un blocage posé PENDANT les quelques minutes du défi doit faire échouer
    # la seconde étape : la passkey prouve l'identité, elle ne vaut pas
    # autorisation. Le contrôle de app.py a eu lieu avant le défi, celui-ci
    # ferme la fenêtre entre les deux.
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
    return jsonify({
        "enabled": _webauthn_enabled(username),
        # L'interface doit pouvoir DIRE d'où vient le mot de passe, et donc si
        # un formulaire de changement (ou l'ajout d'une passkey) a un sens ici.
        "password_managed_by": gestion_mot_de_passe(username)["gestion"],
        "passkey_possible": passkey_possible(username),
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
    # Re-vérification du mot de passe AVANT de créer quoi que ce soit.
    #
    # Sans elle, un simple cookie volé suffisait à enregistrer SA clé : le défi
    # s'obtient avec le cookie seul (`/api/csrf` est public et rend le jeton de
    # la session en cours), `finish_registration` active alors la 2FA
    # (`_set_enabled(username, True)`) avec pour seule clé celle du voleur, et
    # la victime se retrouve dans un compte où son mot de passe correct ne suffit
    # plus et où elle ne peut produire aucune assertion — récupération par un
    # admin uniquement, et destructrice. `security_remove` et `security_toggle`
    # exigeaient déjà ce mot de passe ; l'ajout d'une clé, non, alors que c'est
    # l'opération qui CRÉE la condition de verrouillage.
    #
    # Effet de bord voulu : la portée « local + LDAP » de la 2FA, jusqu'ici
    # seulement affichée par l'interface (`passkey_possible`), est enfin
    # appliquée par le serveur — un compte SSO reçoit 400 « Compte SSO ».
    data = request.get_json(silent=True) or {}
    motdepasse = data.get("password") or ""
    if not motdepasse:
        # Champ absent = requête incomplète, pas un mot de passe faux : répondre
        # 401 ici consommerait une tentative du verrou partagé avec /login pour
        # une requête qui n'a rien tenté.
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
    """Etape finale du login 2FA. PAS de @login_required : l'utilisateur
    n'est pas encore authentifie (il vient de fournir mot de passe / LDAP)."""
    data = request.get_json(silent=True) or {}
    nonce = data.get("nonce")
    credential = data.get("credential")
    if not nonce or not credential:
        return jsonify({"error": "Réponse de clé manquante."}), 400
    res = finish_login(nonce, credential)
    if isinstance(res, tuple):
        return jsonify(res[0]), res[1]
    return jsonify(res)
