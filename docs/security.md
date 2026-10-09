# Security

The posture in one page, what is knowingly accepted, and where the full audit
lives. The complete threat model, the complete list of controls, and the risks
knowingly accepted (starting with OCR, the one sidecar allowed to execute
third-party code) are in [`SECURITY.md`](../SECURITY.md).

## Posture

The gateway refuses any call without a valid key and enforces budgets; vLLM and the
runner are firewalled to localhost plus the docker bridge, and the runner
allowlists every launch flag, requires a Bearer token and runs non-root. No
`docker.sock` is mounted by any sidecar **in this stack** — they are driven through
scoped `sudo` on root-owned wrapper scripts, each on its own docker network with no
route to LiteLLM, Postgres or Traefik. Elevated host access does exist *around* the
stack, and it is the operator's to review rather than something this repository
grants: a third-party control-plane agent the runner is designed for (socket + a
bearer token + an IP allowlist; currently stopped), and one third-party edge
agent outside compose with root, a writable socket and part of the host
filesystem. The second is the heaviest open item of the last audit — see
[`SECURITY.md`](../SECURITY.md) §2.2 and §3.3. The portal adds LDAP/SSO auth,
hardened cookies, CSRF, a persisted brute-force lockout and a per-request nonce
CSP, and a test fails the build if any route loses its authentication guard.

In detail: [`SECURITY.md`](../SECURITY.md) §2 covers model launching (a strict
allowlist, not a denylist), sidecar control, network isolation, session and
request integrity, input validation, resource abuse, maintenance mode, container
hardening, and why admin actions report what actually happened. Authentication
itself is described in [authentication](authentication.md).

## Exposing the API publicly

Path: `api.cronos.website` (**Cloudflare, proxied**) → **Traefik** →
`http://litellm:4001`. LiteLLM listens on **4001 inside the Docker network** and
publishes **no host port at all**; Traefik is attached to the same compose network
and reaches it by service name (the portal does the same, via `LITELLM_URL`). The
host name `dgx.cronos.lan:4001` that this page used to give answers nothing — do
not route to the host.
Keep the route on `4001`, never to the runner (`8001`) or the engine (`8000`).
Consider a per-key rate limit (rpm/tpm) in
LiteLLM and a Cloudflare rate rule before opening to the internet — budgets cap
tokens/day, not request rate on a single GPU.

API clients must send a real `User-Agent` (Cloudflare's browser-integrity check
rejects the default `Python-urllib/*` agent before the request reaches the
platform) — see [api](api.md).

## Accepted risks

- **OCR executes third-party code by design** ([`SECURITY.md`](../SECURITY.md)
  §3.1), which is why it sits on its own network with no route to the rest and
  `cronos-ocr-restrict.service` keeps it from opening connections to the portal.
- **"Admin-only means safe" is not assumed** (§3.2): sensitive actions run
  through an out-of-band confirmation, never on the model's word (see
  [features → Support](features.md#support-assistant)).
- **Known open items** (§3.3), starting with the third-party edge agent above.
- **Host services accepted, not vulnerabilities**: the box is LAN/netbird-only
  (no public IP; the internet path is Cloudflare → Traefik on 80/443). A few host
  services are **deliberately** exposed on `0.0.0.0` with no firewall DROP and
  are used by the operator. The specifics are operator-private and live in a
  machine-local, git-ignored note — do not copy that inventory into any public
  document. Only flag an *unexpected* new port or an internet-reachable one.

## Pen test and perimeter hardening (2026-10-06 / 2026-10-07)

The pen test of **2026-10-06** (three audits, two HIGH closed) is written up in
[`SECURITY.md`](../SECURITY.md) §3.5, with the 2026-10-02 audit in §3.4. Three
holes it named were closed at the **infrastructure** level on **2026-10-07**, not
in this repository's code — they live in systemd and Traefik on the host, and
must not be undone:

- **`/opt/traefik/dynamic/routes.yml`** carries an `ipallowlist-public`
  middleware on the `dgx` and `api` routers: only Cloudflare's published ranges
  plus the operator's own networks (LAN, Netbird, loopback) may reach the vhosts.
  Before it, a client hitting the ORIGIN IP with the right `Host` skipped every
  Cloudflare control and read `/metrics` (pen test M3).
- **`ip6tables` DOCKER-USER** now mirrors the IPv4 admin-port allowlist
  (3000/8080/8090 DROP): `docker-proxy` publishes the same ports on `[::]` and
  userland-proxy forwarding never passes the IPv4 chain (pen test M4). The mirror
  is also in `cronos-docker-restrict.service` so it survives a boot.
- **`comfyui-relay.service`** (socat 172.19.0.1:8188 → 127.0.0.1:8188) exists so
  the portal can reach ComfyUI, which deliberately listens on loopback; the
  firewall accepts 8188 from the PORTAL's address only (pen test H1). If the
  portal container is recreated its IP may change — update the rule in
  `vllm-restrict.service`.

The remaining operator actions are NOT code: rotating the Authentik→LiteLLM
OAuth secret and the SMTP mailbox identity (both were scrubbed from the git
history on 2026-10-07, and purging history does not make readers forget).

## Secret handling

A secret never goes in git — messages included. The incident of 2026-10-06: an
OAuth client secret was written into a *commit message*, and no hook looked at
messages. Three guards now cover it: `scripts/pre-commit-check.sh` (installed as
`.git/hooks/pre-commit`), `scripts/pre-push-check.sh` (same on the pushed range),
and the CI `secrets` job (the net for a `--no-verify` push). If one fires: the fix
is `git commit --amend` (or a history rewrite) **and** a rotation — purging
history does not make readers forget. Real credentials go only in `.env`
(git-ignored, protected by a hook — hand edits to the user) and local-account
passwords (hashed in `local_users`). See
[configuration](configuration.md#secrets-first).

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) §4.
