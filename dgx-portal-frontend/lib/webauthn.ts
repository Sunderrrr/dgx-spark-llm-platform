// Assistance WebAuthn côté client (passkeys / YubiKey / clé 1Password).
//
// Le backend renvoie des options au format `@simplewebauthn/browser` (challenge,
// userId, ids de clés en base64url). L'API `navigator.credentials` exige des
// BufferSource, donc on décode avant d'appeler create/get, puis on re-encode la
// réponse en JSON que `webauthn_routes.py` sait vérifier.
"use client";

function bytesFromBase64url(s: string): Uint8Array {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64url(buf: ArrayBuffer): string {
  const b = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Options du backend (objet simple, strings base64url) -> options DOM.
function prepareCreationOptions(pk: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const user = (pk.user ?? {}) as Record<string, unknown>;
  const opts = {
    ...pk,
    challenge: bytesFromBase64url(pk.challenge as string),
    user: { ...user, id: bytesFromBase64url(user.id as string) },
    excludeCredentials: ((pk.excludeCredentials as Record<string, unknown>[]) ?? []).map((c) => ({
      ...c,
      id: bytesFromBase64url(c.id as string),
    })),
  };
  // Le set complet (pubKeyCredParams, rp, attestation, …) vient du backend ;
  // le cast via `unknown` évite le contrôle structurel des types DOM.
  return opts as unknown as PublicKeyCredentialCreationOptions;
}

function prepareRequestOptions(pk: Record<string, unknown>): PublicKeyCredentialRequestOptions {
  const opts = {
    ...pk,
    challenge: bytesFromBase64url(pk.challenge as string),
    allowCredentials: ((pk.allowCredentials as Record<string, unknown>[]) ?? []).map((c) => ({
      ...c,
      id: bytesFromBase64url(c.id as string),
    })),
  };
  return opts as unknown as PublicKeyCredentialRequestOptions;
}

// Sérialise la réponse du navigateur dans le JSON que py_webauthn attend.
function credentialToJSON(cred: PublicKeyCredential): Record<string, unknown> {
  const resp = cred.response as AuthenticatorAttestationResponse | AuthenticatorAssertionResponse;
  const out: Record<string, unknown> = {
    id: cred.id,
    rawId: bytesToBase64url(cred.rawId),
    type: cred.type,
    response: {},
    clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
  };
  const r = out.response as Record<string, unknown>;
  r.clientDataJSON = bytesToBase64url(resp.clientDataJSON);
  if ("attestationObject" in resp) {
    r.attestationObject = bytesToBase64url((resp as AuthenticatorAttestationResponse).attestationObject);
    const transports = (resp as AuthenticatorAttestationResponse).getTransports?.();
    if (transports && transports.length) r.transports = transports;
  }
  if ("authenticatorData" in resp) {
    const a = resp as AuthenticatorAssertionResponse;
    r.authenticatorData = bytesToBase64url(a.authenticatorData);
    r.signature = bytesToBase64url(a.signature);
    r.userHandle = a.userHandle ? bytesToBase64url(a.userHandle) : null;
  }
  return out;
}

export async function createPasskey(publicKey: Record<string, unknown>): Promise<Record<string, unknown>> {
  const cred = await navigator.credentials.create({ publicKey: prepareCreationOptions(publicKey) });
  if (!cred) throw new Error("create-cancelled");
  return credentialToJSON(cred as unknown as PublicKeyCredential);
}

export async function getPasskeyAssertion(
  publicKey: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const cred = await navigator.credentials.get({ publicKey: prepareRequestOptions(publicKey) });
  if (!cred) throw new Error("create-cancelled");
  return credentialToJSON(cred as unknown as PublicKeyCredential);
}
