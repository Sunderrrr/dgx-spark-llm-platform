"""Access to the portal's SQLite database.

Extracted from app.py on 28/08. This is the SHARED CORE: without it, no
other section of the monolith was extractable, because almost all call
get_db() (103 calls) and a module imported by app.py cannot re-import
app.py — import cycle.

This module depends only on flask.g and sqlite3: it imports nothing from
the portal, so everyone can import it with no cycle risk.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime

from flask import g

from config import KEY_BUDGET, KEY_DURATION, LITELLM_DB_URL

DB_PATH = '/app/data/portal.db'


def log_audit(username, action, detail):
    """Writes an audit log entry (dedicated connection, usable outside a
    request context — even from a module called by app.py).

    `action` is a short, stable label (e.g. « launch », « stop »,
    « user.create »); `detail` the readable description. We log the actor
    (`username`) and the timestamp, never a secret.

    A write failure must not break the ongoing action, but it must not be
    INVISIBLE either: a silent audit is indistinguishable from an action
    that never happened. It thus goes to stderr, where the container logs
    show it.
    """
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("INSERT INTO audit_log (username, action, detail, created_at) VALUES (?,?,?,?)",
                  (username or 'système', action, detail, datetime.now().isoformat()))
        # We only keep recent history in the LIVE table (sensitive actions are
        # rare). 500 rows recycled within a few days on this machine (account
        # lockouts and session revocations write there too, not only model
        # launches): « who did what last week » became
        # unanswerable at the very moment it was asked. Hence 5000.
        # The evicted rows are ARCHIVED, not deleted (2026-10-02): the trace of
        # admin actions must survive, and the plain DELETE that stood here threw
        # away exactly the entries one asks for months later. `audit_log_archive`
        # has the same schema; it grows slowly (one row per eviction batch) and
        # rides along the SQLite backups (cronos-backup).
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log_archive (
            id INTEGER PRIMARY KEY, username TEXT, action TEXT,
            detail TEXT, created_at TEXT)""")
        c.execute("""INSERT OR IGNORE INTO audit_log_archive
                     SELECT * FROM audit_log WHERE id NOT IN (
                         SELECT id FROM audit_log ORDER BY id DESC LIMIT 5000)""")
        c.execute("""DELETE FROM audit_log WHERE id NOT IN (
            SELECT id FROM audit_log ORDER BY id DESC LIMIT 5000)""")
        c.commit()
        c.close()
    except Exception as exc:                                     # noqa: BLE001
        print(f"[audit] écriture impossible ({action}) : {exc}", file=sys.stderr)


def add_notification(username, kind, title, max_per_user=50):
    """Creates an in-app notification (dedicated connection, usable outside a
    request — even from a thread worker). `kind` = 'image'|'music'|'request'|...
    We bound the history per user so the database does not bloat."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("INSERT INTO notifications (username, kind, title, seen, created_at) VALUES (?,?,?,0,?)",
                  (username or 'système', kind, title, datetime.now().isoformat()))
        c.execute("""DELETE FROM notifications WHERE username=? AND id NOT IN (
            SELECT id FROM notifications WHERE username=? ORDER BY id DESC LIMIT ?)""",
                  (username, username, max_per_user))
        c.commit()
        c.close()
    except Exception:
        pass


def notification_unread(username):
    """Number of unread notifications (the bell badge)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        row = c.execute("SELECT COUNT(*) FROM notifications WHERE username=? AND seen=0",
                        (username,)).fetchone()
        c.close()
        return int(row[0] or 0)
    except Exception:
        return 0


def get_db():
    """SQLite connection bound to the Flask application context.

    One connection per request, closed by close_db. THREAD workloads (image
    generation, video, jobs) have no Flask context and thus open their own
    connection with sqlite3.connect(DB_PATH) — deliberate, not an
    oversight.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """Registered by app.py via app.teardown_appcontext(close_db).

    No decorator here: the decorator would require the `app` object, hence
    an import of app.py, hence exactly the cycle this module exists to avoid.
    """
    db = g.pop('db', None)
    if db:
        db.close()


# ── Persisted settings (table `settings`) ───────────────────────────────────
# Simple SQL wrappers over get_db: their place is in the core, not in the
# monolith, because several extracted sections need them.

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    db.commit()


def maintenance_active():
    return get_setting('maintenance_mode', '0') == '1'


# ── Base LiteLLM (Postgres) ──────────────────────────────────────────────────
# Read-only, for the consumption statistics and the key budgets.
# Its place is here, with the SQLite access: this is the databases module,
# and litellm_client as well as the statistics routes need it.

def _spend_conn():
    if not LITELLM_DB_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(LITELLM_DB_URL, connect_timeout=4)
        conn.autocommit = True   # read-only: prevents a failed query from aborting the transaction
        return conn
    except Exception:
        return None


# ── Schema et migrations ─────────────────────────────────────────────────────
# init_db creates the missing tables and applies the column migrations. It
# was in app.py under the « DB » banner, with is_admin_username which, for
# its part, falls under authentication and stays there for now.
# Called once at bootstrap, from app.py.

def init_db():
    os.makedirs('/app/data', exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS model_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            fullname   TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            reason     TEXT,
            status     TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            key_alias  TEXT NOT NULL,
            key_value  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(username, key_alias)
        );
        CREATE TABLE IF NOT EXISTS model_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            hf_model_id TEXT NOT NULL,
            vllm_args   TEXT DEFAULT '',
            engine      TEXT NOT NULL DEFAULT 'vllm',   -- 'vllm' | 'llamacpp'
            added_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ocr_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            hf_model_id TEXT NOT NULL,
            vllm_args   TEXT DEFAULT '',
            added_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voice_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            repo_id     TEXT NOT NULL,   -- chatterbox | chatterbox-turbo | chatterbox-multilingual
            added_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budget_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL,
            fullname        TEXT NOT NULL,
            key_alias       TEXT NOT NULL,
            current_budget  REAL,
            reason          TEXT,
            status          TEXT DEFAULT 'pending',
            granted_amount  REAL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS budget_grants (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            username       TEXT NOT NULL,
            base_budget    REAL NOT NULL,
            current_budget REAL NOT NULL,
            expires_at     TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            a          TEXT DEFAULT '',
            b          TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcement_state (
            username     TEXT PRIMARY KEY,
            last_seen_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discord_links (
            username     TEXT PRIMARY KEY,
            discord_id   TEXT NOT NULL,
            discord_name TEXT DEFAULT '',
            linked_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL,
            name          TEXT NOT NULL,
            url           TEXT NOT NULL,
            auth_header   TEXT,
            description   TEXT DEFAULT '',
            allowed_tools TEXT DEFAULT '',
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            UNIQUE(username, name)
        );
        CREATE TABLE IF NOT EXISTS skills (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT NOT NULL,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL,
            instructions TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            UNIQUE(username, name)
        );
        CREATE TABLE IF NOT EXISTS user_prefs (
            username  TEXT PRIMARY KEY,
            avatar_id TEXT,
            theme_id  TEXT,
            lang      TEXT
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            client_id  TEXT NOT NULL,       -- id généré côté client (idempotence)
            title      TEXT NOT NULL,
            model      TEXT DEFAULT '',
            messages   TEXT NOT NULL,       -- JSON [{role, content}]
            updated_at TEXT NOT NULL,
            UNIQUE(username, client_id)
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            key          TEXT PRIMARY KEY,   -- "ip|user" ou "ip"
            fails        INTEGER NOT NULL DEFAULT 0,
            first_at     REAL NOT NULL,
            locked_until REAL NOT NULL DEFAULT 0
        );
        -- Registre serveur des sessions : le cookie signé ne porte qu'un
        -- sid aléatoire ; la ligne en base permet de révoquer une session à
        -- volonté (logout, compte verrouillé, révocation admin) et pas
        -- seulement à l'expiration HTTP.
        CREATE TABLE IF NOT EXISTS user_sessions (
            sid        TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            auth_at    REAL NOT NULL,
            expires_at REAL NOT NULL,
            revoked    INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            -- IP et user-agent d'ouverture : sans eux, personne — ni
            -- l'utilisateur ni l'admin — ne peut distinguer « ma session de ce
            -- matin » d'un cookie volé, donc la liste des sessions n'aurait
            -- aucune valeur de diagnostic.
            ip         TEXT,
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_sessions_username ON user_sessions(username);
        -- WebAuthn (2FA par passkey) : clés enregistrées + flag d'activation +
        -- challenges en attente (one-time, bornés dans le temps). Cf. webauthn_routes.py
        CREATE TABLE IF NOT EXISTS user_security (
            username   TEXT PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL,
            credential_id TEXT NOT NULL UNIQUE,  -- base64url (toujours normalisé)
            public_key    BLOB NOT NULL,          -- clé publique brute (RAW)
            sign_count    INTEGER NOT NULL DEFAULT 0,
            transports    TEXT,                   -- JSON array
            label         TEXT NOT NULL DEFAULT '',
            created_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webauthn_cred_username ON webauthn_credentials(username);
        CREATE TABLE IF NOT EXISTS pending_webauthn (
            nonce      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            kind       TEXT NOT NULL,             -- 'register' | 'login' | 'reverify'
            challenge  BLOB NOT NULL,
            fullname   TEXT,
            is_admin   INTEGER,
            source     TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        -- Anti-spam : une seule demande « lancer une catégorie média » par
        -- (utilisateur, catégorie) dans la fenêtre MEDIA_REQUEST_COOLDOWN_S.
        CREATE TABLE IF NOT EXISTS media_request_cooldown (
            username   TEXT NOT NULL,
            category   TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (username, category)
        );
        CREATE TABLE IF NOT EXISTS video_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL,
            prompt_id       TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            video_path      TEXT,
            video_subfolder TEXT,
            video_type      TEXT,
            created_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS image_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL,
            prompt_id       TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            image_path      TEXT,
            image_subfolder TEXT,
            image_type      TEXT,
            duration_ms     INTEGER,
            created_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS music_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            job_id      TEXT NOT NULL,
            prompt      TEXT NOT NULL,
            lyrics      TEXT,
            duration_s  INTEGER,
            status      TEXT NOT NULL DEFAULT 'running',
            duration_ms INTEGER,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ocr_jobs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voice_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            text        TEXT NOT NULL,
            audio_path  TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS previews (
            id         TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            html       TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_previews_created ON previews(created_at);
        -- Histories are read per user (WHERE username=? ... ORDER BY id/created_at
        -- DESC LIMIT n) and purged with the same shape; an index on the leading
        -- username column turns what was a full scan into an indexed range lookup.
        CREATE INDEX IF NOT EXISTS idx_image_jobs_user   ON image_jobs(username, id);
        CREATE INDEX IF NOT EXISTS idx_music_jobs_user   ON music_jobs(username, id);
        CREATE INDEX IF NOT EXISTS idx_video_jobs_user   ON video_jobs(username, id);
        CREATE INDEX IF NOT EXISTS idx_voice_jobs_user   ON voice_jobs(username, id);
        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_user     ON ocr_jobs(username, id);
        CREATE INDEX IF NOT EXISTS idx_model_req_user    ON model_requests(username, created_at);
        CREATE INDEX IF NOT EXISTS idx_model_req_status  ON model_requests(username, status);
        CREATE INDEX IF NOT EXISTS idx_budget_req_status ON budget_requests(username, status);
        CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(username, updated_at);
        -- Journal d'audit des actions sensibles (launch/stop modèle, changements
        -- admin, comptes). Lecture par ordre antéchronologique (id DESC), filtre
        -- optionnel par utilisateur → index sur (username, id).
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(username, id);
        -- Partage en lecture seule d'une conversation (lien public). On duplique
        -- un instantané (title/model/messages) plutôt que de pointer vers la
        -- conversation de l'utilisateur : le partage survit à la suppression de
        -- l'original et ne dévoile rien d'autre que ce qui a été partagé.
        CREATE TABLE IF NOT EXISTS conversation_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            -- Propriétaire du partage : sans lui, supprimer un compte laissait
            -- derrière lui des liens publics vers le contenu de ses
            -- conversations, sans aucun moyen de les retrouver.
            username TEXT,
            title TEXT NOT NULL,
            model TEXT NOT NULL,
            messages TEXT NOT NULL,
            created_at TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0
        );
        -- Notifications in-app (cloche, vu/non-vu) : fin de génération média,
        -- demandes modèle/budget traitées. `seen` pilote le badge de la cloche.
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(username, id);
    ''')
    # Migration: columns added to mcp_servers after its initial creation
    # (description, tool filter, enablement) — additive ALTER, lossless.
    pref_cols = {r[1] for r in db.execute("PRAGMA table_info(user_prefs)")}
    for col in ('theme_id', 'lang'):
        if col not in pref_cols:
            db.execute(f"ALTER TABLE user_prefs ADD COLUMN {col} TEXT")
    # Memory: ENABLED by default (2026-09, operator choice — home
    # multi-user portal). The graph stays strictly personal: each
    # reads and writes only their own, and the setting on the Memory
    # page turns it off.
    if 'memory_enabled' not in pref_cols:
        db.execute("ALTER TABLE user_prefs ADD COLUMN memory_enabled INTEGER NOT NULL DEFAULT 1")
    elif 'memory_enabled INTEGER NOT NULL DEFAULT 0' in (
            db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_prefs'"
                       ).fetchone()[0] or '') or str(next(
                (r[4] for r in db.execute("PRAGMA table_info(user_prefs)")
                 if r[1] == 'memory_enabled'), None)) == '0':
        # Databases created before the switch: the column carries DEFAULT 0 —
        # visible in the DDL when the table always had it in its CREATE, and in
        # PRAGMA table_info (dflt_value) when it arrived via ALTER (the
        # sqlite_master DDL does not move then). SQLite cannot change a DEFAULT
        # in place — table copy. The guard makes the migration ONE-SHOT: after
        # the copy the default is 1 and never replays, so a user who turns their
        # memory off afterwards stays off,
        # even after a restart.
        db.execute('''
            CREATE TABLE user_prefs_new (
                username  TEXT PRIMARY KEY,
                avatar_id TEXT,
                theme_id  TEXT,
                lang      TEXT,
                onboarded INTEGER NOT NULL DEFAULT 0,
                memory_enabled    INTEGER NOT NULL DEFAULT 1,
                websearch_enabled INTEGER NOT NULL DEFAULT 1
            )''')
        # Copy only the columns that EXIST (a database may predate
        # websearch_enabled): the missing ones take their DEFAULT.
        colonnes = {r[1] for r in db.execute("PRAGMA table_info(user_prefs)")}
        communes = [c for c in ('username', 'avatar_id', 'theme_id', 'lang',
                                'onboarded', 'memory_enabled', 'websearch_enabled')
                    if c in colonnes]
        cols = ', '.join(communes)
        db.execute(f"INSERT INTO user_prefs_new ({cols}) SELECT {cols} FROM user_prefs")
        db.execute("DROP TABLE user_prefs")
        db.execute("ALTER TABLE user_prefs_new RENAME TO user_prefs")
        # « for all users »: accounts created before the switch carry their
        # original 0 (never an explicit refusal, the feature predating it by a
        # week) — all moved to 1, once only.
        db.execute("UPDATE user_prefs SET memory_enabled=1")
        # The table was recreated with ALL columns: refresh the list so the
        # conditional ALTERs below double nothing.
        pref_cols = {r[1] for r in db.execute("PRAGMA table_info(user_prefs)")}
    # Onboarding shown once per ACCOUNT (not per browser): the newcomer sees it
    # whatever the machine, and never sees it again.
    if 'websearch_enabled' not in pref_cols:
        # Enabled by default, like memory since 2026-09: search keeps nothing
        # about the user. The setting turns it off for whoever does not want their
        # questions to reach external engines.
        db.execute("ALTER TABLE user_prefs ADD COLUMN websearch_enabled INTEGER NOT NULL DEFAULT 1")
    if 'onboarded' not in pref_cols:
        db.execute("ALTER TABLE user_prefs ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
    mcp_cols = {r[1] for r in db.execute("PRAGMA table_info(mcp_servers)")}
    for col, ddl in (('description', "TEXT DEFAULT ''"),
                     ('allowed_tools', "TEXT DEFAULT ''"),
                     ('enabled', "INTEGER NOT NULL DEFAULT 1")):
        if col not in mcp_cols:
            db.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col} {ddl}")
        # Migration: live decode gauge (2026-10-02). TabbyAPI publishes no
        # /metrics and its stream carries no token counter (usage arrives once,
        # at the end), so the dashboard showed « 0 tok/s » while the model was
        # generating. The relay now reports `tokens` (~ chars/4, the composer's
        # live convention) for its in-flight request; `debit_decode_live()` sums
        # the ongoing rates from there.
        # Guarded by table existence: on a fresh database this migration runs
        # BEFORE the script that creates `inflight_requests` — whose CREATE
        # already carries the column. On an existing database the table is
        # there and the ALTER does its job.
        inflight_cols = {r[1] for r in db.execute("PRAGMA table_info(inflight_requests)")}
        if inflight_cols and 'tokens' not in inflight_cols:
            db.execute("ALTER TABLE inflight_requests ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
        if inflight_cols and 'decode_since' not in inflight_cols:
            # Wall time of the FIRST token: the decode rate divides by the
            # decode time, not by the request's age (queue + prefill would
            # dilute it — measured 2026-10-02: 12 tok/s displayed against ~19
            # real).
            db.execute("ALTER TABLE inflight_requests ADD COLUMN decode_since REAL")
    # Migration: api_keys from GLOBAL unique key_alias → unique per (username, alias)
    # (prevents a user from overwriting another's row via an identical alias).
    sql = (db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='api_keys'")
             .fetchone() or [''])[0] or ''
    if 'UNIQUE(username' not in sql.replace(' ', ''):
        db.executescript('''
            CREATE TABLE api_keys_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
                key_alias TEXT NOT NULL, key_value TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(username, key_alias)
            );
            INSERT INTO api_keys_new (id, username, key_alias, key_value, created_at)
                SELECT id, username, key_alias, key_value, created_at FROM api_keys;
            DROP TABLE api_keys;
            ALTER TABLE api_keys_new RENAME TO api_keys;
        ''')
    # Migration: add the inference engine (vLLM historically, llama.cpp for GGUFs)
    cols = {r[1] for r in db.execute("PRAGMA table_info(model_configs)")}
    if 'engine' not in cols:
        db.execute("ALTER TABLE model_configs ADD COLUMN engine TEXT NOT NULL DEFAULT 'vllm'")
    # Migration: analyzed image kept per OCR job (history display
    # with the "detected zones" view, not just the text). NULL for
    # rows already existing before this addition.
    ocr_cols = {r[1] for r in db.execute("PRAGMA table_info(ocr_jobs)")}
    if 'image_path' not in ocr_cols:
        db.execute("ALTER TABLE ocr_jobs ADD COLUMN image_path TEXT")
    # Several versions of one piece (files <job_id>_<idx>.wav): same principle
    # as images — count = requested, done_count = already produced.
    _mj = {r[1] for r in db.execute("PRAGMA table_info(music_jobs)")}
    if 'count' not in _mj:
        db.execute("ALTER TABLE music_jobs ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
    if 'done_count' not in _mj:
        db.execute("ALTER TABLE music_jobs ADD COLUMN done_count INTEGER NOT NULL DEFAULT 0")
    # Batch image generation: N variations per prompt (files <prompt_id>_<idx>.png).
    # count = requested, done_count = produced so far (progressive display).
    _ij = {r[1] for r in db.execute("PRAGMA table_info(image_jobs)")}
    if 'count' not in _ij:
        db.execute("ALTER TABLE image_jobs ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
    if 'done_count' not in _ij:
        db.execute("ALTER TABLE image_jobs ADD COLUMN done_count INTEGER NOT NULL DEFAULT 0")
    if 'format' not in _ij:
        db.execute("ALTER TABLE image_jobs ADD COLUMN format TEXT NOT NULL DEFAULT 'png'")
    # Migration: generation duration (ms) per job → home-page metrics (average
    # OCR / video / voice time). NULL for jobs prior to this addition.
    for _tbl in ('ocr_jobs', 'video_jobs', 'voice_jobs'):
        _jc = {r[1] for r in db.execute(f"PRAGMA table_info({_tbl})")}
        if 'duration_ms' not in _jc:
            db.execute(f"ALTER TABLE {_tbl} ADD COLUMN duration_ms INTEGER")
    # Enriched metrics: duration of the produced audio (voice real-time factor)
    # and requested video duration (generated seconds, video real-time factor).
    _vj = {r[1] for r in db.execute("PRAGMA table_info(voice_jobs)")}
    if 'audio_ms' not in _vj:
        db.execute("ALTER TABLE voice_jobs ADD COLUMN audio_ms INTEGER")
    _vd = {r[1] for r in db.execute("PRAGMA table_info(video_jobs)")}
    if 'req_duration_s' not in _vd:
        db.execute("ALTER TABLE video_jobs ADD COLUMN req_duration_s INTEGER")
    # Local user management by the admin (accounts created from the UI,
    # HASHED passwords via werkzeug).
    # A group carries a default quota and admin right; a user can
    # override the quota. Login checks this table in addition to LDAP/SSO.
    db.executescript('''
        CREATE TABLE IF NOT EXISTS user_groups (
            name       TEXT PRIMARY KEY,
            max_budget INTEGER,
            is_admin   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            fullname      TEXT,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            group_name    TEXT,
            max_budget    INTEGER,
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL
        );
        -- Source(s) d'authentification observées par utilisateur (local/ldap/
        -- sso), enregistrées à chaque login. Permet de savoir COMMENT chaque
        -- compte se connecte, y compris les cumuls (ex. LDAP + SSO).
        CREATE TABLE IF NOT EXISTS user_sources (
            username   TEXT PRIMARY KEY,
            sources    TEXT NOT NULL DEFAULT '',
            fullname   TEXT,
            last_source TEXT,
            last_seen  TEXT
        );
        -- Comptes refusés à la connexion, QUELLE QUE SOIT leur source.
        -- `local_users.enabled` ne couvre que les comptes locaux : un compte
        -- LDAP ou SSO n'a aucune ligne ici, donc sans cette table un partant
        -- dont le compte annuaire survit pouvait se reconnecter indéfiniment —
        -- on ne pouvait que révoquer ses sessions, qu'il rouvrait aussitôt.
        CREATE TABLE IF NOT EXISTS blocked_users (
            username   TEXT PRIMARY KEY,
            reason     TEXT,
            blocked_by TEXT,
            blocked_at TEXT NOT NULL
        );
        -- Live in-flight in-app model requests (Playground/Support), one row per
        -- active request; powers the real-time "who's using the model" panel.
        CREATE TABLE IF NOT EXISTS inflight_requests (
            id         TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            started_at REAL NOT NULL,
            tokens     INTEGER NOT NULL DEFAULT 0,
            decode_since REAL
        );
        -- Compteurs du moteur conservés d'un lancement à l'autre : les métriques
        -- de llama.cpp/vLLM repartent de zéro à chaque démarrage, donc « tokens
        -- générés » retombait à 0 à chaque relance. `base` = total des lancements
        -- terminés, `dernier` = dernière valeur vue du lancement en cours ; une
        -- relance se reconnaît à un compteur qui REPART EN ARRIÈRE (il est
        -- monotone, il ne peut pas baisser autrement). Cf. stats.cumuler_tokens_generes.
        CREATE TABLE IF NOT EXISTS model_counters (
            model   TEXT PRIMARY KEY,
            base    INTEGER NOT NULL DEFAULT 0,
            dernier INTEGER NOT NULL DEFAULT 0,
            maj     REAL
        );
        -- ── Mémoire : graphe de connaissances par utilisateur ────────────────
        -- Un sujet dont le modèle a appris quelque chose (« vLLM », « DGX
        -- Spark »). `name_norm` est la forme normalisée qui sert à retrouver le
        -- nœud : sans elle, « vLLM » et « vllm » créeraient deux nœuds et le
        -- graphe se remplirait de doublons — le principal risque de ce design.
        CREATE TABLE IF NOT EXISTS memory_nodes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            name       TEXT NOT NULL,       -- libellé affiché, tel qu'écrit
            name_norm  TEXT NOT NULL,       -- clé de rapprochement (voir _mem_norm)
            kind       TEXT NOT NULL DEFAULT 'sujet',  -- sujet | personne | outil | préférence
            created_at TEXT NOT NULL,
            UNIQUE(username, name_norm)
        );
        -- Autres façons de nommer un même nœud (« le serveur d'inférence » →
        -- vLLM). Sans extension vectorielle en base, c'est notre seul moyen de
        -- rapprocher des formulations différentes.
        CREATE TABLE IF NOT EXISTS memory_aliases (
            node_id    INTEGER NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
            username   TEXT NOT NULL,
            alias_norm TEXT NOT NULL,
            PRIMARY KEY (username, alias_norm)
        );
        -- Un fait, porté par une arête. `dst_id` est NULL pour un fait qui ne
        -- relie pas deux sujets (« préfère les réponses courtes »).
        -- `valid_until` non NULL = fait périmé : conservé pour l'historique mais
        -- plus jamais injecté, ce qui évite d'accumuler des contradictions quand
        -- une information est remplacée par une plus récente.
        CREATE TABLE IF NOT EXISTS memory_edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            src_id      INTEGER NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
            relation    TEXT NOT NULL,
            dst_id      INTEGER REFERENCES memory_nodes(id) ON DELETE CASCADE,
            fact        TEXT NOT NULL,      -- le fait en clair, tel qu'injecté
            source      TEXT NOT NULL DEFAULT 'model',  -- model | user
            confidence  REAL NOT NULL DEFAULT 1.0,
            created_at  TEXT NOT NULL,
            valid_until TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mem_edges_user ON memory_edges(username, src_id);
        CREATE INDEX IF NOT EXISTS idx_mem_nodes_user ON memory_nodes(username, name_norm);

        -- Confirmation des actions sensibles du Support. Le modele ne decide
        -- JAMAIS seul d'une revocation de cle ou d'un arret de modele : il depose
        -- ici une demande, l'interface affiche un bouton, et l'action n'est
        -- executee qu'au clic (voir chat_routes.support_confirm). Le jeton n'est
        -- jamais transmis au modele — une injection indirecte ne peut donc pas le
        -- rejouer — et il est a USAGE UNIQUE, borne dans le temps.
        CREATE TABLE IF NOT EXISTS pending_actions (
            token       TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            tool        TEXT NOT NULL,
            args        TEXT NOT NULL,
            label       TEXT,
            target      TEXT,
            created_at  REAL NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'   -- pending | done | cancelled
        );
        CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_actions(username, created_at);

        -- Retour utilisateur sur une reponse du Support (pouce haut/bas +
        -- commentaire libre). Sert a savoir QUELLES reponses echouent : sans
        -- cela, on ne corrige le prompt qu'a l'intuition.
        CREATE TABLE IF NOT EXISTS support_feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            vote        INTEGER NOT NULL,                 -- 1 = utile, -1 = pas utile
            comment     TEXT,
            question    TEXT,
            answer      TEXT,
            model       TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_support_feedback_user ON support_feedback(username, id);

        -- Fil de discussion du Support, conserve cote serveur pour qu'un
        -- rechargement de page ne perde pas la conversation. Table DEDIEE : le
        -- Support n'a rien a faire dans l'historique du playground (prompt
        -- systeme, outils et intentions differents), donc surtout pas dans
        -- `conversations`, que le playground liste et rejoue.
        CREATE TABLE IF NOT EXISTS support_thread (
            username    TEXT PRIMARY KEY,
            messages    TEXT NOT NULL,
            updated_at  REAL NOT NULL
        );
    ''')
    # Migration: remember the admin status of the last observed login (local/
    # LDAP/SSO). The Users page uses it to show the effective role with the
    # directory (SSO/LDAP) taking precedence over the local record.
    us_cols = {r[1] for r in db.execute("PRAGMA table_info(user_sources)")}
    if 'last_is_admin' not in us_cols:
        db.execute("ALTER TABLE user_sources ADD COLUMN last_is_admin INTEGER")
    # Migration: IP and user-agent of sessions opened BEFORE their addition.
    # Existing rows keep NULL — the session list will show « inconnu » for
    # them, which is exact.
    share_cols = {r[1] for r in db.execute("PRAGMA table_info(conversation_shares)")}
    if 'username' not in share_cols:
        # Shares created before the column stay ownerless: they predate the
        # tracking, they cannot be attributed retroactively.
        db.execute("ALTER TABLE conversation_shares ADD COLUMN username TEXT")
    sess_cols = {r[1] for r in db.execute("PRAGMA table_info(user_sessions)")}
    for col in ('ip', 'user_agent'):
        if col not in sess_cols:
            db.execute(f"ALTER TABLE user_sessions ADD COLUMN {col} TEXT")
    # ── Hygiene sweep ───────────────────────────────────────────────────────
    # Three tables only grew and nobody cleaned them: ended sessions, login
    # failure counters whose window died long ago, and abandoned WebAuthn
    # challenges (TTL 5 min). Nothing ACTIVE is touched: a still valid
    # session is kept, and the audit log never is — it is the trace of admin
    # actions. Without this sweep, the session table doubled at every
    # deployment and reading a session (at EVERY kept request) stayed a
    # scan.
    now = time.time()
    for requete, args in (
            ("DELETE FROM user_sessions WHERE expires_at < ?", (now - 30 * 86400,)),
            ("DELETE FROM login_attempts WHERE first_at < ?", (now - 7 * 86400,)),
            ("DELETE FROM pending_webauthn WHERE expires_at < ?", (now,)),
    ):
        try:
            db.execute(requete, args)
        except sqlite3.Error:
            # Table absent from an older database: the sweep is no reason to fail
            # a startup.
            pass
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_budget', str(KEY_BUDGET))
    )
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_duration', KEY_DURATION)
    )
    db.commit()
    _detecte_base_reinitialisee(db)
    db.close()


# ── Reset-database detector ──────────────────────────────────────────────
# On 04/09/2026, the contents of portal.db were reset with no identified
# cause and nobody saw it for three days: the application "works" just as
# well on an empty database. Companion marker on the VOLUME (survives the
# container, dies with the data): present = the database once held
# accounts. Marker present + database empty again → critical + infra
# alert email.
DB_RESET_MARKER = os.path.join(os.path.dirname(DB_PATH), '.db_initialized')


def _detecte_base_reinitialisee(db):
    """Called at every init_db(): alerts if a database known as populated
    became empty again. The marker is only created ONCE the database is
    populated (at least one known account), never on a fresh install — a
    factory startup must not alert."""
    try:
        vide = (
            db.execute("SELECT COUNT(*) FROM user_sources").fetchone()[0] == 0
            and db.execute("SELECT COUNT(*) FROM local_users").fetchone()[0] == 0
            and db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        )
        marqueur_present = os.path.exists(DB_RESET_MARKER)
        if vide and marqueur_present:
            detail = ("portal.db est vide alors que le marqueur "
                      f"{DB_RESET_MARKER} atteste d'une base déjà peuplée — "
                      "réinitialisation suspectée (volume mal monté, db effacée).")
            print('[db] CRITIQUE : ' + detail)
            try:
                # Late import: notify depends on config, not on db — but do not weigh
                # down the core import for a rare alert path.
                from notify import notify_infra_alert_email
                notify_infra_alert_email("Portal database appears to have been reset", detail)
            except Exception as exc:
                print(f'[db] email anti-reset impossible : {exc}')
            return
        if not vide and not marqueur_present:
            with open(DB_RESET_MARKER, 'w') as fh:
                fh.write(datetime.now().isoformat() + '\n')
    except Exception as exc:
        # NEVER fail the startup on the detector itself.
        print(f'[db] détecteur anti-reset inopéré : {exc}')
