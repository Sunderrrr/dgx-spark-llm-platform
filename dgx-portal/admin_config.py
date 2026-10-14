"""Admin — instance configuration: the web-only settings (LDAP, SSO, SMTP,
Discord) with their connection tests, the branding, and the full-service
validation run from the panel.

Routes of the `admin` blueprint (defined in admin_routes.py): the endpoint
names stay `admin.*` whatever module holds them.
"""
import re
import threading
import time

from flask import (jsonify, request,
                   session)

from auth import (admin_required)
from config import PUBLIC_WEB_URL
from instance_config import smtp_config
from db import (get_db, log_audit)
from notify import (notify_discord_test, send_test_email)

from admin_routes import _json_error, bp

@bp.route('/admin/validate', methods=['POST'])
@admin_required
def admin_validate():
    """Runs the full validation pipeline on demand, from the admin.

    « je puisse lancer un test qui teste tout et me dise si tout va bien ou là
    où ça n'a pas marché » — scripts/validate.sh is the whole thing (code gates,
    running service, user journeys, health). It takes minutes, so it runs in a
    thread and the UI polls the state: a synchronous route would time out long
    before the journeys finish.

    ONE run at a time: the journeys use the demo account and a shared
    conversation state, two overlapping runs would fight each other and report
    nonsense.
    """
    with _validator['v']:
        if _validator['running']:
            return jsonify({'ok': False, 'error': "Une vérification est déjà en cours."}), 409
        _validator.update(running=True, start=time.time(), output=[], code=None)

    def _launch():
        import subprocess
        # The pipeline needs Docker and the HOST — a container cannot run it.
        # The portal writes a REQUEST FILE and a systemd .path unit picks it up
        # (`dgx-validation.path` → `dgx-validation.service`): the container
        # never executes anything on the host, which is the whole point.
        try:
            import pathlib
            request_row = pathlib.Path('/run/dgx/validation/demande.txt')
            request_row.parent.mkdir(parents=True, exist_ok=True)
            request_row.write_text(str(time.time()))
            # The run itself is the host's job; we watch its log.
            log = pathlib.Path('/run/dgx/validation/courante.log')
            p = subprocess.Popen(['tail', '-F', str(log)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            start = time.time()
            for line in p.stdout:
                with _validator['v']:
                    _validator['output'].append(line.rstrip())
                    # The log is a tail, not an ever-growing string: the panel
                    # shows the last lines, and a runaway output must not eat
                    # the portal's memory.
                    if len(_validator['output']) > 400:
                        del _validator['output'][:len(_validator['output']) - 400]
                # `tail -F` NEVER exits — without this the run stays « running »
                # forever and the panel refuses the next one (measured 2026-10-10,
                # « a check is already running » while nothing was running).
                # Stop when the verdict lands, or after a hard time bound.
                # scripts/validate.sh prints « OVERALL VERDICT »; the former
                # French pipeline printed « VERDICT GLOBAL » (older logs).
                if ('OVERALL VERDICT' in line or 'VERDICT GLOBAL' in line
                        or time.time() - start > 1200):
                    p.kill()
                    break
            _validator['code'] = 0 if any('ALL GREEN' in x or 'TOUT EST VERT' in x
                                         for x in _validator['output']) else 1
        except Exception as e:                      # noqa: BLE001
            with _validator['v']:
                _validator['output'].append(f"Erreur: {e}")
                _validator['code'] = 1
        finally:
            with _validator['v']:
                _validator['running'] = False

    threading.Thread(target=_launch, daemon=True).start()
    return jsonify({'ok': True})


@bp.route('/admin/validate/state')
@admin_required
def admin_validate_state():
    """Progress + the output tail + the verdict, polled by the panel."""
    with _validator['v']:
        return jsonify({
            'ok': True,
            'running': _validator['running'],
            'duration_s': round(time.time() - _validator['start']) if _validator['start'] else 0,
            'code': _validator['code'],
            'output': _validator['output'][-120:],
            'passed': _validator['code'] == 0 if _validator['code'] is not None else None,
        })


_validator = {'running': False, 'start': None, 'output': [], 'code': None,
             'v': threading.Lock()}


@bp.route('/admin/config', methods=['GET', 'POST'])
@admin_required
def admin_config():
    """Instance configuration from the admin: LDAP, SSO, SMTP.

    « je veux que sur l'admin on puisse conf le ldap, le sso, le smtp, bref
    tout … c'est mieux que les vars importantes soient stockées sur l'interface
    web » — .env is the starting point, the database wins, and a secret is only
    ever reported as configured/absent, never shown. The panel also says WHERE
    each value comes from (interface / .env / défaut) — the first question when
    a setting « does not take ».
    """
    from instance_config import CONFIGURABLE, set_value, config_state
    if request.method == 'GET':
        return jsonify({'ok': True, 'config': config_state(),
                        'fields': [{'name': n, 'label': l, 'secret': s}
                                   for n, l, s, _e, _d in CONFIGURABLE],
                        # What to declare at the providers: the URIs this
                        # instance answers on (used when the field is empty).
                        'suggestions': {
                            'oidc_redirect_uri': f'{PUBLIC_WEB_URL}/api/oauth2-redirect',
                            'oidc_post_logout': f'{PUBLIC_WEB_URL}/',
                            'discord_redirect_uri': f'{PUBLIC_WEB_URL}/discord/callback',
                        }})
    # An empty field means « back to the .env value »; a secret is only written
    # when the operator types one, never echoed back.
    modifies = 0
    for name, _label, _secret, _env, _default in CONFIGURABLE:
        if name in request.form:
            set_value(name, request.form.get(name, '').strip())
            modifies += 1
    log_audit(session['username'], 'config.modifiee',
              f'{modifies} valeur(s) — LDAP/SSO/SMTP/sécurité')
    return jsonify({'ok': True, 'modifies': modifies})


def _test_ldap():
    from ldap3 import NONE, SIMPLE, Connection, Server
    from instance_config import ldap_config
    c = ldap_config()
    if not c['uri']:
        return False, "Aucune URI LDAP renseignée."
    if not (c['bind_dn'] and c['bind_pw']):
        return False, "Le compte de service (DN et mot de passe) est requis."
    try:
        conn = Connection(Server(c['uri'], get_info=NONE, connect_timeout=5),
                          user=c['bind_dn'], password=c['bind_pw'],
                          authentication=SIMPLE, auto_bind=True, receive_timeout=15)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"Connexion ou authentification refusée : {exc}"[:300]
    try:
        conn.search(f"{c['users_dn']},{c['base']}", f"({c['login_attr']}=*)",
                    attributes=[c['login_attr']], size_limit=1000)
        n = len(conn.entries)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"Connecté, mais la recherche des comptes échoue : {exc}"[:300]
    finally:
        try:
            conn.unbind()
        except Exception:                                        # noqa: BLE001
            pass
    if not n:
        return False, (f"Connecté, mais aucun compte sous {c['users_dn']},{c['base']} "
                       f"avec l'attribut {c['login_attr']}.")
    return True, f"Connexion réussie : {n} compte(s) trouvé(s)."


def _test_sso():
    from instance_config import oidc_config
    from sso_client import _metadata
    c = oidc_config()
    if not c['metadata_url']:
        return False, "Aucune URL de métadonnées renseignée."
    md = _metadata(c['metadata_url'])
    missing = [k for k in ('issuer', 'authorization_endpoint', 'token_endpoint') if not md.get(k)]
    if missing:
        return False, ("Métadonnées illisibles ou incomplètes "
                       f"({', '.join(missing)} absent) : vérifie l'URL.")
    if not (c['client_id'] and c['client_secret']):
        return False, f"Fournisseur joignable ({md['issuer']}), mais client ID et secret sont requis."
    return True, f"Fournisseur joignable : {md['issuer']}."


def _test_smtp():
    c = smtp_config()
    if not c['configured']:
        return False, "Hôte, utilisateur et mot de passe SMTP sont requis."
    if not c['admin_email']:
        return False, "Renseigne le destinataire des alertes : le test lui est envoyé."
    if send_test_email():
        return True, f"E-mail de test envoyé à {c['admin_email']}."
    return False, "L'envoi a échoué : vérifie l'hôte, le port et les identifiants."


def _test_discord():
    from instance_config import discord_config
    d = discord_config()
    if not d['webhook']:
        return False, "Aucun webhook renseigné."
    if notify_discord_test():
        return True, "Message de test publié sur le webhook."
    return False, "Le webhook a refusé le message : vérifie son URL."


def _test_s3():
    import object_store
    return object_store.test()


_TESTS = {'ldap': _test_ldap, 'sso': _test_sso, 'smtp': _test_smtp,
          'discord': _test_discord, 's3': _test_s3}


@bp.route('/admin/config/test/<part>', methods=['POST'])
@admin_required
def admin_config_test(part):
    """« Tester » of a section of Admin → Config, on the SAVED values: LDAP
    binds with the service account and counts the accounts, SSO reads the
    provider's metadata, SMTP sends the test e-mail, Discord posts on the
    webhook. Always 200 with {ok, message}: a failed test is an answer."""
    test = _TESTS.get(part)
    if test is None:
        return _json_error("Section inconnue.", 404)
    ok, message = test()
    log_audit(session.get('username'), 'config.test', f"{part} : {'ok' if ok else 'échec'}")
    return jsonify({'ok': ok, 'message': message})


@bp.route('/admin/branding', methods=['POST'])
@admin_required
def admin_branding():
    """The deployment's identity: name in the sidebar, and the logo.

    « personnaliser en fonction de celui qui le déploie » — the operator names
    their own instance; « DGX » is only the default (db.platform_name).
    The logo is stored as the uploaded data URL: one row, no filesystem to
    police, and the size is bounded like every other upload on this portal.
    """
    name = (request.form.get('platform_name') or '').strip()[:60]
    logo = (request.form.get('platform_logo') or '').strip()
    if name and not re.fullmatch(r'[\w .\-\u00c0-\u024f]{1,60}', name):
        return jsonify({'ok': False, 'error': "Nom invalide."}), 400
    if logo and not re.fullmatch(r'data:image/(png|jpeg|webp|svg\+xml|gif);base64,[A-Za-z0-9+/=]+', logo):
        return jsonify({'ok': False, 'error': "Logo attendu en PNG, JPEG, WebP, SVG ou GIF."}), 400
    if logo and len(logo) > 2 * 1024 * 1024:
        return jsonify({'ok': False, 'error': "Logo trop lourd (2 Mo maximum)."}), 400
    db = get_db()
    if name:
        db.execute("INSERT INTO settings (key, value) VALUES ('platform_name', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
    # '-' is the explicit « remove the logo », like the auth header of an MCP
    # server: an empty field would mean « change nothing ».
    if logo == '-':
        db.execute("DELETE FROM settings WHERE key='platform_logo'")
    elif logo:
        db.execute("INSERT INTO settings (key, value) VALUES ('platform_logo', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (logo,))
    db.commit()
    log_audit(session['username'], 'branding.modifie', f'nom={name or "(inchangé)"} '
              f'logo={"retiré" if logo == "-" else ("posé" if logo else "inchangé")}')
    return jsonify({'ok': True})
