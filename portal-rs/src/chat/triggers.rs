//! When the playground offers a tool: only on an EXPLICIT request
//! (websearch_tools.py, image_tools.py, video_tools.py, document_tools.py,
//! file_tools.py, conversation_search.py, request_turns.py). The regexes are
//! the Python ones verbatim; a fragment guard drops the system-vocabulary
//! false positives (« image docker », « animation CSS »).
use std::sync::OnceLock;

use regex::Regex;

use super::history::Msg;

fn re(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static regex"))
}

/// `request_turns.ASKED`: a clarifying questionnaire of the model.
fn asked() -> &'static Regex {
    static C: OnceLock<Regex> = OnceLock::new();
    re(&C, r#"```ask\b|"questions"\s*:\s*\["#)
}

fn turns(history: &[Msg]) -> Vec<&Msg> {
    history.iter().filter(|m| m.role == "user" || m.role == "assistant").collect()
}

/// `request_texts`: the last user message, plus the one before the
/// questionnaire it answers, if any.
pub fn request_texts(history: &[Msg]) -> Vec<String> {
    let t = turns(history);
    let Some(last) = (0..t.len()).rev().find(|k| t[*k].role == "user") else { return Vec::new() };
    let mut texts = vec![t[last].content.clone()];
    if last >= 2 && t[last - 1].role == "assistant" && asked().is_match(&t[last - 1].content) && t[last - 2].role == "user" {
        texts.push(t[last - 2].content.clone());
    }
    texts
}

/// `previous_answer`: the assistant message just before the last user message.
pub fn previous_answer(history: &[Msg]) -> String {
    let t = turns(history);
    for k in (1..t.len()).rev() {
        if t[k].role == "user" {
            return if t[k - 1].role == "assistant" { t[k - 1].content.clone() } else { String::new() };
        }
    }
    String::new()
}

/// `_request_text`: what the user WROTE, without the file they pasted.
pub fn request_text(content: &str) -> String {
    static FENCE: OnceLock<Regex> = OnceLock::new();
    static TAG: OnceLock<Regex> = OnceLock::new();
    static URL: OnceLock<Regex> = OnceLock::new();
    let t = re(&FENCE, r"```[\s\S]*?```").replace_all(content, " ");
    let t = re(&TAG, r"<[^>]{1,300}>").replace_all(&t, " ");
    let t = re(&URL, r"https?://\S+").replace_all(&t, " ").into_owned();
    let n = t.chars().count();
    if n > 1200 {
        let head: String = t.chars().take(600).collect();
        let tail: String = t.chars().skip(n - 600).collect();
        return format!("{head} \n {tail}");
    }
    t
}

fn search_request() -> &'static Regex {
    static C: OnceLock<Regex> = OnceLock::new();
    re(&C, concat!(
        r"(?i)(?:cherche|recherche|regarde|va voir|renseigne-toi|informe-toi)",
        r"[^.!?\n]{0,30}?\b(?:sur (?:le )?(?:web|internet|net)|sur google)",
        r"|(?:fais|lance|effectue)[^.!?\n]{0,20}?\brecherche\b",
        r"|recherche\s+web|web\s*search|search\s+(?:the\s+)?web|search\s+online",
        r"|google[-\s]?(?:le|la|it)\b"
    ))
}

/// `_search_relevant`: an explicit search directive in the last user message.
pub fn search_relevant(history: &[Msg]) -> bool {
    let last = history.iter().rev().find(|m| m.role == "user").map(|m| m.content.as_str()).unwrap_or("");
    search_request().is_match(&request_text(last))
}

/// Python's `text[m.start():m.end() + 40]`: the match and the 40 characters after it.
fn fragment(text: &str, m: regex::Match) -> String {
    let tail: String = text[m.end()..].chars().take(40).collect();
    format!("{}{tail}", m.as_str())
}

/// One of the request's texts has a match whose fragment the guard does not veto.
fn requested(history: &[Msg], request: &Regex, guard: &Regex) -> bool {
    request_texts(history).iter().any(|raw| {
        let text = request_text(raw);
        request.find_iter(&text).any(|m| !guard.is_match(&fragment(&text, m)))
    })
}

/// `_image_requested`: a verb of creation then a visual object (or a follow-up on generated images).
pub fn image_requested(history: &[Msg]) -> bool {
    static REQ: OnceLock<Regex> = OnceLock::new();
    static NOT: OnceLock<Regex> = OnceLock::new();
    let request = re(&REQ, concat!(
        r"(?i)\b(?:g[ée]n[éeè]r\w*|",
        r"cree|crée|creer|créer|créé|créée|créés|créées|veux|voudrais|",
        r"aimerais|fais|fait|faire|faites|refais|refait|generate|create|make|design)\b",
        r"[^.!?\n]{0,40}?",
        r"(?:\b(?:une?|des|les|ces|mes|la|le|an?|\d+|deux|trois|quatre|cinq)\s+|\bl')",
        r"(?:image|dessin|illustration|logo|ic[ôo]ne|avatar|visuel|affiche|",
        r"flyer|poster|photo|picture|drawing|icon)s?\b",
        r"|\bdessine\b[^.!?\n]{0,40}",
        r"|\bdraw\b[^.!?\n]{0,40}"
    ));
    let guard = re(&NOT, r"(?i)\b(?:docker|conteneur|container|oci|disque|disk|iso|syst[èe]me|systeme|kernel)\b");
    requested(history, request, guard) || previous_answer(history).contains("/image/file/")
}

/// `_video_requested`.
pub fn video_requested(history: &[Msg]) -> bool {
    static REQ: OnceLock<Regex> = OnceLock::new();
    static NOT: OnceLock<Regex> = OnceLock::new();
    let request = re(&REQ, concat!(
        r"(?i)\b(?:genere|génère|generer|générer|cree|crée|creer|créer|veux|voudrais|",
        r"aimerais|fais|faites|realise|réalise|realiser|réaliser|generate|create|make|",
        r"animate|design)\b",
        r"[^.!?\n]{0,40}?",
        r"\b(?:une?|des|an?|the|a)\s+(?:\S+\s+){0,2}?",
        r"(?:vid[ée]os?|films?|animations?|clips?|s[ée]quences?|movies?)\b"
    ));
    let guard = re(&NOT, concat!(
        r"(?i)\b(?:css|html|web|react|vue|svelte|composant|interface|bouton|transition|",
        r"page|hover|survol|chargement|loading|curseur|plastique|protecteur|protection|",
        r"alimentaire|étirable|etirable|adh[ée]sif|isolant|windows|bios|presse-papiers)\b"
    ));
    requested(history, request, guard)
}

/// `_document_requested`: a reading verb next to a document object, or « OCR ».
pub fn document_requested(history: &[Msg]) -> bool {
    const VERBS: &str = concat!(
        r"lis|lire|lit|transcris|transcrire|transcription|extrais|",
        r"extraire|extraction|reconnais|reconna[îi]tre|saisis|saisir|",
        r"d[ée]chiffre|d[ée]chiffrer|d[ée]crypte|d[ée]crypter|",
        r"lecture|ocr|que contient|que dit|read|transcribe|extract"
    );
    const OBJECTS: &str = concat!(
        r"documents?|pi[èe]ces? jointes?|fichiers? joints?|images?|",
        r"pictures?|photos?|factures?|invoices?|re[çc]us?|receipts?|",
        r"tickets?|bons? de commande|formulaire|contrats?|notes? de ",
        r"frais|manuscrits?|lettres?|scans?|num[ée]ris[ée]s?|textes?|",
        r"captures? d.?[eé]cran|attestations?|bulletins?|pages?"
    );
    static REQ: OnceLock<Regex> = OnceLock::new();
    static NOT: OnceLock<Regex> = OnceLock::new();
    let request = REQ.get_or_init(|| {
        Regex::new(&format!(r"(?i)\b(?:{VERBS})\b[^.!?\n]{{0,40}}\b(?:{OBJECTS})\b|\b(?:{OBJECTS})\b[^.!?\n]{{0,30}}\b(?:{VERBS})\b|\bocr\b")).expect("static regex")
    });
    let guard = re(&NOT, concat!(
        r"(?i)\b(?:web|internet|en ligne|site|url|adresse|html|code|source|répertoire|",
        r"depot|dépôt|github|documentation|manuel|docker|conteneur|container|disque|",
        r"disk|iso|syst[èe]me|systeme)\b"
    ));
    requested(history, request, guard)
}

/// `file_tools.edit_requested`: the request asks to CHANGE something.
pub fn edit_requested(history: &[Msg]) -> bool {
    static C: OnceLock<Regex> = OnceLock::new();
    let edit = re(&C, concat!(
        r"(?i)\b(?:modif|chang|corrig|r[ée]par|ajout|rajout|remplac|supprim|enl[eè]v|retir|",
        r"renomm|d[ée]plac|mets?\b|passe\b|am[ée]lior|optimis|refactor|r[ée][ée]cri|adapt|",
        r"fix|edit|updat|add|remov|delet|renam|replac|improv|rewrit|tweak|mov)",
        r"|\b(?:en fai(?:t|te)|plut[oô]t|au lieu|[àa] la place|pas \S+(?: \S+){0,3} mais|",
        r"int[ée]gr|inclu|utilis|bascul|transform|convert|",
        r"actually|instead|rather|switch|use\b|make it|turn it)"
    ));
    request_texts(history).iter().any(|t| edit.is_match(&t.chars().take(4000).collect::<String>()))
}

/// `conversation_search.refers_to_past`: the last user message points to an earlier conversation.
pub fn refers_to_past(history: &[Msg]) -> bool {
    static C: OnceLock<Regex> = OnceLock::new();
    let past = re(&C, concat!(
        r"(?i)\b(?:la derni[eè]re fois|l'autre jour|on (?:avait|a) (?:parl[ée]|vu|fait|dit)|",
        r"tu te (?:souviens|rappelles)|(?:conversation|discussion|[ée]change) ",
        r"(?:pr[ée]c[ée]dente|d'avant|d'hier|pass[ée]e)|d[ée]j[aà] (?:parl[ée]|discut[ée]|vu ensemble)|",
        r"comme (?:je t'ai dit|on a dit|convenu)|hier on|",
        r"last time|the other day|we (?:talked|discussed|spoke) about|as (?:we|I) (?:discussed|said|mentioned)|",
        r"(?:previous|earlier|past|last) (?:chat|conversation|discussion)|do you remember|you remember)"
    ));
    history.iter().rev().find(|m| m.role == "user").is_some_and(|m| past.is_match(&m.content.chars().take(4000).collect::<String>()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    /// Verdicts computed by the Python triggers on the same conversations.
    #[test]
    fn matches_python_on_the_fixture() {
        let fx: Value = serde_json::from_str(include_str!("../../tests/fixtures/chat_triggers.json")).unwrap();
        for case in fx.as_array().unwrap() {
            let h: Vec<Msg> = case["history"].as_array().unwrap().iter()
                .map(|m| Msg::text(m["role"].as_str().unwrap(), m["content"].as_str().unwrap()))
                .collect();
            let label = format!("{:?}", case["history"]);
            assert_eq!(search_relevant(&h), case["search"].as_bool().unwrap(), "search {label}");
            assert_eq!(image_requested(&h), case["image"].as_bool().unwrap(), "image {label}");
            assert_eq!(video_requested(&h), case["video"].as_bool().unwrap(), "video {label}");
            assert_eq!(document_requested(&h), case["document"].as_bool().unwrap(), "document {label}");
            assert_eq!(edit_requested(&h), case["edit"].as_bool().unwrap(), "edit {label}");
            assert_eq!(refers_to_past(&h), case["past"].as_bool().unwrap(), "past {label}");
            assert_eq!(request_texts(&h), case["texts"].as_array().unwrap().iter().map(|t| t.as_str().unwrap().to_string()).collect::<Vec<_>>(), "texts {label}");
        }
    }
}
