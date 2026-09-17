"""Apercu d'une page HTML generee par le modele, dans un bac a sable.

Extrait de app.py le 28/08. La banniere « Apercu » du monolithe couvrait aussi
le chat du playground, des routes d'administration, la recherche et le
classement : seul l'apercu est ici. Le reste attend son propre decoupage.

Les pages sont gardees en SQLite : cle ephemeres, liees a une conversation en
cours, purgees au bout d'une heure (et bornees en nombre). La base plutot qu'un
dict en memoire parce que gunicorn sert plusieurs workers : un apercu cree dans
un worker doit etre lisible par le GET suivant, qui peut tomber sur un autre.
"""
import secrets
import time

from flask import Blueprint, Response, abort, jsonify, request, session

from auth import login_required
from conversation_routes import MSG_MAX_CHARS
from db import get_db

bp = Blueprint('preview', __name__)

_PREVIEW_TTL = 3600             # 1 h : le temps de regarder, pas de stocker
_PREVIEW_MAX = 40               # plafond PAR COMPTE (voir _preview_purge)
_PREVIEW_MAX_TOTAL = 400        # borne de la table entière (une page = jusqu'à 400 ko)


def _preview_purge(username):
    """Purge les aperçus périmés et plafonne, PAR COMPTE puis au global.

    Le plafond était global : les 200 aperçus les plus récents, tous comptes
    confondus. Un compte qui enchaîne les générations — ce que fait précisément
    une session de travail sur le playground — évinçait donc les aperçus des
    AUTRES, dont l'iframe se mettait à répondre 404 (« aperçu introuvable ») au
    milieu d'une conversation. Le plafond par compte ne peut plus evincer que
    ses propres aperçus ; le plafond global reste, lui, une borne de taille pour
    la base (une page peut peser 400 ko).

    Executé à chaque création ; SQLite fait la purge et les plafonds dans la
    même transaction, donc pas de course entre workers gunicorn."""
    db = get_db()
    db.execute("DELETE FROM previews WHERE created_at < ?", (time.time() - _PREVIEW_TTL,))
    db.execute("""DELETE FROM previews WHERE username=? AND id NOT IN (
                     SELECT id FROM previews WHERE username=?
                     ORDER BY created_at DESC LIMIT ?)""",
               (username, username, _PREVIEW_MAX))
    db.execute("""DELETE FROM previews WHERE id NOT IN (
                     SELECT id FROM previews ORDER BY created_at DESC LIMIT ?)""",
               (_PREVIEW_MAX_TOTAL,))
    db.commit()


@bp.route('/playground/preview', methods=['POST'])
@login_required
def playground_preview_create():
    data = request.get_json(silent=True) or {}
    html = str(data.get('html', ''))[:MSG_MAX_CHARS]
    if not html.strip():
        return jsonify({'ok': False, 'error': 'vide'}), 400
    _preview_purge(session['username'])
    pid = secrets.token_urlsafe(18)
    db = get_db()
    db.execute("INSERT INTO previews (id, username, html, created_at) VALUES (?,?,?,?)",
               (pid, session['username'], html, time.time()))
    db.commit()
    return jsonify({'ok': True, 'id': pid})


# Une page générée peut être syntaxiquement parfaite et ne rien faire : mauvaise
# version de bibliothèque, méthode inexistante, variable non définie. Aucune analyse
# statique ne voit ça — seule l'exécution le révèle. L'aperçu exécute déjà la page :
# on lui greffe de quoi REMONTER ses erreurs au playground, qui peut alors les
# afficher et proposer une correction. Le script est inséré en tête pour attraper
# aussi ce qui casse au chargement.
_PREVIEW_RAPPORT = """<script>
(function(){
  var envoye = 0;
  // Le bac à sable lui-même provoque des erreurs qui ne disent RIEN sur la page :
  // origine opaque (donc parent.document illisible, localStorage interdit), et
  // ressources externes bloquées. Les remonter accusait à tort du code correct.
  var bruit = /cross-origin|Blocked a frame|SecurityError|operation is insecure|localStorage|sessionStorage|indexedDB|Access is denied/i;
  function dire(m){
    var s = String(m);
    if (bruit.test(s)) return;
    if (envoye++ > 3) return;                       // on ne noie pas le parent
    try { parent.postMessage({ cronosPreviewError: s.slice(0, 400) }, '*'); } catch (e) {}
  }
  window.addEventListener('error', function(e){
    dire(e && e.message ? e.message : 'erreur de chargement');
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    dire((e && e.reason && e.reason.message) || 'promesse rejetee');
  });
})();
</script>"""


def _preview_avec_rapport(html):
    """Insère le mouchard d'erreurs le plus tôt possible dans le document."""
    for balise in ('<head>', '<HEAD>', '<html>', '<HTML>'):
        i = html.find(balise)
        if i >= 0:
            return html[:i + len(balise)] + _PREVIEW_RAPPORT + html[i + len(balise):]
    return _PREVIEW_RAPPORT + html


@bp.route('/playground/preview/<pid>')
@login_required
def playground_preview_show(pid):
    row = get_db().execute(
        "SELECT username, html FROM previews WHERE id=? AND username=?",
        (pid, session['username'])).fetchone()
    # Cloisonné par compte : l'aperçu d'un autre utilisateur n'est pas lisible.
    if not row:
        abort(404)
    resp = Response(_preview_avec_rapport(row['html']), mimetype='text/html')
    # `sandbox` en en-tête → origine opaque, même hors iframe. Les autres
    # directives sont volontairement absentes : la page doit pouvoir s'exécuter.
    resp.headers['Content-Security-Policy'] = (
        'sandbox allow-scripts allow-forms allow-modals allow-popups')
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control'] = 'no-store'
    return resp
