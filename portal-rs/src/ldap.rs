//! The LDAP directory (auth.py, directory.py, directory_groups.py): an
//! account's password check, its e-mail address, and a background snapshot of
//! every account's state and groups, read with the service account.
//!
//! Never on a request's path: a bind on an Authentik outpost costs ~3 s. The
//! snapshot is refreshed every two minutes in the background and the pages
//! read it; it is None until one read succeeded (missing data is not proof
//! of absence).
use ldap3::adapters::{Adapter, EntriesOnly, PagedResults};
use ldap3::{ldap_escape, dn_escape, Ldap, LdapConnAsync, LdapConnSettings, Scope, SearchEntry};
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

pub const ACTIVE: &str = "active";
pub const DISABLED: &str = "disabled";
pub const ABSENT: &str = "absent";

/// `ldap_config()`: Admin → Config, with the code defaults.
#[derive(Clone, Debug, Default)]
pub struct Config {
    pub uri: String,
    pub base: String,
    pub users_dn: String,
    pub login_attr: String,
    pub bind_dn: String,
    pub bind_pw: String,
}

impl Config {
    fn users_base(&self) -> String {
        format!("{},{}", self.users_dn, self.base)
    }
    fn service_account(&self) -> bool {
        !self.bind_dn.is_empty() && !self.bind_pw.is_empty()
    }
}

async fn setting(pool: &SqlitePool, name: &str, default: &str) -> String {
    let v: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key=?").bind(format!("cfg_{name}")).fetch_optional(pool).await.ok().flatten();
    v.filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

pub async fn config(pool: &SqlitePool) -> Config {
    Config {
        uri: setting(pool, "ldap_uri", "").await,
        base: setting(pool, "ldap_base", "dc=example,dc=org").await,
        users_dn: setting(pool, "ldap_users_dn", "ou=users").await,
        login_attr: setting(pool, "ldap_login_attr", "cn").await,
        bind_dn: setting(pool, "ldap_bind_dn", "").await,
        bind_pw: setting(pool, "ldap_bind_pw", "").await,
    }
}

/// A connection, its driver running in the background.
async fn connect(uri: &str) -> Result<Ldap, String> {
    if uri.is_empty() {
        return Err("no LDAP URI".into());
    }
    let settings = LdapConnSettings::new().set_conn_timeout(Duration::from_secs(5));
    let (conn, mut ldap) = LdapConnAsync::with_settings(settings, uri).await.map_err(|e| e.to_string())?;
    ldap3::drive!(conn);
    ldap.with_timeout(Duration::from_secs(15));
    Ok(ldap)
}

/// A refused bind worded as Python's ldap3 words it (`automatic bind not
/// successful - invalidCredentials`), the result code's name included.
fn bind_refusal(rc: u32) -> String {
    let name = match rc {
        32 => "noSuchObject",
        48 => "inappropriateAuthentication",
        49 => "invalidCredentials",
        50 => "insufficientAccessRights",
        53 => "unwillingToPerform",
        _ => return format!("automatic bind not successful - rc={rc}"),
    };
    format!("automatic bind not successful - {name}")
}

async fn service_bind(c: &Config) -> Result<Ldap, String> {
    let mut ldap = connect(&c.uri).await?;
    let r = ldap.simple_bind(&c.bind_dn, &c.bind_pw).await.map_err(|e| e.to_string())?;
    if r.rc != 0 {
        return Err(bind_refusal(r.rc));
    }
    Ok(ldap)
}

fn first<'a>(e: &'a SearchEntry, name: &str) -> Option<&'a str> {
    e.attrs.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).and_then(|(_, v)| v.first()).map(String::as_str)
}

fn all(e: &SearchEntry, name: &str) -> Vec<String> {
    e.attrs.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.clone()).unwrap_or_default()
}

/// `_is_admin_group(dn)`: one RDN component is exactly cn=<admin group>.
pub fn is_admin_group(dn: &str, admin_group: &str) -> bool {
    let group = admin_group.trim().to_lowercase();
    !group.is_empty()
        && dn.split(',').any(|part| {
            let (attr, val) = part.trim().split_once('=').unwrap_or((part, ""));
            attr.trim().eq_ignore_ascii_case("cn") && val.trim().to_lowercase() == group
        })
}

/// `group_names(values)`: memberOf DNs (or plain names) reduced to their cn.
pub fn group_names(values: &[String]) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for v in values {
        let v = v.trim();
        if v.is_empty() {
            continue;
        }
        let head = v.split(',').next().unwrap_or("");
        let name = if head.contains('=') {
            let (attr, val) = head.split_once('=').unwrap_or(("", ""));
            if !attr.trim().eq_ignore_ascii_case("cn") {
                continue;
            }
            val.trim().to_string()
        } else {
            v.to_string()
        };
        if !name.is_empty() && !names.contains(&name) {
            names.push(name);
        }
    }
    names
}

/// `ldap_authenticate(username, password, with_groups=True)`: (admin, display
/// name, memberOf DNs) when the directory accepts the password.
pub async fn authenticate(pool: &SqlitePool, username: &str, password: &str) -> Option<(bool, String, Vec<String>)> {
    // An empty password is an « unauthenticated bind » some directories accept.
    if password.is_empty() || !crate::handlers::username_ok(username) {
        return None;
    }
    let c = config(pool).await;
    let mut ldap = connect(&c.uri).await.ok()?;
    let user_dn = format!("{}={},{}", c.login_attr, dn_escape(username), c.users_base());
    ldap.simple_bind(&user_dn, password).await.ok()?.success().ok()?;
    let filter = format!("({}={})", c.login_attr, ldap_escape(username));
    let (rows, _) = ldap.search(&c.users_base(), Scope::Subtree, &filter, vec!["cn", "displayName", "memberOf"]).await.ok()?.success().ok()?;
    let _ = ldap.unbind().await;
    let e = SearchEntry::construct(rows.into_iter().next()?);
    let fullname = first(&e, "displayName").filter(|v| !v.is_empty()).or_else(|| first(&e, "cn")).unwrap_or(username).to_string();
    let groups = all(&e, "memberOf");
    let admin_group = setting(pool, "oidc_admin_group", "admins").await;
    let is_admin = groups.iter().any(|g| is_admin_group(g, &admin_group));
    Some((is_admin, fullname, groups))
}

/// `ldap_lookup_email(username)`: the account's address, via the service account.
pub async fn lookup_email(pool: &SqlitePool, username: &str) -> Option<String> {
    let c = config(pool).await;
    if !c.service_account() || !crate::handlers::username_ok(username) {
        return None;
    }
    let mut ldap = service_bind(&c).await.ok()?;
    let filter = format!("({}={})", c.login_attr, ldap_escape(username));
    let found = ldap.search(&c.users_base(), Scope::Subtree, &filter, vec!["mail"]).await.ok().and_then(|r| r.success().ok());
    let _ = ldap.unbind().await;
    let e = SearchEntry::construct(found?.0.into_iter().next()?);
    first(&e, "mail").filter(|m| !m.is_empty()).map(str::to_string)
}

/// `ldap_resolve_sso_identity(sub, email)`: the canonical account of an SSO
/// sign-in, from the directory — by the immutable `sub` (Authentik's `uid`),
/// else by the e-mail — never from the token's own, user-editable username.
/// (username, admin, display name, memberOf DNs).
pub async fn resolve_sso_identity(pool: &SqlitePool, sub: &str, email: &str) -> Option<(String, bool, String, Vec<String>)> {
    let c = config(pool).await;
    if !c.service_account() {
        return None;
    }
    let mut ldap = service_bind(&c).await.ok()?;
    let attrs = vec!["cn", "displayName", "mail", "memberOf"];
    let sub_ok = (1..=256).contains(&sub.len()) && sub.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'@' | b'-'));
    let mut entry = None;
    if sub_ok {
        if let Ok(r) = ldap.search(&c.users_base(), Scope::Subtree, &format!("(uid={})", ldap_escape(sub)), attrs.clone()).await {
            entry = r.0.into_iter().next();
        }
    }
    if entry.is_none() && email.contains('@') {
        if let Ok(r) = ldap.search(&c.users_base(), Scope::Subtree, &format!("(mail={})", ldap_escape(email)), attrs).await {
            entry = r.0.into_iter().next();
        }
    }
    let _ = ldap.unbind().await;
    let e = SearchEntry::construct(entry?);
    let username = first(&e, "cn").filter(|u| !u.is_empty() && crate::handlers::username_ok(u))?.to_string();
    let fullname = first(&e, "displayName").filter(|v| !v.is_empty()).unwrap_or(&username).to_string();
    let groups = all(&e, "memberOf");
    let admin_group = setting(pool, "oidc_admin_group", "admins").await;
    let is_admin = groups.iter().any(|g| is_admin_group(g, &admin_group));
    Some((username, is_admin, fullname, groups))
}

fn truthy_attr(v: &str) -> bool {
    matches!(v.trim().to_uppercase().as_str(), "TRUE" | "1" | "YES")
}

/// `entry_state(attrs)`: what each directory flavour says about an account.
pub fn entry_state(attrs: &HashMap<String, Vec<String>>) -> &'static str {
    let get = |name: &str| attrs.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).and_then(|(_, v)| v.first()).map(String::as_str);
    if get("ak-active").is_some_and(|v| !truthy_attr(v)) {
        return DISABLED;
    }
    if get("userAccountControl").and_then(|v| v.trim().parse::<i64>().ok()).is_some_and(|u| u & 2 != 0) {
        return DISABLED;
    }
    if get("nsAccountLock").is_some_and(truthy_attr) {
        return DISABLED;
    }
    if get("pwdAccountLockedTime").is_some_and(|v| !v.is_empty()) {
        return DISABLED;
    }
    ACTIVE
}

/// Every account's (login lowercase) state and groups.
pub struct Directory {
    pub states: HashMap<String, &'static str>,
    pub groups: HashMap<String, Vec<String>>,
}

/// `read_directory()`: one paged search of all accounts; None when the
/// directory cannot be asked. Slow (seconds): background only.
pub async fn read_directory(pool: &SqlitePool) -> Option<Directory> {
    let c = config(pool).await;
    if c.uri.is_empty() || !c.service_account() {
        return None;
    }
    let mut ldap = service_bind(&c).await.ok()?;
    let attr = c.login_attr.clone();
    let filter = format!("({}=*)", ldap_escape(&attr));
    let adapters: Vec<Box<dyn Adapter<_, _>>> = vec![Box::new(EntriesOnly::new()), Box::new(PagedResults::new(500))];
    let attrs = vec![attr.clone(), "ak-active".into(), "userAccountControl".into(), "nsAccountLock".into(), "pwdAccountLockedTime".into(), "memberOf".into()];
    let mut stream = ldap.streaming_search_with(adapters, &c.users_base(), Scope::Subtree, &filter, attrs).await.ok()?;
    let mut dir = Directory { states: HashMap::new(), groups: HashMap::new() };
    loop {
        match stream.next().await {
            Ok(Some(entry)) => {
                let e = SearchEntry::construct(entry);
                if let Some(name) = first(&e, &attr).filter(|n| !n.is_empty()) {
                    let key = name.to_lowercase();
                    dir.states.insert(key.clone(), entry_state(&e.attrs));
                    dir.groups.insert(key, group_names(&all(&e, "memberOf")));
                }
            }
            Ok(None) => break,
            Err(_) => return None,
        }
    }
    let ok = stream.finish().await.success().is_ok();
    let _ = ldap.unbind().await;
    ok.then_some(dir)
}

/// `_test_ldap()`: Admin → Config « Tester » on the saved values.
pub async fn test(pool: &SqlitePool) -> (bool, String) {
    let c = config(pool).await;
    if c.uri.is_empty() {
        return (false, "Aucune URI LDAP renseignée.".into());
    }
    if !c.service_account() {
        return (false, "Le compte de service (DN et mot de passe) est requis.".into());
    }
    let mut ldap = match service_bind(&c).await {
        Ok(l) => l,
        Err(e) => return (false, format!("Connexion ou authentification refusée : {e}").chars().take(300).collect()),
    };
    let filter = format!("({}=*)", c.login_attr);
    let found = ldap.with_search_options(ldap3::SearchOptions::new().sizelimit(1000)).search(&c.users_base(), Scope::Subtree, &filter, vec![c.login_attr.as_str()]).await;
    let _ = ldap.unbind().await;
    // A size limit reached still counts what came back, as ldap3 (Python) does.
    let n = match found {
        Ok(r) => r.0.len(),
        Err(e) => return (false, format!("Connecté, mais la recherche des comptes échoue : {e}").chars().take(300).collect()),
    };
    if n == 0 {
        return (false, format!("Connecté, mais aucun compte sous {} avec l'attribut {}.", c.users_base(), c.login_attr));
    }
    (true, format!("Connexion réussie : {n} compte(s) trouvé(s)."))
}

/// `_FRESH_S`: the snapshot's age before a background refresh.
const FRESH_S: f64 = 120.0;

struct Snapshot {
    t: f64,
    states: Option<HashMap<String, &'static str>>,
    running: bool,
}

static SNAPSHOT: Mutex<Snapshot> = Mutex::new(Snapshot { t: 0.0, states: None, running: false });

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `directory_states_for(names)`: {name: active | disabled | absent} from the
/// last background read, None until one succeeded. A stale snapshot
/// schedules a refresh (which also syncs the LDAP groups) and answers with
/// what it has.
pub fn states_for(state: &crate::AppState, names: &[String]) -> Option<HashMap<String, String>> {
    let (launch, states) = {
        let mut s = SNAPSHOT.lock().unwrap_or_else(|e| e.into_inner());
        let launch = now() - s.t > FRESH_S && !s.running;
        if launch {
            s.running = true;
        }
        (launch, s.states.clone())
    };
    if launch {
        let state = state.clone();
        tokio::spawn(async move {
            let read = read_directory(state.db.pool()).await;
            let groups = {
                let mut s = SNAPSHOT.lock().unwrap_or_else(|e| e.into_inner());
                // A failed read keeps the previous answer, retried after the same delay.
                let groups = read.map(|d| {
                    s.states = Some(d.states);
                    d.groups
                });
                s.t = now();
                groups
            };
            if let Some(g) = groups {
                crate::handlers::sync_ldap_groups(&state, &g).await;
            }
            SNAPSHOT.lock().unwrap_or_else(|e| e.into_inner()).running = false;
        });
    }
    let states = states?;
    Some(names.iter().filter(|n| !n.is_empty()).map(|n| (n.clone(), states.get(&n.to_lowercase()).copied().unwrap_or(ABSENT).to_string())).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn groups_and_admin_dns() {
        let v = vec!["cn=platform-admins,ou=groups,dc=x".to_string(), "ou=x,dc=y".into(), "Famille".into(), "cn=platform-admins,ou=other".into()];
        assert_eq!(group_names(&v), vec!["platform-admins".to_string(), "Famille".into()]);
        assert!(is_admin_group("cn=Admins,ou=groups,dc=x", "admins"));
        assert!(!is_admin_group("cn=admins_readonly,ou=groups", "admins"));
    }

    #[test]
    fn states_follow_each_flavour() {
        let m = |k: &str, v: &str| HashMap::from([(k.to_string(), vec![v.to_string()])]);
        assert_eq!(entry_state(&m("ak-active", "FALSE")), DISABLED);
        assert_eq!(entry_state(&m("userAccountControl", "514")), DISABLED);
        assert_eq!(entry_state(&m("userAccountControl", "512")), ACTIVE);
        assert_eq!(entry_state(&m("nsAccountLock", "true")), DISABLED);
        assert_eq!(entry_state(&m("pwdAccountLockedTime", "20260101000000Z")), DISABLED);
        assert_eq!(entry_state(&HashMap::new()), ACTIVE);
    }
}
