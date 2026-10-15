//! Should the model THINK about this request? (dgx-portal reasoning.py)
//! Rather under-trigger: thinking costs tokens and time; a « pourquoi… » is
//! worth it, a « merci » is not.

/// A single one of these words makes the request « worth thinking about ».
const MARKERS: &[&str] = &[
    "pourquoi", "comment faire", "comment puis-je", "explique", "expliquer",
    "démontre", "prouve", "raisonne", "réfléchis", "étape par étape",
    "step by step", "pas à pas",
    "compare", "comparer", "analyse", "analyser", "évalue", "évaluer",
    "optimise", "optimiser", "arbitrage", "trade-off", "avantages",
    "inconvénients", "risques", "meilleur", "meilleure", "que choisir",
    "lesquels", "laquelle", "vaut-il", "faut-il",
    "corrige", "corriger", "debug", "débogue", "erreur", "traceback",
    "stacktrace", "bug", "algorithme", "complexité", "sql", "script",
    "fonction", "requête", "architecture", "migration", "sécurité", "audit",
    "calcule", "calculer", "équation", "probabilité", "statistique",
    "stratégie", "plan", "planifier", "organise", "étapes", "procédure",
];

/// Structural signs: code or maths in the request.
const CODE_MARKERS: &[&str] = &["```", "def ", "class ", "function ", "SELECT ", "INSERT ", "import ", "=>", "->", "docker ", "systemctl"];

/// Python's `str.isdigit()`: decimal digits plus the other « digit » characters
/// (superscripts, subscripts, circled and parenthesised digits…).
pub fn py_isdigit(c: char) -> bool {
    use unicode_properties::{GeneralCategory, UnicodeGeneralCategory};
    if c.general_category() == GeneralCategory::DecimalNumber {
        return true;
    }
    matches!(c as u32,
        0x00B2 | 0x00B3 | 0x00B9 | 0x1369..=0x1371 | 0x19DA | 0x2070 | 0x2074..=0x2079 | 0x2080..=0x2089
        | 0x2460..=0x2468 | 0x2474..=0x247C | 0x2488..=0x2490 | 0x24EA | 0x24F5..=0x24FD | 0x24FF
        | 0x2776..=0x277E | 0x2780..=0x2788 | 0x278A..=0x2792 | 0x10A40..=0x10A43 | 0x10E60..=0x10E68
        | 0x11052..=0x1105A | 0x1F100..=0x1F10A)
}

/// `reasonably_complex`: true when the request warrants a reasoning pass.
pub fn reasonably_complex(text: &str) -> bool {
    if text.trim_matches(crate::memory::py_is_space).is_empty() {
        return false;
    }
    let low = text.to_lowercase();
    // 1. explicit invitation to think
    if ["réfléchis", "raisonne", "étape par étape", "step by step", "pas à pas", "réfléchir"].iter().any(|m| low.contains(m)) {
        return true;
    }
    // 2. code / maths
    if CODE_MARKERS.iter().any(|m| text.contains(m)) {
        return true;
    }
    if text.chars().filter(|c| py_isdigit(*c)).count() >= 4 && ['+', '-', '*', '/', '=', '%'].iter().any(|op| text.contains(*op)) {
        return true;
    }
    // 3. an analysis marker
    if MARKERS.iter().any(|m| low.contains(m)) {
        return true;
    }
    // 4. several questions, or a long request
    text.matches('?').count() >= 2 || text.chars().count() >= 500
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Verdicts computed by dgx-portal/reasoning.py on the same texts.
    #[test]
    fn matches_python_on_the_fixture() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!("../../tests/fixtures/reasoning.json")).unwrap();
        for case in fixture.as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            assert_eq!(reasonably_complex(text), case["expected"].as_bool().unwrap(), "{text:?}");
        }
    }

    #[test]
    fn digits_like_python() {
        assert!(py_isdigit('7') && py_isdigit('²') && py_isdigit('٣') && py_isdigit('①'));
        assert!(!py_isdigit('½') && !py_isdigit('a'));
    }
}
