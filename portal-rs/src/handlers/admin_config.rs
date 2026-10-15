//! Admin → Config (admin_config.py): the instance settings (a secret is only
//! ever reported as configured or absent), each section's « Tester », the
//! full validation run on demand, and the storage page.
use super::*;
use super::admin_routes::admin_guard;
use super::admin_writes::{admin_form, json_error};

/// `instance_config.CONFIGURABLE`: (name, label, secret, code default).
const CONFIGURABLE: [(&str, &str, bool, &str); 33] = [
    ("ldap_uri", "LDAP — URI", false, ""),
    ("ldap_base", "LDAP — base DN", false, "dc=example,dc=org"),
    ("ldap_users_dn", "LDAP — RDN des comptes", false, "ou=users"),
    ("ldap_login_attr", "LDAP — attribut de connexion", false, "cn"),
    ("ldap_bind_dn", "LDAP — compte de service (DN)", false, ""),
    ("ldap_bind_pw", "LDAP — mot de passe du compte de service", true, ""),
    ("oidc_metadata_url", "SSO — metadata URL", false, ""),
    ("oidc_client_id", "SSO — client ID", false, ""),
    ("oidc_client_secret", "SSO — client secret", true, ""),
    ("oidc_admin_group", "SSO — groupe administrateur", false, "admins"),
    ("oidc_redirect_uri", "SSO — URI de retour", false, ""),
    ("oidc_logout_url", "SSO — URL de déconnexion", false, ""),
    ("smtp_host", "SMTP — hôte", false, ""),
    ("smtp_port", "SMTP — port", false, "587"),
    ("smtp_user", "SMTP — utilisateur", false, ""),
    ("smtp_pass", "SMTP — mot de passe", true, ""),
    ("smtp_from", "SMTP — adresse expéditrice", false, ""),
    ("admin_email", "SMTP — destinataire des alertes", false, ""),
    ("discord_webhook_url", "Discord — webhook des alertes admin", true, ""),
    ("discord_bot_token", "Discord — jeton du bot (messages privés)", true, ""),
    ("discord_client_id", "Discord — client ID (liaison des comptes)", false, ""),
    ("discord_client_secret", "Discord — client secret", true, ""),
    ("discord_redirect_uri", "Discord — URI de retour", false, ""),
    ("security_contact", "Sécurité — contact (security.txt)", false, ""),
    ("s3_endpoint", "S3 — endpoint", false, ""),
    ("s3_region", "S3 — région", false, "us-east-1"),
    ("s3_bucket", "S3 — bucket", false, ""),
    ("s3_prefix", "S3 — préfixe", false, ""),
    ("s3_access_key", "S3 — clé d'accès", false, ""),
    ("s3_secret_key", "S3 — clé secrète", true, ""),
    ("ranking_price_input", "Classement — prix en entrée (par million de tokens)", false, "2.50"),
    ("ranking_price_output", "Classement — prix en sortie (par million de tokens)", false, "10.00"),
    ("ranking_price_currency", "Classement — devise", false, "USD"),
];

/// Sent by the panel to clear a secret (an empty secret field means unchanged).
const CLEAR: &str = "__effacer__";

fn web() -> String {
    std::env::var("DGX_WEB").unwrap_or_else(|_| "https://example.org".into()).trim_end_matches('/').to_string()
}

async fn stored(pool: &sqlx::SqlitePool, name: &str) -> String {
    sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key=?").bind(format!("cfg_{name}")).fetch_optional(pool).await.ok().flatten().unwrap_or_default()
}

/// GET|POST /admin/config - the settings and where each comes from; a POST
/// writes the fields present (empty = back to the default; a secret only
/// changes when one is typed, or is cleared by `__effacer__`).
pub async fn admin_config_get(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, "/admin/config").await {
        return r;
    }
    let pool = state.db.pool();
    let mut config = Vec::new();
    for (name, label, secret, default) in CONFIGURABLE {
        let db_value = stored(pool, name).await;
        let v = if db_value.is_empty() { default.to_string() } else { db_value.clone() };
        config.push(json!({
            "name": name, "label": label, "secret": secret,
            "value": if secret { String::new() } else { v.clone() },
            "configure": !v.is_empty(),
            "source": if db_value.is_empty() { "défaut" } else { "interface" },
            "default": if secret { "" } else { default },
        }));
    }
    let fields: Vec<Value> = CONFIGURABLE.iter().map(|(n, l, s, _)| json!({"name": n, "label": l, "secret": s})).collect();
    let w = web();
    Json(json!({
        "ok": true, "config": config, "fields": fields,
        "suggestions": {
            "oidc_redirect_uri": format!("{w}/api/oauth2-redirect"),
            "oidc_post_logout": format!("{w}/"),
            "discord_redirect_uri": format!("{w}/discord/callback"),
        },
    }))
    .into_response()
}

pub async fn admin_config_post(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/config").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let mut changed = 0;
    for (name, _, secret, _) in CONFIGURABLE {
        let Some(raw) = form.get(name) else { continue };
        changed += 1;
        let value = crate::memory::py_strip(raw).to_string();
        // instance_config.set_value
        if secret && value.is_empty() {
            continue;
        }
        let key = format!("cfg_{name}");
        if !value.is_empty() && value != CLEAR {
            let _ = sqlx::query("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(&key).bind(&value).execute(pool).await;
        } else {
            let _ = sqlx::query("DELETE FROM settings WHERE key=?").bind(&key).execute(pool).await;
        }
    }
    crate::sessions::log_audit(pool, &s.username, "config.modifiee", &format!("{changed} valeur(s) — LDAP/SSO/SMTP/sécurité")).await;
    Json(json!({"ok": true, "modifies": changed})).into_response()
}

/// `_test_sso()`: the provider's metadata (a browser User-Agent: some IdPs
/// behind Cloudflare refuse others).
async fn test_sso(pool: &sqlx::SqlitePool) -> (bool, String) {
    let url = stored(pool, "oidc_metadata_url").await;
    if url.is_empty() {
        return (false, "Aucune URL de métadonnées renseignée.".into());
    }
    let md: Value = match crate::sidecars::http()
        .get(&url)
        .header(reqwest::header::USER_AGENT, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
    {
        Ok(r) => r.json().await.unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    };
    let missing: Vec<&str> = ["issuer", "authorization_endpoint", "token_endpoint"].into_iter().filter(|k| !md.get(*k).is_some_and(keys::truthy)).collect();
    if !missing.is_empty() {
        return (false, format!("Métadonnées illisibles ou incomplètes ({} absent) : vérifie l'URL.", missing.join(", ")));
    }
    let issuer = md["issuer"].as_str().map(str::to_string).unwrap_or_else(|| md["issuer"].to_string());
    if stored(pool, "oidc_client_id").await.is_empty() || stored(pool, "oidc_client_secret").await.is_empty() {
        return (false, format!("Fournisseur joignable ({issuer}), mais client ID et secret sont requis."));
    }
    (true, format!("Fournisseur joignable : {issuer}."))
}

async fn test_smtp(pool: &sqlx::SqlitePool) -> (bool, String) {
    let c = crate::mailer::smtp_config(pool).await;
    if !c.configured() {
        return (false, "Hôte, utilisateur et mot de passe SMTP sont requis.".into());
    }
    if c.admin_email.is_empty() {
        return (false, "Renseigne le destinataire des alertes : le test lui est envoyé.".into());
    }
    if crate::mailer::test(pool).await {
        return (true, format!("E-mail de test envoyé à {}.", c.admin_email));
    }
    (false, "L'envoi a échoué : vérifie l'hôte, le port et les identifiants.".into())
}

async fn test_discord(pool: &sqlx::SqlitePool) -> (bool, String) {
    let hook = stored(pool, "discord_webhook_url").await;
    if hook.is_empty() {
        return (false, "Aucun webhook renseigné.".into());
    }
    let ok = crate::sidecars::http()
        .post(&hook)
        .json(&json!({"content": "Test du webhook depuis le portail : les alertes admin arriveront ici."}))
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await
        .is_ok_and(|r| r.status().is_success());
    if ok { (true, "Message de test publié sur le webhook.".into()) } else { (false, "Le webhook a refusé le message : vérifie son URL.".into()) }
}

/// `object_store.test()`: write, read back and delete a probe object.
async fn test_s3(state: &AppState) -> (bool, String) {
    let Some(cfg) = crate::s3::Config::load(&state.db).await else {
        return (false, "Endpoint, bucket et clés S3 sont requis.".into());
    };
    let probe = cfg.key(&[".dgx-probe"]);
    if let Err(e) = crate::s3::put(&cfg, &probe, b"ok".to_vec(), "text/plain").await {
        return (false, format!("Le bucket refuse l'accès : {e}").chars().take(300).collect());
    }
    let found = crate::s3::exists(&cfg, &probe).await;
    if let Err(e) = crate::s3::delete(&cfg, &probe).await {
        return (false, format!("Le bucket refuse l'accès : {e}").chars().take(300).collect());
    }
    if !found {
        return (false, "Objet écrit mais introuvable à la relecture : vérifie le bucket.".into());
    }
    (true, format!("Bucket « {} » accessible en lecture et en écriture.", cfg.bucket))
}

/// POST /admin/config/test/<part> - « Tester » on the SAVED values; a failed
/// test is an answer (200).
pub async fn admin_config_test(State(state): State<AppState>, Path(part): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/config/test/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let (ok, message) = match part.as_str() {
        "ldap" => crate::ldap::test(pool).await,
        "sso" => test_sso(pool).await,
        "smtp" => test_smtp(pool).await,
        "discord" => test_discord(pool).await,
        "s3" => test_s3(&state).await,
        _ => return json_error("Section inconnue.", StatusCode::NOT_FOUND),
    };
    crate::sessions::log_audit(pool, &s.username, "config.test", &format!("{part} : {}", if ok { "ok" } else { "échec" })).await;
    Json(json!({"ok": ok, "message": message})).into_response()
}

/// The validation run watched from here: one at a time.
struct Validation {
    running: bool,
    start: Option<f64>,
    output: Vec<String>,
    code: Option<i64>,
}

static VALIDATION: std::sync::Mutex<Validation> = std::sync::Mutex::new(Validation { running: false, start: None, output: Vec::new(), code: None });

fn epoch_now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn validation_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var("DGX_VALIDATION_DIR").unwrap_or_else(|_| "/run/dgx/validation".into()))
}

/// `tail -F`: the file's last 10 lines, then what it grows by (a truncated
/// or replaced file is read again from its start).
async fn follow(path: std::path::PathBuf, mut on_line: impl FnMut(String) -> bool) {
    use tokio::io::{AsyncReadExt, AsyncSeekExt};
    let started = epoch_now();
    let mut pos: u64 = 0;
    let mut first = true;
    let mut pending = String::new();
    loop {
        if epoch_now() - started > 1200.0 {
            return;
        }
        if let Ok(mut f) = tokio::fs::File::open(&path).await {
            let len = f.metadata().await.map(|m| m.len()).unwrap_or(0);
            if len < pos {
                pos = 0;
            }
            let mut buf = Vec::new();
            if f.seek(std::io::SeekFrom::Start(pos)).await.is_ok() && f.read_to_end(&mut buf).await.is_ok() {
                pos += buf.len() as u64;
                pending.push_str(&String::from_utf8_lossy(&buf));
                let mut lines: Vec<String> = Vec::new();
                while let Some(i) = pending.find('\n') {
                    lines.push(pending[..i].trim_end_matches('\r').to_string());
                    pending.drain(..=i);
                }
                if first {
                    let skip = lines.len().saturating_sub(10);
                    lines.drain(..skip);
                    first = false;
                }
                for l in lines {
                    if !on_line(l) {
                        return;
                    }
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}

/// POST /admin/validate - the full validation pipeline on demand. The host
/// runs it (a request file picked up by a systemd .path unit); its log is
/// followed here until the verdict.
pub async fn admin_validate(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    if let Err(r) = admin_form(&state, &headers, &body, "/admin/validate").await {
        return r;
    }
    {
        let mut v = VALIDATION.lock().unwrap_or_else(|e| e.into_inner());
        if v.running {
            return (StatusCode::CONFLICT, Json(json!({"ok": false, "error": "Une vérification est déjà en cours."}))).into_response();
        }
        *v = Validation { running: true, start: Some(epoch_now()), output: Vec::new(), code: None };
    }
    tokio::spawn(async move {
        let dir = validation_dir();
        let wrote = async {
            tokio::fs::create_dir_all(&dir).await?;
            tokio::fs::write(dir.join("demande.txt"), epoch_now().to_string()).await
        }
        .await;
        if let Err(e) = wrote {
            let mut v = VALIDATION.lock().unwrap_or_else(|e| e.into_inner());
            v.output.push(format!("Erreur: {e}"));
            v.code = Some(1);
            v.running = false;
            return;
        }
        follow(dir.join("courante.log"), |line| {
            let mut v = VALIDATION.lock().unwrap_or_else(|e| e.into_inner());
            let done = line.contains("OVERALL VERDICT") || line.contains("VERDICT GLOBAL");
            v.output.push(line);
            if v.output.len() > 400 {
                let extra = v.output.len() - 400;
                v.output.drain(..extra);
            }
            !done
        })
        .await;
        let mut v = VALIDATION.lock().unwrap_or_else(|e| e.into_inner());
        v.code = Some(if v.output.iter().any(|x| x.contains("ALL GREEN") || x.contains("TOUT EST VERT")) { 0 } else { 1 });
        v.running = false;
    });
    Json(json!({"ok": true})).into_response()
}

/// GET /admin/validate/state - progress, the output tail and the verdict.
pub async fn admin_validate_state(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, "/admin/validate/state").await {
        return r;
    }
    let v = VALIDATION.lock().unwrap_or_else(|e| e.into_inner());
    let tail: Vec<&String> = v.output.iter().skip(v.output.len().saturating_sub(120)).collect();
    Json(json!({
        "ok": true,
        "running": v.running,
        "duration_s": v.start.map_or(0, |s| (epoch_now() - s).round() as i64),
        "code": v.code,
        "output": tail,
        "passed": v.code.map(|c| c == 0),
    }))
    .into_response()
}

/// `backups._keep(name)`: 1..=365, 7 by default or on a bad value.
async fn keep(pool: &sqlx::SqlitePool, name: &str) -> i64 {
    let v = sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key=?").bind(name).fetch_optional(pool).await.ok().flatten().unwrap_or_default();
    let v = if v.is_empty() { "7".to_string() } else { v };
    super::support_routes::py_int(&Value::String(v)).map_or(7, |n| n.clamp(1, 365))
}

/// GET /admin/storage - where files go, what they weigh (here and in the
/// bucket), the migration, and the backups' settings and last run.
pub async fn admin_storage(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, "/admin/storage").await {
        return r;
    }
    let pool = state.db.pool();
    let s3 = crate::s3::Config::load(&state.db).await;
    let chosen = state.db.get_setting("media_storage").await.ok().flatten().unwrap_or_default();
    let location = if chosen == "s3" && s3.is_some() { "s3" } else { "local" };
    let root = crate::media_store::root();
    let mut local = serde_json::Map::new();
    for kind in crate::media_store::KINDS {
        let (mut n, mut size) = (0u64, 0u64);
        if let Ok(rd) = std::fs::read_dir(std::path::Path::new(&root).join(kind)) {
            for e in rd.flatten() {
                if let Ok(m) = e.metadata() {
                    if m.is_file() {
                        n += 1;
                        size += m.len();
                    }
                }
            }
        }
        local.insert(kind.into(), json!({"files": n, "bytes": size}));
    }
    let mut s3_usage = Value::Null;
    if let Some(cfg) = &s3 {
        match crate::s3::list_keys(cfg, &cfg.key(&["media"])).await {
            Ok(objects) => {
                let mut by_kind: serde_json::Map<String, Value> = crate::media_store::KINDS.iter().map(|k| (k.to_string(), json!({"files": 0, "bytes": 0}))).collect();
                for (k, size, _) in objects {
                    let mut parts = k.rsplitn(3, '/');
                    let _file = parts.next();
                    if let Some(kind) = parts.next() {
                        if let Some(e) = by_kind.get_mut(kind) {
                            e["files"] = json!(e["files"].as_u64().unwrap_or(0) + 1);
                            e["bytes"] = json!(e["bytes"].as_u64().unwrap_or(0) + size);
                        }
                    }
                }
                s3_usage = Value::Object(by_kind);
            }
            Err(e) => eprintln!("[media_store] S3 listing failed: {e}"),
        }
    }
    let migration = std::fs::read_to_string(std::path::Path::new(&root).join(".media_migration.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .unwrap_or_else(|| json!({"running": false}));
    let destination = state.db.get_setting("backup_destination").await.ok().flatten().filter(|d| ["local", "s3", "both"].contains(&d.as_str())).unwrap_or_else(|| "local".into());
    let last = std::fs::read_to_string(std::env::var("BACKUP_STATE_FILE").unwrap_or_else(|_| "/var/backups/dgx/state.json".into()))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok());
    Json(json!({
        "ok": true,
        "s3_ready": s3.is_some(),
        "media_storage": location,
        "media_usage": {"local": local, "s3": s3_usage},
        "migration": migration,
        "backup": {"destination": destination, "keep_local": keep(pool, "backup_keep_local").await, "keep_s3": keep(pool, "backup_keep_s3").await, "s3_ready": s3.is_some()},
        "last_backup": last,
        "max_keep": 365,
    }))
    .into_response()
}

fn media_status_path() -> std::path::PathBuf {
    std::path::Path::new(&crate::media_store::root()).join(".media_migration.json")
}

/// `_write_status(st)`: written whole then renamed, as Flask reads it.
fn write_status(st: &Value) {
    let path = media_status_path();
    let tmp = path.with_extension("json.tmp");
    if std::fs::write(&tmp, st.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, &path);
    }
}

/// The migration lock, shared with Flask's workers (flock on the data volume).
struct MigrationLock(std::fs::File);

impl MigrationLock {
    fn take() -> Option<Self> {
        use std::os::unix::io::AsRawFd;
        let root = crate::media_store::root();
        let _ = std::fs::create_dir_all(&root);
        let f = std::fs::OpenOptions::new().create(true).truncate(false).read(true).write(true).open(std::path::Path::new(&root).join(".media_migration.lock")).ok()?;
        // SAFETY: flock on a file descriptor this struct owns.
        (unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0).then_some(Self(f))
    }
}

impl Drop for MigrationLock {
    fn drop(&mut self) {
        use std::os::unix::io::AsRawFd;
        // SAFETY: the descriptor is still open (closed right after, with the file).
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// One file moved to `target`; the source is deleted only once the copy
/// is checked (same size). Returns the bytes moved.
async fn migrate_one(cfg: &crate::s3::Config, target: &str, kind: &str, name: &str, listed: u64) -> Result<u64, String> {
    let path = crate::media_store::local_path(kind, name);
    let key = cfg.key(&["media", kind, name]);
    if target == "s3" {
        let data = tokio::fs::read(&path).await.map_err(|e| e.to_string())?;
        let size = data.len() as u64;
        crate::s3::put(cfg, &key, data, "application/octet-stream").await?;
        if crate::s3::size(cfg, &key).await != Some(size) {
            return Err(format!("size mismatch after upload of {kind}/{name}"));
        }
        tokio::fs::remove_file(&path).await.map_err(|e| e.to_string())?;
        return Ok(size);
    }
    let crate::s3::Get::Found(r) = crate::s3::get(cfg, &key, None).await? else {
        return Err("object missing".into());
    };
    let bytes = r.bytes().await.map_err(|e| e.to_string())?;
    if bytes.len() as u64 != listed {
        return Err("size mismatch after download".into());
    }
    let dir = std::path::Path::new(&path).parent().map(std::path::Path::to_path_buf).unwrap_or_default();
    tokio::fs::create_dir_all(&dir).await.map_err(|e| e.to_string())?;
    let tmp = dir.join(format!(".mig-{name}"));
    tokio::fs::write(&tmp, &bytes).await.map_err(|e| e.to_string())?;
    tokio::fs::rename(&tmp, &path).await.map_err(|e| e.to_string())?;
    crate::s3::delete(cfg, &key).await?;
    Ok(listed)
}

/// POST /admin/storage/migrate - every existing file moved to `target`, in
/// the background; files stay readable meanwhile (looked up in both places).
pub async fn admin_storage_migrate(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/storage/migrate").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let target = form.get("target").cloned().unwrap_or_default();
    let refuse = |msg: &str, code| json_error(msg, code);
    if target != "local" && target != "s3" {
        return refuse("Destination inconnue.", StatusCode::BAD_REQUEST);
    }
    let Some(cfg) = crate::s3::Config::load(&state.db).await else {
        return refuse("Configure d'abord le stockage S3 (Admin → Config).", StatusCode::BAD_REQUEST);
    };
    let Some(lock) = MigrationLock::take() else {
        return refuse("Une migration est déjà en cours.", StatusCode::CONFLICT);
    };
    let mut st = json!({"running": true, "target": target, "total": 0, "done": 0, "failed": 0, "bytes": 0, "started": epoch_now(), "finished": null, "last_error": ""});
    write_status(&st);
    crate::sessions::log_audit(state.db.pool(), &s.username, "stockage.migration", &format!("vers {target}")).await;
    let answer = st.clone();
    tokio::spawn(async move {
        let _lock = lock;
        let mut todo: Vec<(String, String, u64)> = Vec::new();
        if target == "s3" {
            for kind in crate::media_store::KINDS {
                if let Ok(rd) = std::fs::read_dir(std::path::Path::new(&crate::media_store::root()).join(kind)) {
                    for e in rd.flatten() {
                        if e.metadata().is_ok_and(|m| m.is_file()) {
                            todo.push((kind.to_string(), e.file_name().to_string_lossy().into_owned(), 0));
                        }
                    }
                }
            }
        } else {
            match crate::s3::list_keys(&cfg, &cfg.key(&["media"])).await {
                Ok(keys) => {
                    for (k, size, _) in keys {
                        let mut parts = k.rsplitn(3, '/');
                        let (Some(name), Some(kind)) = (parts.next(), parts.next()) else { continue };
                        todo.push((kind.to_string(), name.to_string(), size));
                    }
                }
                Err(e) => st["last_error"] = json!(e.chars().take(300).collect::<String>()),
            }
        }
        st["total"] = json!(todo.len());
        for (kind, name, listed) in todo {
            match migrate_one(&cfg, &target, &kind, &name, listed).await {
                Ok(n) => {
                    st["done"] = json!(st["done"].as_u64().unwrap_or(0) + 1);
                    st["bytes"] = json!(st["bytes"].as_u64().unwrap_or(0) + n);
                }
                Err(e) => {
                    st["failed"] = json!(st["failed"].as_u64().unwrap_or(0) + 1);
                    st["last_error"] = json!(format!("{kind}/{name}: {e}").chars().take(300).collect::<String>());
                }
            }
            write_status(&st);
        }
        st["running"] = json!(false);
        st["finished"] = json!(epoch_now());
        write_status(&st);
    });
    Json(json!({"ok": true, "migration": answer})).into_response()
}
