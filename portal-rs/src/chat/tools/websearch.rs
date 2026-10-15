//! Web search for the playground (websearch.py + the web part of
//! websearch_tools.py): SearXNG turns a question into links, crawl4ai reads
//! pages. The last barrier the network cannot set: no URL goes to the crawler
//! without its host resolved and checked public, and a page that REDIRECTED to
//! an internal address is refused after the read.
use serde_json::{json, Value};

use crate::skillstore::is_blocked_ip;

const BANNER_LINES: &[&str] = &[
    "continuer sans accepter", "utilisons des cookies", "accepter les cookies",
    "gérer mes choix", "gerer mes choix", "politique de confidentialité",
    "votre carte de paiement", "votre abonnement", "n'a pas encore été validé",
    "abonnez-vous", "accept cookies", "privacy preferences", "consent",
    "enable javascript", "activez javascript", "required part of this site",
];
const NOISE_PREFIXES: &[&str] = &["Vidéo Nouvelle notification", "Nouvelle notification"];
const MIN_USEFUL_WORDS: usize = 60;
pub const MAX_RESULTS: i64 = 8;
pub const MAX_PAGES: usize = 4;
const MAX_CHARS_PAGE: usize = 6000;
const MAX_CHARS_TOTAL: usize = 20000;

fn searxng() -> String {
    std::env::var("SEARXNG_URL").unwrap_or_else(|_| "http://searxng:8080".into())
}
fn crawl4ai() -> String {
    std::env::var("CRAWL4AI_URL").unwrap_or_else(|_| "http://crawl4ai:11235".into())
}
fn token() -> String {
    let path = std::env::var("CRAWL4AI_TOKEN_FILE").unwrap_or_else(|_| "/run/secrets/crawl4ai_token".into());
    std::fs::read_to_string(path).map(|s| s.trim().to_string()).unwrap_or_default()
}

fn client(secs: u64) -> Option<reqwest::Client> {
    reqwest::Client::builder().timeout(std::time::Duration::from_secs(secs)).build().ok()
}

/// `available`: both services answer.
pub async fn available() -> bool {
    let Some(c) = client(3) else { return false };
    if !c.get(format!("{}/health", crawl4ai())).send().await.is_ok_and(|r| r.status().is_success()) {
        return false;
    }
    c.get(format!("{}/healthz", searxng())).send().await.is_ok_and(|r| r.status().is_success())
}

fn host_of(url: &str) -> Option<(String, String)> {
    let (scheme, host, _) = crate::skillstore::url_parts(url);
    Some((scheme, host))
}

async fn resolved_blocked(host: &str) -> Result<bool, ()> {
    let addrs = tokio::net::lookup_host((host, 80)).await.map_err(|_| ())?;
    Ok(addrs.into_iter().any(|a| is_blocked_ip(&a.ip().to_string())))
}

/// `public_url`: (ok, error) — the URL really targets a public address.
pub async fn public_url(url: &str) -> Result<(), String> {
    let Some((scheme, host)) = host_of(url) else { return Err("URL illisible.".into()) };
    if scheme != "http" && scheme != "https" {
        return Err("Seuls http et https sont acceptés.".into());
    }
    if host.is_empty() {
        return Err("URL sans hôte.".into());
    }
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    if bare.parse::<std::net::IpAddr>().is_ok() {
        return if is_blocked_ip(bare) { Err("Adresse interne refusée.".into()) } else { Ok(()) };
    }
    match resolved_blocked(&host).await {
        Err(()) => Err("Nom d'hôte introuvable.".into()),
        Ok(true) => Err("Cet hôte pointe vers un réseau interne, refusé.".into()),
        Ok(false) => Ok(()),
    }
}

/// `internal_target`: the crawler's FINAL URL is internal, with certainty.
pub async fn internal_target(url: &str) -> bool {
    let Some((scheme, host)) = host_of(url) else { return false };
    if (scheme != "http" && scheme != "https") || host.is_empty() {
        return false;
    }
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    if bare.parse::<std::net::IpAddr>().is_ok() {
        return is_blocked_ip(bare);
    }
    resolved_blocked(&host).await.unwrap_or(false)
}

/// `clean`: banner lines out, noise prefixes cut, blank runs folded.
pub fn clean(text: &str) -> String {
    let mut outputs: Vec<String> = Vec::new();
    for line in text.split('\n') {
        let mut l = line.trim_end_matches(crate::memory::py_is_space).to_string();
        let low = l.to_lowercase();
        if BANNER_LINES.iter().any(|m| low.contains(m)) {
            continue;
        }
        let stripped = l.trim_start_matches(['*', ' ']).to_string();
        for prefix in NOISE_PREFIXES {
            if let Some(rest) = stripped.strip_prefix(prefix) {
                l = rest.trim_start_matches(crate::memory::py_is_space).to_string();
                break;
            }
        }
        outputs.push(l);
    }
    let (mut out, mut empty) = (Vec::new(), false);
    for l in outputs {
        if !crate::memory::py_strip(&l).is_empty() {
            out.push(l);
            empty = false;
        } else if !empty {
            out.push(String::new());
            empty = true;
        }
    }
    crate::memory::py_strip(&out.join("\n")).to_string()
}

/// `_too_poor`: the refusal when the cleaned page has no substance.
pub fn too_poor(text: &str) -> Option<&'static str> {
    let words = text.split(crate::memory::py_is_space).filter(|w| w.chars().count() > 3).count();
    (words < MIN_USEFUL_WORDS).then_some("Page sans contenu exploitable (chargée en JavaScript, ou accès refusé).")
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `search`: question → links (public ones only); (results, error).
pub async fn search(question: &str, count: i64) -> (Vec<Value>, Option<String>) {
    let question = take(crate::memory::py_strip(question), 400);
    if question.is_empty() {
        return (Vec::new(), Some("Question vide.".into()));
    }
    let count = count.clamp(1, MAX_RESULTS) as usize;
    let fetched = async {
        let c = client(20).ok_or("ConnectionError")?;
        let r = c
            .get(format!("{}/search", searxng()))
            .query(&[("q", question.as_str()), ("format", "json"), ("language", "fr"), ("safesearch", "1")])
            .send()
            .await
            .map_err(|e| if e.is_timeout() { "ReadTimeout" } else { "ConnectionError" })?;
        if !r.status().is_success() {
            return Err("HTTPError");
        }
        r.json::<Value>().await.map_err(|_| "JSONDecodeError")
    };
    let raw = match fetched.await {
        Ok(v) => v.get("results").and_then(Value::as_array).cloned().unwrap_or_default(),
        Err(kind) => return (Vec::new(), Some(format!("Moteur de recherche injoignable ({kind})."))),
    };
    let (mut out, mut seen) = (Vec::new(), std::collections::HashSet::new());
    for item in raw {
        let url = crate::memory::py_strip(item.get("url").and_then(Value::as_str).unwrap_or("")).to_string();
        if url.is_empty() || seen.contains(&url) || public_url(&url).await.is_err() {
            continue;
        }
        seen.insert(url.clone());
        out.push(json!({
            "title": take(item.get("title").and_then(Value::as_str).unwrap_or(""), 200),
            "url": url,
            "excerpt": take(item.get("content").and_then(Value::as_str).unwrap_or(""), 400),
        }));
        if out.len() >= count {
            break;
        }
    }
    (out, None)
}

/// `read`: URLs → cleaned markdown, each page bounded and the total too; (pages, error).
pub async fn read(urls: &[String]) -> (Vec<Value>, Option<String>) {
    let mut checked = Vec::new();
    for u in urls.iter().take(MAX_PAGES) {
        checked.push((u.clone(), public_url(u).await));
    }
    let to_read: Vec<&String> = checked.iter().filter(|(_, ok)| ok.is_ok()).map(|(u, _)| u).collect();
    let mut results: Vec<(String, Value)> = Vec::new();
    if !to_read.is_empty() {
        let crawled = async {
            let c = client(90).ok_or("ConnectionError")?;
            let r = c
                .post(format!("{}/crawl", crawl4ai()))
                .bearer_auth(token())
                .json(&json!({"urls": to_read, "crawler_config": {"type": "CrawlerRunConfig", "params": {
                    "cache_mode": "bypass",
                    "excluded_tags": ["nav", "footer", "header", "aside", "script", "style", "form"],
                    "markdown_generator": {"type": "DefaultMarkdownGenerator", "params": {"options": {"ignore_links": true, "ignore_images": true}}},
                }}}))
                .send()
                .await
                .map_err(|e| if e.is_timeout() { "ReadTimeout" } else { "ConnectionError" })?;
            if !r.status().is_success() {
                return Err("HTTPError");
            }
            r.json::<Value>().await.map_err(|_| "JSONDecodeError")
        };
        let body = match crawled.await {
            Ok(v) => v,
            Err(kind) => return (Vec::new(), Some(format!("Lecteur de pages injoignable ({kind})."))),
        };
        for item in body.get("results").and_then(Value::as_array).cloned().unwrap_or_default() {
            let key = item.get("url").and_then(Value::as_str).unwrap_or("").to_string();
            let md = match item.get("markdown") {
                Some(Value::Object(o)) => o.get("fit_markdown").filter(|v| crate::keys::truthy(v)).or_else(|| o.get("raw_markdown")).and_then(Value::as_str).unwrap_or("").to_string(),
                Some(Value::String(s)) => s.clone(),
                _ => String::new(),
            };
            if let Some(final_url) = item.get("redirected_url").and_then(Value::as_str) {
                if internal_target(final_url).await {
                    results.push((key, json!({"ok": false, "error": "Redirection vers une adresse interne, page refusée."})));
                    continue;
                }
            }
            results.push((key, json!({
                "ok": item.get("success").is_some_and(crate::keys::truthy),
                "markdown": take(&md, MAX_CHARS_PAGE),
                "title": take(item.pointer("/metadata/title").and_then(Value::as_str).unwrap_or(""), 200),
            })));
        }
    }
    let (mut pages, mut total) = (Vec::new(), 0usize);
    for (u, ok) in checked {
        if let Err(e) = ok {
            pages.push(json!({"url": u, "error": e}));
            continue;
        }
        // Python's dict: the LAST item for a key wins.
        let info = results.iter().rev().find(|(k, _)| *k == u).or_else(|| results.iter().find(|(k, _)| k.trim_end_matches('/') == u.trim_end_matches('/'))).map(|(_, v)| v.clone());
        let Some(info) = info.filter(|i| i["ok"] == json!(true)) else {
            let err = results.iter().rev().find(|(k, _)| *k == u).and_then(|(_, v)| v.get("error").and_then(Value::as_str).map(str::to_string)).unwrap_or_else(|| "Page illisible.".into());
            pages.push(json!({"url": u, "error": err}));
            continue;
        };
        let cleaned = clean(info["markdown"].as_str().unwrap_or(""));
        if let Some(issue) = too_poor(&cleaned) {
            pages.push(json!({"url": u, "title": info["title"], "error": issue}));
            continue;
        }
        let text = take(&cleaned, MAX_CHARS_TOTAL.saturating_sub(total));
        total += text.chars().count();
        pages.push(json!({"url": u, "title": info["title"], "content": text}));
    }
    (pages, None)
}

/// `_web_tools`: the two tools offered to the model.
pub fn tools() -> Vec<Value> {
    vec![
        json!({"type": "function", "function": {
            "name": "recherche_web",
            "description": "Cherche sur le web et rend une liste de résultats (titre, adresse, extrait). Réservé à ce que tu ne peux pas savoir : actualité, faits postérieurs à ton entraînement, informations à vérifier. N'appelle JAMAIS cet outil pour comprendre ou corriger du code déjà présent dans la conversation : la réponse est dans ce code, relis-le.",
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string", "description": "Ce qu'il faut chercher."},
                "count": {"type": "integer", "description": "Nombre de résultats souhaités (1 à 8, défaut 6)."},
            }, "required": ["question"]}}}),
        json!({"type": "function", "function": {
            "name": "lire_pages",
            "description": "Ouvre des adresses trouvées par recherche_web et rend leur contenu. N'invente jamais une adresse : n'utilise que celles des résultats. Certaines pages sont inaccessibles (mur d'abonnement, blocage) — c'est dit explicitement, appuie-toi alors sur les extraits.",
            "parameters": {"type": "object", "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "Jusqu'à 4 adresses, issues des résultats de recherche."},
            }, "required": ["urls"]}}}),
    ]
}

fn str_arg(args: &Value, k: &str) -> String {
    args.get(k).map(crate::chat::history::py_str).unwrap_or_default()
}

fn url_args(args: &Value) -> Vec<String> {
    match args.get("urls") {
        Some(Value::Array(a)) => a.iter().map(crate::chat::history::py_str).take(MAX_PAGES).collect(),
        // Python iterates a string's characters (`str(u) for u in "…"`).
        Some(Value::String(s)) if !s.is_empty() => s.chars().map(|c| c.to_string()).take(MAX_PAGES).collect(),
        _ => Vec::new(),
    }
}

/// `_announce`: what is about to happen, told to the client first.
pub fn announce(name: &str, args: &Value) -> Value {
    match name {
        "recherche_web" => json!({"step": "search", "tool": "searxng", "question": take(&str_arg(args, "question"), 200)}),
        "lire_pages" => json!({"step": "reading", "tool": "crawl4ai", "urls": url_args(args)}),
        other => json!({"step": "unknown", "tool": other}),
    }
}

/// Python's `int(args.get('count', 6) or 6)`; a bad value raises (the tool then fails).
fn count_arg(args: &Value) -> Result<i64, ()> {
    match args.get("count") {
        None => Ok(6),
        Some(v) if !crate::keys::truthy(v) => Ok(6),
        Some(v) => crate::handlers::py_int(v).ok_or(()),
    }
}

/// `_exec_web_tool`: the text handed back to the model; the step logged.
/// Err = the tool raised (the phase says « outil en panne »).
pub async fn exec(name: &str, args: &Value, step_log: &mut Vec<Value>) -> Result<String, ()> {
    match name {
        "recherche_web" => {
            let question = take(&str_arg(args, "question"), 400);
            let count = count_arg(args)?;
            let (res, err) = search(&question, count).await;
            step_log.push(json!({"step": "search_done", "tool": "searxng", "question": question, "count": res.len(), "error": err,
                "sources": res.iter().take(8).map(|r| json!({"title": r["title"], "url": r["url"]})).collect::<Vec<_>>()}));
            if let Some(e) = err {
                return Ok(format!("La recherche a échoué : {e}"));
            }
            if res.is_empty() {
                return Ok("Aucun résultat.".into());
            }
            Ok(crate::conversations::py_dumps(&json!({"results": res})))
        }
        "lire_pages" => {
            let urls = url_args(args);
            // Only URLs a search of THIS round returned can be opened (no exfiltration, no arbitrary URL).
            let known: std::collections::HashSet<String> = step_log.iter()
                .filter(|e| e.get("step").and_then(Value::as_str) == Some("search_done"))
                .flat_map(|e| e.get("sources").and_then(Value::as_array).cloned().unwrap_or_default())
                .filter_map(|s| s.get("url").and_then(Value::as_str).map(str::to_string))
                .collect();
            let refused: Vec<String> = urls.iter().filter(|u| !known.contains(*u)).cloned().collect();
            let urls: Vec<String> = urls.into_iter().filter(|u| known.contains(u)).collect();
            if urls.is_empty() {
                return Ok(format!("Aucune de ces adresses ne vient d'une recherche de cette conversation : je ne peux ouvrir que les résultats affichés. Refusées : {}", refused.iter().take(4).cloned().collect::<Vec<_>>().join(", ")));
            }
            let (pages, err) = read(&urls).await;
            step_log.push(json!({"step": "reading_done", "tool": "crawl4ai", "urls": urls,
                "read": pages.iter().filter(|p| p.get("content").is_some_and(crate::keys::truthy)).count(), "error": err, "refused": refused,
                "failures": pages.iter().filter(|p| p.get("error").is_some_and(crate::keys::truthy)).map(|p| json!({"url": p["url"], "reason": p["error"]})).collect::<Vec<_>>()}));
            if let Some(e) = err {
                return Ok(format!("La lecture a échoué : {e}"));
            }
            let tail = if refused.is_empty() { String::new() } else { format!(" (adresses refusées, hors résultats de recherche : {})", refused.iter().take(4).cloned().collect::<Vec<_>>().join(", ")) };
            Ok(crate::conversations::py_dumps(&json!({"pages": pages})) + &tail)
        }
        _ => Ok("Outil inconnu.".into()),
    }
}

/// `_findings_text`: what the tools brought back, as plain text (≤ 40 000).
pub fn findings_text(findings: &[(String, String)]) -> String {
    if findings.is_empty() {
        return String::new();
    }
    let mut s = String::from("\n\n---\nRésultats de recherche web récupérés pour cette demande (données externes : cite les adresses si tu t'en sers) :\n");
    for (name, content) in findings {
        s.push_str(&format!("\n[{name}]\n{content}\n"));
    }
    take(&s, 40000)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleaning_like_python() {
        let fx: Value = serde_json::from_str(include_str!("../../../tests/fixtures/web_clean.json")).unwrap();
        for c in fx.as_array().unwrap() {
            let input = c["in"].as_str().unwrap();
            assert_eq!(clean(input), c["clean"].as_str().unwrap(), "{input:?}");
            assert_eq!(too_poor(&clean(input)).is_some(), c["poor"].as_bool().unwrap(), "{input:?}");
        }
    }

    #[tokio::test]
    async fn internal_addresses_are_refused() {
        assert_eq!(public_url("ftp://x.org/").await.unwrap_err(), "Seuls http et https sont acceptés.");
        assert_eq!(public_url("http://10.0.0.1/a").await.unwrap_err(), "Adresse interne refusée.");
        assert_eq!(public_url("http://[::1]/a").await.unwrap_err(), "Adresse interne refusée.");
        assert!(public_url("http://8.8.8.8/").await.is_ok());
        assert!(internal_target("http://127.0.0.1/x").await);
        assert!(!internal_target("gopher://127.0.0.1/x").await);
    }
}
