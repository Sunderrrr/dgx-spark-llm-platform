//! The Support assistant's catalogue (support.py): its system prompt, the
//! context about the logged-in account (never anyone else's), its tools and
//! their execution — always for the session's user. Sensitive actions are only
//! PROPOSED: a token goes to the UI, and the click executes (`/support/confirm`).
//! The admin tools live in `support_admin`, offered to admin sessions only.
use serde_json::{json, Value};
use sqlx::SqlitePool;

use crate::chat::support_admin;
use crate::AppState;

const SUPPORT_FAQ: &str = "FAQ plateforme __PLATFORM__ :\n\
- Plateforme IA interne et GRATUITE (pas de facturation, pas de plan payant).\n\
- API compatible OpenAI. Endpoint public : configuré dans « Mes clés API ».\n\
- Budget PAR COMPTE, partagé par toutes les clés d'un même utilisateur, réinitialisé chaque jour. Le quota compte les vrais tokens : 1 token de prompt = 1, 1 token généré = 1.\n\
- Obtenir plus de budget : demande envoyée à un admin (bouton « Demander plus de tokens » ou via toi, __PLATFORM__). Un admin valide.\n\
- Demander un nouveau modèle : via la page « Demander un modèle » (identifiant Hugging Face) ou via toi ; un admin le valide puis le lance.\n\
- Intégrations : OpenCode, Hermes Agent, Codex, Aider, Cursor, Continue, Python/cURL — snippets prêts sur « Mes clés API ».\n\
- Un seul modèle tourne à la fois sur le GPU (mémoire unifiée du DGX Spark).";

pub const SUPPORT_SYSTEM: &str = "Tu es __PLATFORM__, l'assistant IA de la plateforme __PLATFORM__ (NVIDIA DGX Spark, auto-hébergée). Tu aides les utilisateurs en français, de façon concise et concrète, sur les clés API, le budget/quota, les intégrations, l'accès aux modèles et le dépannage.\n\
Tu peux AGIR pour l'utilisateur via des outils (tools) — toujours au nom du compte connecté, jamais pour quelqu'un d'autre :\n\
- create_api_key : créer une clé API.\n\
- revoke_api_key : supprimer une de ses clés (DESTRUCTIF).\n\
- request_budget : déposer une demande d'augmentation de budget.\n\
- request_model : demander l'ajout d'un modèle (identifiant Hugging Face).\n\
- launch_model / stop_model : (admin uniquement) piloter le modèle du GPU.\n\
Règles d'usage des outils :\n\
- N'appelle un outil QUE pour une action explicitement demandée (créer/révoquer une clé, demander du budget/un modèle, lancer/arrêter), ou pour LIRE les logs d'un service en cas de panne (read_logs). Pour toute question d'information ou d'explication, réponds DIRECTEMENT en texte.\n\
- Les actions SENSIBLES (revoke_api_key, launch_model, stop_model) te répondent « NON EXÉCUTÉ : en attente de confirmation » : c'est normal. Le système affiche alors un bouton Confirmer à l'utilisateur, et l'action ne part qu'à son clic. Ne la rappelle pas, ne la présente jamais comme faite, et n'invente pas son résultat : dis en une phrase ce qui va se passer s'il confirme.\n\
- create_api_key et request_* s'exécutent directement — SAUF create_api_key si tu as lu un contenu externe (MCP/compétence) dans le même tour : elle est alors REFUSÉE, et c'est à l'utilisateur de créer sa clé depuis l'interface.\n\
- Quand tu crées une clé, AFFICHE la clé complète une seule fois à l'utilisateur (c'est sa nouvelle clé) et rappelle-lui de la copier.\n\
Règles générales :\n\
- Appuie-toi sur le CONTEXTE et la FAQ fournis. N'invente rien (ni plan payant, ni page de facturation, ni fonctionnalité inexistante).\n\
- Les clés du CONTEXTE sont MASQUÉES : ne tente jamais d'en reconstituer une.\n\
- IMPORTANT : réponds DIRECTEMENT, en français, sans montrer ton raisonnement ni de préambule interne. Va droit au but.";

pub const GUARDED_TOOLS: [&str; 4] = ["revoke_api_key", "launch_model", "stop_model", "manage_service"];
pub const SERVICE_KINDS: [&str; 5] = ["ocr", "video", "voice", "asr", "image"];
const PENDING_TTL: f64 = 600.0;
const PENDING_MAX: i64 = 5;

/// `TOOLS_REFUSED_IF_EXTERNAL`: the guarded tools, and the one that delivers a secret.
pub fn refused_if_external(name: &str) -> bool {
    GUARDED_TOOLS.contains(&name) || name == "create_api_key"
}

pub async fn platform_name(pool: &SqlitePool) -> String {
    let v: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key='platform_name'").fetch_optional(pool).await.ok().flatten();
    let v = v.map(|s| crate::memory::py_strip(&s).to_string()).unwrap_or_default();
    if v.is_empty() { "DGX".into() } else { v }
}

pub async fn support_text(pool: &SqlitePool, text: &str) -> String {
    text.replace("__PLATFORM__", &platform_name(pool).await)
}

/// Python's `"{:,.0f}".format(x).replace(',', ' ')`.
pub fn grouped(x: f64) -> String {
    let s = format!("{x:.0}");
    let (sign, digits) = s.strip_prefix('-').map_or(("", s.as_str()), |d| ("-", d));
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(' ');
        }
        out.push(c);
    }
    format!("{sign}{out}")
}

fn mask_key(k: &str) -> String {
    let n = k.chars().count();
    if n > 12 { format!("{}…{}", k.chars().take(6).collect::<String>(), k.chars().skip(n - 4).collect::<String>()) } else { "—".into() }
}

/// `_sidecar_status`: the container's state, « running » only once the service answers.
pub async fn sidecar_status(kind: &str) -> String {
    let proc = crate::sidecars::proc_status(kind).await;
    if proc != "running" {
        return proc;
    }
    let ready = match kind {
        "ocr" => crate::sidecars::ocr_model().await.is_some(),
        "video" => crate::sidecars::comfyui_up().await,
        "voice" => crate::sidecars::voice_model().await.is_some(),
        "asr" => crate::media::asr_info().await.get("loaded").is_some_and(crate::keys::truthy),
        "image" => crate::sidecars::image_ready().await,
        _ => false,
    };
    if ready {
        return "running".into();
    }
    let info = crate::media::asr_info().await;
    let failed = kind == "asr" && !info.get("loaded").is_some_and(crate::keys::truthy) && info.get("error").is_some_and(crate::keys::truthy);
    if failed { "failed".into() } else { "starting".into() }
}

static SERVICES: std::sync::Mutex<Option<(std::time::Instant, Vec<(String, String)>)>> = std::sync::Mutex::new(None);

/// `services_state`: every service's state, cached 15 s.
async fn services_state() -> Vec<(String, String)> {
    if let Some((at, s)) = SERVICES.lock().unwrap_or_else(|e| e.into_inner()).clone() {
        if at.elapsed() < std::time::Duration::from_secs(15) {
            return s;
        }
    }
    let mut state = Vec::new();
    for kind in SERVICE_KINDS {
        state.push((kind.to_string(), sidecar_status(kind).await));
    }
    *SERVICES.lock().unwrap_or_else(|e| e.into_inner()) = Some((std::time::Instant::now(), state.clone()));
    state
}

/// `_support_context`.
pub async fn context(state: &AppState, username: &str, is_admin: bool) -> String {
    let pool = state.db.pool();
    let mut lines = vec![format!("Utilisateur connecté : {username}{}", if is_admin { " (admin)" } else { "" })];
    let acct = crate::keys::account_info(username).await;
    if is_admin {
        lines.push("Budget du compte : illimité (admin).".into());
    } else if acct.exists {
        if let Some(b) = acct.max_budget {
            let mut l = format!("Budget du compte : {} / {} tokens utilisés", grouped(acct.spend), grouped(b));
            if !acct.budget_reset_at.is_empty() {
                l.push_str(&format!(" (reset {})", acct.budget_reset_at.chars().take(10).collect::<String>()));
            }
            lines.push(l);
        }
    }
    let keys = crate::keys::user_keys(pool, state.spend.as_ref(), username).await;
    let keys = keys.as_array().cloned().unwrap_or_default();
    if keys.is_empty() {
        lines.push("L'utilisateur n'a aucune clé API pour l'instant.".into());
    } else {
        lines.push("Clés de l'utilisateur (masquées, alias = identifiant pour les actions) :".into());
        for k in &keys {
            let alias = k.get("key_alias").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| "None".into());
            lines.push(format!("  - {alias} : {}, dépensé {}", mask_key(k.get("key").and_then(Value::as_str).unwrap_or("")), grouped(k.get("spend").and_then(Value::as_f64).unwrap_or(0.0))));
        }
    }
    let u = crate::home::user_hourly(state.spend.as_ref(), username).await;
    if u.get("has_data").is_some_and(crate::keys::truthy) {
        lines.push(format!("Conso aujourd'hui : {} tokens réels (pic vers {}h).", grouped(u.get("total").and_then(Value::as_f64).unwrap_or(0.0)), crate::chat::history::py_str(u.get("peak_hour").unwrap_or(&Value::Null))));
    }
    let running = crate::models::running_models().await;
    let rows: Vec<(Option<String>, Option<String>, Option<String>)> = sqlx::query_as("SELECT name, vllm_args, engine FROM model_configs ORDER BY name").fetch_all(pool).await.unwrap_or_default();
    let cat: Vec<String> = rows.iter().map(|(name, args, engine)| {
        let name = name.clone().unwrap_or_default();
        let eng = engine.as_deref().filter(|e| !e.is_empty()).unwrap_or("vllm");
        let args = args.as_deref().unwrap_or("");
        let ctx = crate::health::effective_ctx(args, eng).filter(|c| *c != 0);
        let tools = matches!(eng, "llamacpp" | "ds4") || args.contains("--tool-call-parser") || args.contains("--enable-auto-tool-choice") || args.contains("--jinja");
        format!("  - {name}{} : contexte {}, tool-calling {}", if running.contains(&name) { " [ACTIVE]" } else { "" },
            ctx.map(|c| grouped(c as f64)).unwrap_or_else(|| "?".into()), if tools { "oui" } else { "non" })
    }).collect();
    if !cat.is_empty() {
        lines.push(format!("Catalogue des modèles (le [ACTIVE] est celui chargé sur le GPU) :\n{}", cat.join("\n")));
    }
    let st = crate::system::runner_status().await;
    lines.push(format!("Runner vLLM : {}{}", st.get("status").and_then(Value::as_str).unwrap_or("?"), if running.is_empty() { " — aucun modèle chargé" } else { "" }));
    let services: Vec<String> = services_state().await.iter().map(|(k, v)| format!("  - {k} : {v}")).collect();
    lines.push(format!("Services de la plateforme (running = prêt à servir, starting = en chargement, stopped = arrêté, failed = en échec de chargement) :\n{}", services.join("\n")));
    let mreqs: Vec<(Option<String>, Option<String>)> = sqlx::query_as("SELECT model_id, status FROM model_requests WHERE username=? ORDER BY created_at DESC LIMIT 5").bind(username).fetch_all(pool).await.unwrap_or_default();
    if !mreqs.is_empty() {
        lines.push(format!("Demandes de modèle de l'utilisateur : {}", mreqs.iter().map(|(m, s)| format!("{} ({})", m.as_deref().unwrap_or("None"), s.as_deref().unwrap_or("None"))).collect::<Vec<_>>().join(", ")));
    }
    let breqs: Vec<Option<String>> = sqlx::query_scalar("SELECT status FROM budget_requests WHERE username=? ORDER BY created_at DESC LIMIT 3").bind(username).fetch_all(pool).await.unwrap_or_default();
    if !breqs.is_empty() {
        lines.push(format!("Demandes de budget de l'utilisateur : {}", breqs.iter().map(|s| s.clone().unwrap_or_else(|| "None".into())).collect::<Vec<_>>().join(", ")));
    }
    let acts: Vec<(Option<String>, Option<String>, Option<String>)> = sqlx::query_as("SELECT action, detail, created_at FROM audit_log WHERE username=? ORDER BY id DESC LIMIT 5").bind(username).fetch_all(pool).await.unwrap_or_default();
    if !acts.is_empty() {
        let l: Vec<String> = acts.iter().map(|(a, d, c)| format!("  - {} : {}",
            c.as_deref().unwrap_or("").chars().take(16).collect::<String>().replace('T', " "),
            d.as_deref().filter(|x| !x.is_empty()).or(a.as_deref()).unwrap_or("").chars().take(160).collect::<String>())).collect();
        lines.push(format!("Ses dernières actions (plus récente d'abord) :\n{}", l.join("\n")));
    }
    if is_admin {
        lines.push("Les logs des services ne sont PAS dans ce contexte : s'ils sont utiles pour répondre (panne, erreur, démarrage), lis-les avec l'outil read_logs, puis cite la cause trouvée.".into());
    }
    format!("{}\n\n{}", support_text(pool, SUPPORT_FAQ).await, lines.join("\n"))
}

/// `_support_tools(is_admin)`: the self-service tools, and the admin ones for an admin.
pub fn tools(is_admin: bool) -> Vec<Value> {
    let mut t = self_service_tools();
    if is_admin {
        t.extend(support_admin::tools());
    }
    t
}

fn self_service_tools() -> Vec<Value> {
    vec![
        json!({"type": "function", "function": {"name": "create_api_key", "description": "Crée une nouvelle clé API pour l'utilisateur connecté et la retourne.",
            "parameters": {"type": "object", "properties": {"alias": {"type": "string", "description": "Nom court de la clé (ex: mon-laptop). Optionnel."}}}}}),
        json!({"type": "function", "function": {"name": "revoke_api_key", "description": "Révoque (supprime) une clé de l'utilisateur, par son alias. Destructif : confirmer avant.",
            "parameters": {"type": "object", "properties": {"alias": {"type": "string", "description": "Alias exact de la clé à révoquer."}}, "required": ["alias"]}}}),
        json!({"type": "function", "function": {"name": "request_budget", "description": "Dépose une demande d'augmentation de budget pour l'utilisateur (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string", "description": "Raison (optionnel)."}}}}}),
        json!({"type": "function", "function": {"name": "request_model", "description": "Demande l'ajout d'un modèle par son identifiant Hugging Face (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {"hf_model_id": {"type": "string", "description": "Ex: Qwen/Qwen3-Coder-30B-A3B-Instruct."}, "reason": {"type": "string", "description": "Pourquoi ce modèle (optionnel)."}}, "required": ["hf_model_id"]}}}),
    ]
}

pub fn label(name: &str) -> String {
    match name {
        "create_api_key" => "Créer une clé API",
        "revoke_api_key" => "Révoquer une clé API",
        "request_budget" => "Demander du budget",
        "request_model" => "Demander un modèle",
        "launch_model" => "Lancer un modèle",
        "stop_model" => "Arrêter le modèle",
        "manage_service" => "Gérer un service",
        "read_logs" => "Lire les logs d'un service",
        other => other,
    }
    .to_string()
}

pub fn arg(args: &Value, k: &str) -> String {
    crate::memory::py_strip(&args.get(k).filter(|v| crate::keys::truthy(v)).map(crate::chat::history::py_str).unwrap_or_default()).to_string()
}

/// `_support_tool_target`: the short target shown with a tool call.
pub fn target(name: &str, args: &Value) -> Option<String> {
    let s = |k: &str| Some(arg(args, k)).filter(|v| !v.is_empty());
    match name {
        "create_api_key" | "revoke_api_key" => s("alias"),
        "request_model" => s("hf_model_id"),
        "launch_model" => s("name"),
        "manage_service" => s("service").map(|sv| format!("{sv} · {}", arg(args, "action"))),
        _ => None,
    }
}

/// `_exec_support_tool`: (result, ok). Always on behalf of the session user.
pub async fn exec(state: &AppState, name: &str, args: &Value, username: &str, fullname: Option<&str>, is_admin: bool) -> (String, bool) {
    let pool = state.db.pool();
    match name {
        "create_api_key" => {
            let raw = arg(args, "alias");
            let alias = if raw.is_empty() { format!("{username}-{}", chrono::Utc::now().timestamp()) } else { crate::handlers::key_alias(&raw) };
            let Some(new_key) = crate::handlers::create_key(state, &alias, username, is_admin).await else {
                return ("Échec de la création (alias déjà pris ou LiteLLM injoignable).".into(), false);
            };
            let _ = sqlx::query("INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) VALUES (?,?,?,?)")
                .bind(username).bind(&alias).bind(&new_key).bind(crate::keys::iso(&chrono::Local::now().naive_local())).execute(pool).await;
            (format!("Clé créée (alias={alias}). CLÉ COMPLÈTE à montrer une fois : {new_key}"), true)
        }
        "revoke_api_key" => {
            let alias = arg(args, "alias");
            let key: Option<String> = sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username=? AND key_alias=?").bind(username).bind(&alias).fetch_optional(pool).await.ok().flatten();
            let Some(key) = key else { return (format!("Aucune clé « {alias} » pour cet utilisateur."), false) };
            if crate::handlers::revoke_key(&key).await {
                let _ = sqlx::query("DELETE FROM api_keys WHERE username=? AND key_alias=?").bind(username).bind(&alias).execute(pool).await;
                (format!("Clé « {alias} » révoquée."), true)
            } else {
                ("Échec de la révocation côté LiteLLM.".into(), false)
            }
        }
        "request_budget" => request_budget(state, args, username, fullname).await,
        "request_model" => request_model(state, args, username, fullname).await,
        "read_logs" if !is_admin => ("Lecture réservée aux administrateurs.".into(), false),
        "manage_service" if !is_admin => ("Action réservée aux administrateurs.".into(), false),
        "launch_model" | "stop_model" if !is_admin => ("Action réservée aux admins.".into(), false),
        _ if support_admin::ADMIN_TOOLS.contains(&name) => support_admin::exec(pool, name, args, username).await,
        other => (format!("Outil inconnu : {other}"), false),
    }
}

/// `request_budget`: one pending request per account; the admins are told.
async fn request_budget(state: &AppState, args: &Value, username: &str, fullname: Option<&str>) -> (String, bool) {
    let pool = state.db.pool();
    let reason = arg(args, "reason");
    let pending: Option<i64> = sqlx::query_scalar("SELECT 1 FROM budget_requests WHERE username=? AND status='pending'").bind(username).fetch_optional(pool).await.ok().flatten();
    if pending.is_some() {
        return ("Une demande de budget est déjà en attente.".into(), true);
    }
    let current = crate::keys::account_info(username).await.max_budget;
    let inserted = sqlx::query("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) VALUES (?,?,?,?,?,?,?)")
        .bind(username).bind(fullname).bind("(compte)").bind(current).bind(&reason).bind("pending")
        .bind(crate::keys::iso(&chrono::Local::now().naive_local())).execute(pool).await;
    if inserted.is_err() {
        return ("Erreur lors de l'exécution de l'action (DatabaseError).".into(), false);
    }
    let who = fullname.unwrap_or("None");
    tokio::join!(
        crate::discord::budget_request(pool, username, who, "(compte)", current, &reason),
        crate::mailer::budget_request(pool, username, who, "(compte)", current, &reason),
    );
    ("Demande de budget envoyée à un admin.".into(), true)
}

/// `request_model`: one pending request per model and account; the admins are told.
async fn request_model(state: &AppState, args: &Value, username: &str, fullname: Option<&str>) -> (String, bool) {
    let pool = state.db.pool();
    let hf = arg(args, "hf_model_id");
    if hf.is_empty() {
        return ("Identifiant de modèle manquant.".into(), false);
    }
    let reason = arg(args, "reason");
    let pending: Option<i64> = sqlx::query_scalar("SELECT 1 FROM model_requests WHERE username=? AND model_id=? AND status='pending'").bind(username).bind(&hf).fetch_optional(pool).await.ok().flatten();
    if pending.is_some() {
        return (format!("Une demande pour « {hf} » est déjà en attente."), true);
    }
    let inserted = sqlx::query("INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) VALUES (?,?,?,?,?,?)")
        .bind(username).bind(fullname).bind(&hf).bind(&reason).bind("pending")
        .bind(crate::keys::iso(&chrono::Local::now().naive_local())).execute(pool).await;
    if inserted.is_err() {
        return ("Erreur lors de l'exécution de l'action (DatabaseError).".into(), false);
    }
    let who = fullname.unwrap_or("None");
    let _ = tokio::join!(
        crate::discord::model_request(pool, &hf, username, who, &reason),
        crate::mailer::model_request(pool, &hf, username, fullname, &reason),
    );
    (format!("Demande d'ajout du modèle « {hf} » envoyée à un admin."), true)
}

/// `create_pending_action`: the confirmation request; its opaque token.
pub async fn create_pending(pool: &SqlitePool, username: &str, tool: &str, args: &Value, label: &str, target: Option<&str>) -> String {
    use base64::Engine;
    let mut b = [0u8; 24];
    let _ = getrandom::getrandom(&mut b);
    let token = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b);
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let _ = sqlx::query("DELETE FROM pending_actions WHERE created_at < ?").bind(now - PENDING_TTL).execute(pool).await;
    let _ = sqlx::query("DELETE FROM pending_actions WHERE username=? AND status='pending' AND token NOT IN (SELECT token FROM pending_actions WHERE username=? ORDER BY created_at DESC LIMIT ?)")
        .bind(username).bind(username).bind(PENDING_MAX).execute(pool).await;
    let _ = sqlx::query("INSERT INTO pending_actions (token, username, tool, args, label, target, created_at, status) VALUES (?,?,?,?,?,?,?, 'pending')")
        .bind(&token).bind(username).bind(tool).bind(crate::handlers::py_dumps_ascii(args)).bind(label).bind(target).bind(now).execute(pool).await;
    token
}

/// `pending_action`: (tool, args, label) of THIS user's still-valid request.
pub async fn pending(pool: &SqlitePool, username: &str, token: &str) -> Option<(String, Option<String>, Option<String>)> {
    let row: Option<(Option<String>, Option<String>, Option<String>, Option<f64>, Option<String>)> =
        sqlx::query_as("SELECT tool, args, label, created_at, status FROM pending_actions WHERE token=? AND username=?").bind(token).bind(username).fetch_optional(pool).await.ok().flatten();
    let (tool, args, label, created, status) = row?;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    (status.as_deref() == Some("pending") && now - created.unwrap_or(0.0) <= PENDING_TTL).then(|| (tool.unwrap_or_default(), args, label))
}

/// `close_action`: true only for the request that did the closing (single use).
pub async fn close(pool: &SqlitePool, username: &str, token: &str, status: &str) -> bool {
    sqlx::query("UPDATE pending_actions SET status=? WHERE token=? AND username=? AND status='pending'")
        .bind(status).bind(token).bind(username).execute(pool).await.is_ok_and(|r| r.rows_affected() == 1)
}

/// What an extra tool runs.
pub enum Route {
    Mcp { server_id: i64, server_name: String, tool_name: String },
    Chats,
    Skill,
}

/// `_user_extra_tools`: the user's MCP tools (discovered, filtered), history and skill tools.
pub async fn extra_tools(state: &AppState, username: &str, ldap: &str) -> (Vec<Value>, std::collections::HashMap<String, Route>) {
    let pool = state.db.pool();
    let mut tools = Vec::new();
    let mut routing = std::collections::HashMap::new();
    let servers: Vec<(i64, Option<String>, Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT id, name, url, auth_header, allowed_tools FROM mcp_servers WHERE username=? AND enabled=1").bind(username).fetch_all(pool).await.unwrap_or_default();
    for (id, name, url, auth, allowed) in servers {
        let mut discovered = crate::mcp::list_tools_cached(id, url.as_deref().unwrap_or(""), auth.as_deref(), ldap).await;
        let allow: std::collections::HashSet<String> = allowed.unwrap_or_default().split(',').map(|t| crate::memory::py_strip(t).to_string()).filter(|t| !t.is_empty()).collect();
        if !allow.is_empty() {
            discovered.retain(|t| t.get("name").and_then(Value::as_str).is_some_and(|n| allow.contains(n)));
        }
        let sname = name.unwrap_or_default();
        for t in discovered {
            let tname = t.get("name").and_then(Value::as_str).unwrap_or("").to_string();
            let safe: String = tname.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '_' }).take(60).collect();
            let prefixed = format!("mcp_{id}_{safe}");
            let desc = t.get("description").and_then(Value::as_str).filter(|d| !d.is_empty()).map(str::to_string).unwrap_or_else(|| tname.clone());
            tools.push(json!({"type": "function", "function": {"name": prefixed, "description": format!("[Serveur MCP « {sname} »] {desc}"),
                "parameters": t.get("inputSchema").filter(|v| crate::keys::truthy(v)).cloned().unwrap_or_else(|| json!({"type": "object", "properties": {}}))}}));
            routing.insert(prefixed, Route::Mcp { server_id: id, server_name: sname.clone(), tool_name: tname });
        }
    }
    if crate::chat::tools::library::search_enabled(pool, username).await {
        for t in crate::chat::tools::library::history_tools() {
            routing.insert(t["function"]["name"].as_str().unwrap_or("").to_string(), Route::Chats);
            tools.push(t);
        }
    }
    let rows = crate::chat::tools::library::enabled_skills(pool, username).await;
    for t in crate::chat::tools::library::skill_tools(&rows) {
        routing.insert(t["function"]["name"].as_str().unwrap_or("").to_string(), Route::Skill);
        tools.push(t);
    }
    (tools, routing)
}

/// `_exec_mcp_tool`: a tool of one of THIS user's enabled servers.
pub async fn exec_mcp(pool: &SqlitePool, server_id: i64, tool: &str, args: &Value, username: &str, ldap: &str) -> (String, bool) {
    let row: Option<(Option<String>, Option<String>)> = sqlx::query_as("SELECT url, auth_header FROM mcp_servers WHERE id=? AND username=? AND enabled=1").bind(server_id).bind(username).fetch_optional(pool).await.ok().flatten();
    let Some((url, auth)) = row else { return ("Serveur MCP introuvable.".into(), false) };
    let mut c = crate::mcp::Client::new(url.as_deref().unwrap_or(""), auth.as_deref(), ldap);
    let called = async {
        c.initialize().await?;
        c.call_tool(tool, args).await
    };
    match called.await {
        Ok(r) => r,
        Err(crate::mcp::McpError::Said(e)) => (e, false),
        Err(crate::mcp::McpError::Transport) => ("Erreur MCP (ConnectionError).".into(), false),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_grouped_like_python() {
        assert_eq!(grouped(1234567.5), "1 234 568");
        assert_eq!(grouped(999.0), "999");
        assert_eq!(grouped(0.0), "0");
        assert_eq!(grouped(262144.0), "262 144");
    }

    #[test]
    fn masked_keys() {
        assert_eq!(mask_key("sk-1234567890abcd"), "sk-123…abcd");
        assert_eq!(mask_key("short"), "—");
    }
}
