# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, a **breaking change in the portal's HTTP contract, the
`.env` variable set, or the deployment layout bumps the minor number** (`0.1` →
`0.2`), not the major one: the platform is in production and self-hosted, so the
major number stays at `0` until the contract is declared frozen.

How a release is cut: the notes below are written **before** the tag, then
`scripts/release.sh X.Y.Z` bumps `dgx-portal-frontend/package.json`, commits,
creates the annotated tag `vX.Y.Z` and pushes it. `.github/workflows/release.yml`
publishes the GitHub Release from the section of this file — so the release body
and this file can never disagree. `scripts/release.sh --check` (also run by CI)
fails if `package.json` and the newest version here have drifted apart.

## [Unreleased]

## [0.1.0] - 2026-10-01

First tagged release. The platform has been in production on a single DGX Spark
since July 2026; this tag freezes the state that the README and the screenshots
describe, and starts the version line.

### Added

- **Portal (`dgx-portal/`, Flask + Gunicorn)** — one internal service behind the
  frontend: session and API-key management, token budgets, model lifecycle,
  conversation history, memory graph, media jobs, web search, ranking, users and
  admin. Shared SQLite state, 4 workers × 16 threads.
- **Frontend (`dgx-portal-frontend/`, Next.js 16 + Astryx Design)** — the
  user-facing application: `/playground`, `/support`, `/memory`, `/ocr`,
  `/voice`, `/video`, `/image`, `/music`, `/search`, `/request`, `/ranking`,
  `/admin`, `/users`, dark and light themes, French-first UI with an English
  translation, and a `proxy.ts` that forwards to Flask while adding a per-request
  nonce CSP.
- **Playground** — streaming chat through LiteLLM, reasoning trace, self-hosted
  dictation (no browser speech API), web search with page extraction, document
  panel, image generation, pinned and shared conversations (`/c/<token>`).
- **Cronos assistant (`/support`)** — a tool-using assistant that can create an
  API key, request budget or request a model. The three sensitive tools do not
  execute inside the chat loop: they park a single-use, account-bound, 10-minute
  confirmation token that only the UI button can spend.
- **Memory** — a subject/relation/object graph the assistant keeps per account,
  on by default, with a recursive traversal in the UI and Markdown import.
- **Media pages** — OCR, voice cloning, video (MiniMax H3), image (FLUX.2 Klein)
  and music generation, each backed by an **on-demand** sidecar that the admin or
  the first user starts; the portal reports an honest empty state when its
  backend is not loaded.
- **Authentication** — local accounts (hashed), LDAP, and OIDC SSO, all three
  cumulative per account; optional WebAuthn/passkey second factor for local and
  LDAP accounts; server-revocable sessions with IP and user agent; a persistent
  lockout shared by every password check.
- **Budgets** — the blocking envelope is a LiteLLM user object shared by all of an
  account's keys, so the quota applies whatever client the key is used from.
  Models are priced at 1 per token, which is what makes the spend counter read as
  tokens. Per-user overrides, per-group defaults, time-limited boosts.
- **Model lifecycle** — `vllm-runner`, a host systemd daemon that owns
  launch/stop for llama.cpp, vLLM and ds4 engines; a strict argument allowlist;
  one start log per attempt; auto-resume of the last model on boot; and an
  `auto-model` LiteLLM alias that always points at whatever is currently served,
  so clients configure their endpoint once.
- **Operations** — `install.sh` bootstrap, systemd units for the runner and the
  network restrictions, a monitoring timer with email alerts, encrypted-free but
  verified SQLite backups, and `scripts/pre-push-check.sh` (test suite + i18n
  coverage + secret scan) wired as the pre-push hook.
- **Screenshots pipeline** — `scripts/screenshots.py` captures the published
  gallery in the dark theme, in English, with a dedicated non-admin `demo`
  account, blurs anything sensitive before capture and re-reads every final PNG
  by OCR to prove that no email, API key or other account's name made it in.

### Security

- Hardened containers (`cap_drop: ALL`, `no-new-privileges`, non-root portal),
  digest-pinned images, restricted Docker networks, and host firewall rules that
  keep the model runner, LiteLLM and ComfyUI off the LAN.
- The portal's route inventory is locked by a test: a new route without an
  explicit guard fails the suite.
- Session state is re-read on every guarded request, CSRF tokens are compared in
  constant time, and account offboarding revokes sessions, API keys and the
  LiteLLM envelope.
- Full security audit of 2026-10-01 (code, services, network exposure,
  dependencies, sidecars): findings, fixes and the items left to the operator are
  recorded in `SECURITY.md` §3.4.

[Unreleased]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Sunderrrr/dgx-spark-llm-platform/releases/tag/v0.1.0
