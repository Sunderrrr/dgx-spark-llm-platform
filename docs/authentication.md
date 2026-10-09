# Authentication and accounts

How people get in, how accounts are managed, and what deletion actually does.
Handled by [`auth.py`](../dgx-portal/auth.py). For the security posture around it,
see [security](security.md).

## Login methods

Login order (`login()` in [`app.py`](../dgx-portal/app.py)): `local_users` (hashed) →
**LDAP** → **SSO**. Two directory methods plus local accounts:

- **OIDC SSO (Authentik)** — primary, the SSO button. Flow:
  `/login/sso` → Authentik → `/api/oauth2-redirect`. Admin comes from the `groups`
  claim (the admin group), falling back to an LDAP lookup by username if absent.
- **LDAP (Authentik)** — username/password fallback: direct bind, injection-escaped,
  empty-password binds rejected, with brute-force lockout (6 fails / 15 min)
  persisted in SQLite so it survives a redeploy and is shared across workers.
  The counter is keyed **per IP and per username**: rotating source IPs can't
  dodge the threshold, since all attempts against one account feed the same
  counter regardless of origin.
- **Passkey 2FA (WebAuthn/FIDO2)** — optional per account (Settings ▸ Security),
  local and LDAP accounts only: registration and login challenges are single-use,
  and removing a passkey or switching 2FA off requires a password
  re-verification.

Local accounts managed by an admin ([`local_users.py`](../dgx-portal/local_users.py))
sit between the two: a hashed, admin-managed account system checked before
LDAP/SSO.

Each successful login records its source in the `user_sources` table
(`local` / `ldap` / `sso`, cumulative). The **Users** page shows every known account
with a badge per source — **Local**, **LDAP**, **SSO** or **External**.

**Account state is re-read on every request, never frozen at login.** A session
carries a name and a role copied at sign-in; each guarded request re-checks the
account before serving, so deleting, disabling, blocking or demoting an account
takes effect immediately instead of at cookie expiry (`SESSION_MAX_AGE`, 12 h by
default). For a **local** account the portal is the authority on the role, since
it owns `local_users`; for a directory account the role follows the last login
recorded — LDAP cannot be queried per request, and the portal never *infers* a
demotion from a record that is merely absent (the first version of the fix did,
and logged everyone out).

## Local accounts

Local user management is admin-only: create users, assign groups with quota/admin
rights, hashed passwords. Budget precedence: user override → group → global
default. The password policy (`local_users.password_policy_error`, shared by admin
creation and self-service change): 8 characters minimum, refusal of the most common
passwords, of the account name contained in the password, and of overly repetitive
sequences. No forced rotation nor character classes: they push towards predictable
passwords. Deleting a **group** resynchronizes its members onto their new effective
cap.

**Where the password lives, the portal says it** (`local_users.gestion_mot_de_passe`):
a `local_users` row is authoritative (« portail »), else ldap → « annuaire », else
sso → « fournisseur d'identité », else `inconnu` — a case where the card explains
where to change it instead of showing a doomed form, and where 2FA disappears.
A local account can **change its own password** (Settings → Security,
`POST /api/account/password`): it requires the current password, enforces the
policy, and closes every *other* session while keeping the current one. Directory
accounts are told their password lives in the directory rather than shown a form
that cannot work.

**Passkeys** (WebAuthn/FIDO2, not TOTP) are bound to the **origin**
(`WEBAUTHN_ORIGIN`, e.g. `https://dgx.cronos.website`) — a key registered there
does not work from `http://dgx.cronos.lan`. `WEBAUTHN_REQUIRE_UV` (default off)
accepts "touch-only" keys; enabling it would require PIN/biometrics. Any
deletion/disabling requires a password re-verification. One-time challenges are
stored in `pending_webauthn`, TTL 5 min.

## Blocking

**Blocking** (`Users → Bloquer`, `blocked_users` table) refuses an account at
login whatever its authentication source, and revokes its open sessions. It is
the only lever that works for an **LDAP/SSO** account, which has no local row to
disable — before it, the only option was revoking sessions, and the account
simply logged in again. It is reversible and leaves the local account untouched.
The refusal is checked **after** credential verification (a blocked account must
not serve to enumerate accounts), and also in the passkey login finish: the
passkey proves identity, it does not equal authorization.

## Deleting and purging

**Deleting an account is a deprovisioning, not a row removal.** It revokes the
sessions, revokes every API key on LiteLLM, deletes the LiteLLM user envelope
(budget *and* accumulated spend — otherwise an account re-created under the same
name inherits the previous one's spend) and purges the personal data: memory,
conversations, share links, preferences, passkeys, media jobs. It requires an
explicit `confirm=DELETE`, refuses self-deletion and refuses to remove the last
local administrator. If LiteLLM is unreachable the deletion still goes through —
better an account gone from the portal than a stuck one — but the response **and**
the audit trail name the keys that could not be revoked. `audit_log` is kept on
purpose: it is the record of admin actions and outlives the account.

The two gestures are not interchangeable, and the interface says so: **deleting** a
local account removes its access *and* erases its data, while **purging** an
LDAP/SSO account erases its data only — that account can sign in again and will
simply start from an empty account (`POST /admin/users/<username>/purge`, audit
`user.purge`). Removing access is what **blocking** is for. Since 2026-09-16 the
person concerned can delete themselves (`POST /api/account/delete`): `confirm=DELETE`,
password required for local accounts, "last local admin" guard.

The portal refuses to leave itself without a local administrator: an admin cannot
disable or demote **themselves** (the role is re-read on every request, so the loss
of access would be immediate and there would be no session left to undo it), and the
**last** active local admin cannot be removed — not by deleting the account, not by
disabling it, and not by deleting the group that carries its rights. Admin rights
come from `local_users.is_admin` *or* from the group, which is why removing a group
is checked too.

## Sessions

Sessions are **server-revocable**: the signed cookie carries only a random
`sid`, and a `user_sessions` row (same SQLite) records its creation time, IP and
user-agent. An admin can kill any active session (`POST
/admin/users/<username>/revoke-sessions`), and every account sees **its own**
sessions under **Settings → Security** and can close one or all the others
(`GET /api/account/sessions`, `POST /api/account/sessions/revoke`). Sessions
predating the registry expire by age only, so the migration logs nobody out.

Session hardening: `HttpOnly` + `SameSite=Lax` cookies + `Secure` behind TLS.
`ProxyFix` trusts Traefik's `X-Forwarded-*` headers. The CSRF token **survives
login** (2026-09-14): regenerating it there changed the token behind the browser's
back and every kept POST failed on logout after an SSO login. The token lives in a
signed, HttpOnly cookie — the protection against session fixation is the fresh
`sid` created at every login.

## Avatars

The default avatar is **generated** (blobatar, deterministic from the handle —
nothing to store, no network call) and `user_prefs.avatar_id IS NULL` means
exactly that. `/settings/avatar` accepts the empty value to go back to it; the 15
brand logos remain selectable and **win** over the generated one.
