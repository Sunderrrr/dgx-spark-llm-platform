"""Acces a la base SQLite du portail.

Extrait de app.py le 28/08. C'est le NOYAU PARTAGE : sans lui, aucune autre
section du monolithe n'etait extractible, parce que presque toutes appellent
get_db() (103 appels) et qu'un module importe par app.py ne peut pas
reimporter app.py — cycle a l'import.

Ce module ne depend que de flask.g et de sqlite3 : il n'importe rien du
portail, donc tout le monde peut l'importer sans risque de cycle.
"""
import os
import sqlite3
from datetime import datetime

from flask import g

from config import KEY_BUDGET, KEY_DURATION, LITELLM_DB_URL

DB_PATH = '/app/data/portal.db'


def get_db():
    """Connexion SQLite liee au contexte d'application Flask.

    Une seule connexion par requete, refermee par close_db. Les traitements en
    FIL (generation d'image, video, jobs) n'ont pas de contexte Flask et
    ouvrent donc leur propre connexion avec sqlite3.connect(DB_PATH) — c'est
    voulu, pas un oubli.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """Enregistre par app.py via app.teardown_appcontext(close_db).

    Pas de decorateur ici : le decorateur exigerait l'objet `app`, donc un
    import de app.py, donc exactement le cycle que ce module existe pour eviter.
    """
    db = g.pop('db', None)
    if db:
        db.close()


# ── Reglages persistes (table `settings`) ────────────────────────────────────
# De simples enveloppes SQL sur get_db : leur place est dans le noyau, pas dans
# le monolithe, parce que plusieurs sections extraites en ont besoin.

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
# Lecture seule, pour les statistiques de consommation et les budgets de cles.
# Sa place est ici, avec l'acces SQLite : c'est le module des bases, et
# litellm_client comme les routes de statistiques en ont besoin.

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
# init_db cree les tables absentes et applique les migrations de colonnes. Il
# etait dans app.py sous la banniere « DB », avec is_admin_username qui, elle,
# releve de l'authentification et reste pour l'instant la-bas.
# Appele une fois a l'amorcage, depuis app.py.

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
            created_at REAL NOT NULL
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
    ''')
    # Migration: columns added to mcp_servers after its initial creation
    # (description, tool filter, enablement) — additive ALTER, lossless.
    pref_cols = {r[1] for r in db.execute("PRAGMA table_info(user_prefs)")}
    for col in ('theme_id', 'lang'):
        if col not in pref_cols:
            db.execute(f"ALTER TABLE user_prefs ADD COLUMN {col} TEXT")
    # Mémoire : désactivée par défaut (opt-in). C'est de la donnée personnelle,
    # donc personne n'en accumule sans l'avoir explicitement demandé.
    if 'memory_enabled' not in pref_cols:
        db.execute("ALTER TABLE user_prefs ADD COLUMN memory_enabled INTEGER NOT NULL DEFAULT 0")
    # Prise en main affichée une fois par COMPTE (et non par navigateur) : le
    # nouvel arrivant la voit quel que soit le poste, et ne la revoit jamais.
    if 'websearch_enabled' not in pref_cols:
        # Activée par défaut, contrairement à la mémoire : la recherche ne
        # conserve rien sur l'utilisateur. Le réglage sert à la couper pour qui
        # ne veut pas que ses questions atteignent des moteurs externes.
        db.execute("ALTER TABLE user_prefs ADD COLUMN websearch_enabled INTEGER NOT NULL DEFAULT 1")
    if 'onboarded' not in pref_cols:
        db.execute("ALTER TABLE user_prefs ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
    mcp_cols = {r[1] for r in db.execute("PRAGMA table_info(mcp_servers)")}
    for col, ddl in (('description', "TEXT DEFAULT ''"),
                     ('allowed_tools', "TEXT DEFAULT ''"),
                     ('enabled', "INTEGER NOT NULL DEFAULT 1")):
        if col not in mcp_cols:
            db.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col} {ddl}")
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
    # Plusieurs versions d'un même morceau (fichiers <job_id>_<idx>.wav) : même
    # principe que les images — count = demandé, done_count = déjà produit.
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
        -- Live in-flight in-app model requests (Playground/Support), one row per
        -- active request; powers the real-time "who's using the model" panel.
        CREATE TABLE IF NOT EXISTS inflight_requests (
            id         TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            started_at REAL NOT NULL
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
    ''')
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_budget', str(KEY_BUDGET))
    )
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_duration', KEY_DURATION)
    )
    ORNITH_ARGS = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 262144 --gpu-memory-utilization 0.7 --max-num-seqs 8"
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR IGNORE INTO model_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
        ("ornith-35b-fp8", "deepreinforce-ai/Ornith-1.0-35B-FP8", ORNITH_ARGS, now)
    )
    # Always update the args of the pre-configured model
    db.execute("UPDATE model_configs SET hf_model_id=?, vllm_args=? WHERE name=?",
               ("deepreinforce-ai/Ornith-1.0-35B-FP8", ORNITH_ARGS, "ornith-35b-fp8"))
    db.commit()
    db.close()
