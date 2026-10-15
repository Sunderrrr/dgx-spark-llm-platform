//! Normalized name of a memory node, parity with memory_routes.py `_mem_norm`.
//!
//! The graph needs « vLLM », « vllm » and « VLLM » to be one node. Python's rule
//! is reproduced exactly: lowercase, NFKD-fold latin characters only (other
//! scripts are kept as they are), then every run of non-word characters becomes
//! a space; the word class is Python's `\w` (letters, digits, underscore).
use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;
use unicode_properties::{GeneralCategory, UnicodeGeneralCategory};

pub const MEM_MAX_NAME_LEN: usize = 120;

/// Python's `\w` in unicode mode: `_`, or any character for which `str.isalnum()`
/// holds (letters Lm/Lt/Lu/Ll/Lo and numbers Nd/Nl/No).
pub fn is_word(c: char) -> bool {
    if c == '_' {
        return true;
    }
    matches!(
        c.general_category(),
        GeneralCategory::ModifierLetter
            | GeneralCategory::TitlecaseLetter
            | GeneralCategory::UppercaseLetter
            | GeneralCategory::LowercaseLetter
            | GeneralCategory::OtherLetter
            | GeneralCategory::DecimalNumber
            | GeneralCategory::LetterNumber
            | GeneralCategory::OtherNumber
    )
}

/// `_mem_norm(name)`.
pub fn mem_norm(name: &str) -> String {
    let mut folded = String::new();
    for ch in name.trim().to_lowercase().chars() {
        let decomposed: Vec<char> = ch.to_string().nfkd().collect();
        if decomposed.first().is_some_and(|c| c.is_ascii()) {
            folded.extend(decomposed.iter().filter(|c| !is_combining_mark(**c)));
        } else {
            folded.push(ch);
        }
    }
    // re.sub(r'[^\w]+', ' ', …) then the underscores become spaces.
    let mut spaced = String::new();
    let mut in_gap = false;
    for c in folded.chars() {
        if is_word(c) {
            spaced.push(c);
            in_gap = false;
        } else if !in_gap {
            spaced.push(' ');
            in_gap = true;
        }
    }
    let spaced = spaced.replace('_', " ");
    // re.sub(r'\s+', ' ', …).strip()[:120]: Python's \s is Unicode whitespace.
    let mut collapsed = String::new();
    let mut pending_space = false;
    for c in spaced.chars() {
        if c.is_whitespace() {
            pending_space = true;
        } else {
            if pending_space && !collapsed.is_empty() {
                collapsed.push(' ');
            }
            pending_space = false;
            collapsed.push(c);
        }
    }
    collapsed.chars().take(MEM_MAX_NAME_LEN).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    /// The reference set produced by the Python function (tests/fixtures/mem_norm.json).
    #[test]
    fn matches_the_python_reference_on_1512_cases() {
        let cases: Vec<Value> = serde_json::from_str(include_str!("../tests/fixtures/mem_norm.json")).unwrap();
        let mut bad = Vec::new();
        for c in &cases {
            let input = c["in"].as_str().unwrap();
            let expected = c["out"].as_str().unwrap();
            let got = mem_norm(input);
            if got != expected {
                bad.push(format!("{input:?}: python {expected:?} rust {got:?}"));
            }
        }
        assert!(bad.is_empty(), "{} mismatches, first: {:?}", bad.len(), bad.iter().take(5).collect::<Vec<_>>());
    }
}
