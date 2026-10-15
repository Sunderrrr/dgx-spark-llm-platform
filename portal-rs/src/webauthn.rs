//! Passkeys, the WebAuthn protocol (webauthn_routes.py on py_webauthn 3.0.0):
//! the registration and assertion checks, the COSE public keys, the attestation
//! formats. Pure functions, so the rules can be tested without a browser.
//!
//! Scope, on purpose: the algorithms are the ones py_webauthn accepts by
//! default (ES256, RS256, EdDSA); the attestation formats are `none`,
//! `packed` (self, or with an `x5c` certificate checked without roots, as
//! Flask does when no root is configured) and `fido-u2f`. The formats tpm,
//! apple and android-key are refused: a refused key is said so, never
//! accepted unchecked.
use ciborium::value::Value as Cbor;
use ed25519_dalek::{Signature as EdSig, Verifier as _};
use p256::ecdsa::{Signature as EcSig, VerifyingKey as EcKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const ES256: i64 = -7;
pub const RS256: i64 = -257;
pub const EDDSA: i64 = -8;

pub fn b64url(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// base64url, padding optional (py_webauthn and the browser may send either).
pub fn unb64url(s: &str) -> Option<Vec<u8>> {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s.trim_end_matches('=')).ok()
}

fn sha256(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

/// `parse_client_data_json`, with the token-binding check of py_webauthn.
pub struct ClientData {
    pub typ: String,
    pub challenge: Vec<u8>,
    pub origin: String,
}

pub fn client_data(raw: &[u8]) -> Result<ClientData, String> {
    let v: Value = serde_json::from_slice(raw).map_err(|_| "clientDataJSON malformed")?;
    let text = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_string).ok_or_else(|| format!("clientDataJSON has no {k}"));
    if let Some(tb) = v.get("tokenBinding") {
        let status = tb.get("status").and_then(Value::as_str).unwrap_or("");
        if status != "supported" && status != "present" {
            return Err("unexpected token_binding status".into());
        }
    }
    Ok(ClientData {
        typ: text("type")?,
        challenge: unb64url(&text("challenge")?).ok_or("challenge is not base64url")?,
        origin: text("origin")?,
    })
}

pub struct AttCred {
    pub cred_id: Vec<u8>,
    /// The COSE key exactly as the authenticator wrote it: what is stored.
    pub cose: Vec<u8>,
}

pub struct AuthData {
    pub rp_id_hash: [u8; 32],
    pub flags: u8,
    pub sign_count: u32,
    pub cred: Option<AttCred>,
}

impl AuthData {
    pub fn up(&self) -> bool {
        self.flags & 0x01 != 0
    }
    pub fn uv(&self) -> bool {
        self.flags & 0x04 != 0
    }
}

/// `parse_authenticator_data`: the fixed head, then the attested credential
/// when the AT flag says so.
pub fn auth_data(b: &[u8]) -> Result<AuthData, String> {
    if b.len() < 37 {
        return Err("authenticatorData too short".into());
    }
    let mut rp_id_hash = [0u8; 32];
    rp_id_hash.copy_from_slice(&b[..32]);
    let flags = b[32];
    let sign_count = u32::from_be_bytes([b[33], b[34], b[35], b[36]]);
    let cred = if flags & 0x40 != 0 {
        if b.len() < 55 {
            return Err("attested credential data truncated".into());
        }
        let id_len = u16::from_be_bytes([b[53], b[54]]) as usize;
        let id_end = 55 + id_len;
        if b.len() < id_end {
            return Err("credential id truncated".into());
        }
        let cred_id = b[55..id_end].to_vec();
        let mut cur = std::io::Cursor::new(&b[id_end..]);
        let _key: Cbor = ciborium::de::from_reader(&mut cur).map_err(|_| "credential public key malformed")?;
        let n = cur.position() as usize;
        Some(AttCred { cred_id, cose: b[id_end..id_end + n].to_vec() })
    } else {
        None
    };
    Ok(AuthData { rp_id_hash, flags, sign_count, cred })
}

/// A credential's public key, as py_webauthn accepts it.
pub enum PubKey {
    Ec(EcKey),
    Rsa(rsa::RsaPublicKey),
    Ed(ed25519_dalek::VerifyingKey),
}

pub struct CoseKey {
    pub key: PubKey,
    pub alg: i64,
    /// The uncompressed EC point (0x04 || x || y), for fido-u2f.
    pub ec_point: Option<Vec<u8>>,
}

fn int_key(v: &Cbor) -> Option<i64> {
    match v {
        Cbor::Integer(i) => i64::try_from(i128::from(*i)).ok(),
        _ => None,
    }
}

fn field<'a>(map: &'a [(Cbor, Cbor)], key: i64) -> Option<&'a Cbor> {
    map.iter().find(|(k, _)| int_key(k) == Some(key)).map(|(_, v)| v)
}

fn bytes(v: Option<&Cbor>) -> Option<&[u8]> {
    match v? {
        Cbor::Bytes(b) => Some(b),
        _ => None,
    }
}

/// `decode_credential_public_key`: EC2/P-256 (ES256), RSA (RS256), OKP/Ed25519 (EdDSA).
pub fn cose_key(raw: &[u8]) -> Result<CoseKey, String> {
    let v: Cbor = ciborium::de::from_reader(raw).map_err(|_| "COSE key malformed")?;
    let Cbor::Map(map) = v else { return Err("COSE key is not a map".into()) };
    let kty = field(&map, 1).and_then(int_key).ok_or("COSE key without kty")?;
    let alg = field(&map, 3).and_then(int_key).ok_or("COSE key without alg")?;
    match (kty, alg) {
        (2, ES256) => {
            if field(&map, -1).and_then(int_key) != Some(1) {
                return Err("unsupported EC curve".into());
            }
            let x = bytes(field(&map, -2)).filter(|b| b.len() == 32).ok_or("bad EC x")?;
            let y = bytes(field(&map, -3)).filter(|b| b.len() == 32).ok_or("bad EC y")?;
            let mut point = vec![0x04];
            point.extend_from_slice(x);
            point.extend_from_slice(y);
            let key = EcKey::from_sec1_bytes(&point).map_err(|_| "invalid EC point")?;
            Ok(CoseKey { key: PubKey::Ec(key), alg, ec_point: Some(point) })
        }
        (3, RS256) => {
            let n = bytes(field(&map, -1)).ok_or("bad RSA n")?;
            let e = bytes(field(&map, -2)).ok_or("bad RSA e")?;
            let key = rsa::RsaPublicKey::new(rsa::BigUint::from_bytes_be(n), rsa::BigUint::from_bytes_be(e)).map_err(|_| "invalid RSA key")?;
            Ok(CoseKey { key: PubKey::Rsa(key), alg, ec_point: None })
        }
        (1, EDDSA) => {
            if field(&map, -1).and_then(int_key) != Some(6) {
                return Err("unsupported OKP curve".into());
            }
            let x: [u8; 32] = bytes(field(&map, -2)).and_then(|b| b.try_into().ok()).ok_or("bad OKP x")?;
            let key = ed25519_dalek::VerifyingKey::from_bytes(&x).map_err(|_| "invalid Ed25519 key")?;
            Ok(CoseKey { key: PubKey::Ed(key), alg, ec_point: None })
        }
        _ => Err(format!("unsupported credential public key (kty {kty}, alg {alg})")),
    }
}

/// `verify_signature`: the key's own algorithm, over the raw data.
pub fn verify_sig(key: &PubKey, sig: &[u8], data: &[u8]) -> bool {
    match key {
        PubKey::Ec(k) => EcSig::from_der(sig).is_ok_and(|s| k.verify(data, &s).is_ok()),
        PubKey::Rsa(k) => {
            use rsa::signature::Verifier as _;
            let vk = rsa::pkcs1v15::VerifyingKey::<Sha256>::new(k.clone());
            rsa::pkcs1v15::Signature::try_from(sig).is_ok_and(|s| vk.verify(data, &s).is_ok())
        }
        PubKey::Ed(k) => EdSig::from_slice(sig).is_ok_and(|s| k.verify(data, &s).is_ok()),
    }
}

/// The certificate's key, for `x5c` attestations (its chain is not checked:
/// py_webauthn checks it only against configured roots, and none are set).
fn cert_key(der: &[u8]) -> Result<PubKey, String> {
    let (_, cert) = x509_parser::parse_x509_certificate(der).map_err(|_| "attestation certificate malformed")?;
    match cert.public_key().parsed().map_err(|_| "attestation key unreadable")? {
        x509_parser::public_key::PublicKey::EC(point) => {
            let key = EcKey::from_sec1_bytes(point.data()).map_err(|_| "attestation EC key unsupported")?;
            Ok(PubKey::Ec(key))
        }
        x509_parser::public_key::PublicKey::RSA(r) => {
            let key = rsa::RsaPublicKey::new(rsa::BigUint::from_bytes_be(r.modulus), rsa::BigUint::from_bytes_be(r.exponent)).map_err(|_| "attestation RSA key invalid")?;
            Ok(PubKey::Rsa(key))
        }
        _ => Err("attestation key type unsupported".into()),
    }
}

/// `verify_*` of the attestation statement, per format.
fn verify_attestation(fmt: &str, stmt: &[(Cbor, Cbor)], auth_raw: &[u8], auth: &AuthData, cdh: &[u8; 32], cose: &CoseKey) -> Result<(), String> {
    let x5c: Option<Vec<Vec<u8>>> = match field_text_value(stmt, "x5c") {
        Some(Cbor::Array(items)) => Some(items.iter().filter_map(|i| bytes(Some(i)).map(<[u8]>::to_vec)).collect()),
        _ => None,
    };
    let stmt_alg = field_text_value(stmt, "alg").and_then(int_key);
    let sig = || field_text_value(stmt, "sig").and_then(|v| bytes(Some(v))).ok_or("attestation without sig");
    match fmt {
        "none" => {
            if stmt.is_empty() {
                Ok(())
            } else {
                Err("none attestation with a statement".into())
            }
        }
        "packed" => {
            if stmt_alg != Some(cose.alg) {
                return Err("attestation alg differs from the credential's".into());
            }
            let mut data = auth_raw.to_vec();
            data.extend_from_slice(cdh);
            let key = match &x5c {
                Some(chain) if !chain.is_empty() => cert_key(&chain[0])?,
                _ => clone_key(cose)?,
            };
            if verify_sig(&key, sig()?, &data) {
                Ok(())
            } else {
                Err("packed attestation signature invalid".into())
            }
        }
        "fido-u2f" => {
            let chain = x5c.filter(|c| !c.is_empty()).ok_or("fido-u2f without x5c")?;
            let point = cose.ec_point.as_ref().ok_or("fido-u2f needs an EC credential")?;
            let cred_id = &auth.cred.as_ref().ok_or("no credential")?.cred_id;
            let mut data = vec![0u8];
            data.extend_from_slice(&auth.rp_id_hash);
            data.extend_from_slice(cdh);
            data.extend_from_slice(cred_id);
            data.extend_from_slice(point);
            if verify_sig(&cert_key(&chain[0])?, sig()?, &data) {
                Ok(())
            } else {
                Err("fido-u2f attestation signature invalid".into())
            }
        }
        other => Err(format!("attestation format {other} is not supported")),
    }
}

/// The credential's own key, as a second owned value (a verifier takes it by value).
fn clone_key(cose: &CoseKey) -> Result<PubKey, String> {
    match &cose.key {
        PubKey::Ec(k) => Ok(PubKey::Ec(*k)),
        PubKey::Rsa(k) => Ok(PubKey::Rsa(k.clone())),
        PubKey::Ed(k) => Ok(PubKey::Ed(*k)),
    }
}

/// A registration that passed every check: the id, the key to store, its counter.
pub struct Registered {
    pub cred_id: Vec<u8>,
    pub cose: Vec<u8>,
    pub sign_count: u32,
}

/// `verify_registration_response` (up presence required, RP ID, origin, challenge).
pub fn verify_registration(cred: &Value, challenge: &[u8], rp_id: &str, origin: &str, require_uv: bool) -> Result<Registered, String> {
    check_envelope(cred, "public-key")?;
    let resp = cred.get("response").ok_or("no response")?;
    let cd_raw = resp.get("clientDataJSON").and_then(Value::as_str).and_then(unb64url).ok_or("clientDataJSON missing")?;
    let att_raw = resp.get("attestationObject").and_then(Value::as_str).and_then(unb64url).ok_or("attestationObject missing")?;
    let cd = client_data(&cd_raw)?;
    if cd.typ != "webauthn.create" || cd.challenge != challenge || cd.origin != origin {
        return Err("client data does not match the challenge".into());
    }
    let att: Cbor = ciborium::de::from_reader(att_raw.as_slice()).map_err(|_| "attestationObject malformed")?;
    let Cbor::Map(att) = att else { return Err("attestationObject is not a map".into()) };
    let fmt = match field_text(&att, "fmt") {
        Some(f) => f,
        None => return Err("attestationObject without fmt".into()),
    };
    let stmt = match field_text_value(&att, "attStmt") {
        Some(Cbor::Map(m)) => m.clone(),
        _ => Vec::new(),
    };
    let auth_raw = bytes(field_text_value(&att, "authData")).ok_or("authData missing")?.to_vec();
    let auth = auth_data(&auth_raw)?;
    if auth.rp_id_hash != sha256(rp_id.as_bytes()) {
        return Err("unexpected RP ID hash".into());
    }
    if !auth.up() {
        return Err("user presence required".into());
    }
    if require_uv && !auth.uv() {
        return Err("user verification required".into());
    }
    let att_cred = auth.cred.as_ref().ok_or("no attested credential data")?;
    let cose = cose_key(&att_cred.cose)?;
    verify_attestation(&fmt, &stmt, &auth_raw, &auth, &sha256(&cd_raw), &cose)?;
    Ok(Registered { cred_id: att_cred.cred_id.clone(), cose: att_cred.cose.clone(), sign_count: auth.sign_count })
}

/// `verify_authentication_response`: the assertion's signature by the stored
/// key, the counter that must grow, then the same envelope checks.
pub fn verify_assertion(cred: &Value, challenge: &[u8], rp_id: &str, origin: &str, stored_cose: &[u8], stored_count: i64, require_uv: bool) -> Result<u32, String> {
    check_envelope(cred, "public-key")?;
    let resp = cred.get("response").ok_or("no response")?;
    let cd_raw = resp.get("clientDataJSON").and_then(Value::as_str).and_then(unb64url).ok_or("clientDataJSON missing")?;
    let auth_raw = resp.get("authenticatorData").and_then(Value::as_str).and_then(unb64url).ok_or("authenticatorData missing")?;
    let sig = resp.get("signature").and_then(Value::as_str).and_then(unb64url).ok_or("signature missing")?;
    let cd = client_data(&cd_raw)?;
    if cd.typ != "webauthn.get" || cd.challenge != challenge || cd.origin != origin {
        return Err("client data does not match the challenge".into());
    }
    let auth = auth_data(&auth_raw)?;
    if auth.rp_id_hash != sha256(rp_id.as_bytes()) {
        return Err("unexpected RP ID hash".into());
    }
    if !auth.up() {
        return Err("user was not present".into());
    }
    if require_uv && !auth.uv() {
        return Err("user verification required".into());
    }
    let new = auth.sign_count as i64;
    if (new > 0 || stored_count > 0) && new <= stored_count {
        return Err("sign count did not grow: possible cloned authenticator".into());
    }
    let cose = cose_key(stored_cose)?;
    let mut data = auth_raw.clone();
    data.extend_from_slice(&sha256(&cd_raw));
    if !verify_sig(&cose.key, &sig, &data) {
        return Err("signature does not verify".into());
    }
    Ok(auth.sign_count)
}

/// `rawId` must be the `id`, and the type a public key.
fn check_envelope(cred: &Value, kind: &str) -> Result<(), String> {
    let id = cred.get("id").and_then(Value::as_str).ok_or("no id")?;
    let raw = cred.get("rawId").and_then(Value::as_str).and_then(unb64url).ok_or("no rawId")?;
    if b64url(&raw) != id.trim_end_matches('=') {
        return Err("id and raw_id were not equivalent".into());
    }
    if cred.get("type").and_then(Value::as_str) != Some(kind) {
        return Err("unexpected credential type".into());
    }
    Ok(())
}

fn field_text<'a>(map: &'a [(Cbor, Cbor)], key: &str) -> Option<String> {
    match field_text_value(map, key)? {
        Cbor::Text(t) => Some(t.clone()),
        _ => None,
    }
}

fn field_text_value<'a>(map: &'a [(Cbor, Cbor)], key: &str) -> Option<&'a Cbor> {
    map.iter().find(|(k, _)| matches!(k, Cbor::Text(t) if t == key)).map(|(_, v)| v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64url_both_ways() {
        assert_eq!(unb64url("YWJj"), Some(b"abc".to_vec()));
        assert_eq!(unb64url("YWI"), Some(b"ab".to_vec()));
        assert_eq!(b64url(b"abc"), "YWJj");
    }

    #[test]
    fn auth_data_flags_and_counter() {
        let mut b = vec![0u8; 37];
        b[32] = 0x05; // UP + UV
        b[33..37].copy_from_slice(&7u32.to_be_bytes());
        let a = auth_data(&b).unwrap();
        assert!(a.up() && a.uv());
        assert_eq!(a.sign_count, 7);
        assert!(a.cred.is_none());
    }

    #[test]
    fn es256_round_trip() {
        use p256::ecdsa::{signature::Signer, SigningKey};
        let sk = SigningKey::from_slice(&[7u8; 32]).unwrap();
        let point = sk.verifying_key().to_encoded_point(false);
        let mut cose = Vec::new();
        let map = vec![
            (Cbor::Integer(1.into()), Cbor::Integer(2.into())),
            (Cbor::Integer(3.into()), Cbor::Integer((-7).into())),
            (Cbor::Integer((-1).into()), Cbor::Integer(1.into())),
            (Cbor::Integer((-2).into()), Cbor::Bytes(point.x().unwrap().to_vec())),
            (Cbor::Integer((-3).into()), Cbor::Bytes(point.y().unwrap().to_vec())),
        ];
        ciborium::ser::into_writer(&Cbor::Map(map), &mut cose).unwrap();
        let key = cose_key(&cose).unwrap();
        let sig: EcSig = sk.sign(b"data");
        assert!(verify_sig(&key.key, &sig.to_der().to_bytes(), b"data"));
        assert!(!verify_sig(&key.key, &sig.to_der().to_bytes(), b"other"));
    }
}
