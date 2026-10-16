//! The vllm-runner's rules, ported from runner.py (phase 1 of the Rust
//! migration). Pure logic first: the argument allow-lists and their validation,
//! the repository identity, the token check. Process supervision follows.
pub mod flags;
pub mod validate;

/// `_check_auth`: the Bearer token, compared in constant time.
pub fn token_ok(header: &str, expected: &str) -> bool {
    let token = header.strip_prefix("Bearer ").unwrap_or("");
    let (a, b) = (token.as_bytes(), expected.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

#[cfg(test)]
mod tests {
    use super::token_ok;

    #[test]
    fn bearer_token_is_compared_exactly() {
        assert!(token_ok("Bearer secret-token-0123", "secret-token-0123"));
        assert!(!token_ok("Bearer secret-token-012", "secret-token-0123"));
        assert!(!token_ok("secret-token-0123", "secret-token-0123"));
        assert!(!token_ok("", "secret-token-0123"));
    }
}
