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

### Fixed

Deployment-path audit of 2026-10-01 (fresh-install rehearsal, read-only on the live
box). Every item below was invisible on the running installation, whose state had
been repaired by hand; each one broke a *clone*, a *nightly job* or a *restore*.

- **`install.sh` deployed 4 of the 13 systemd units**, no media wrapper and not a
  single sudoers rule: on a fresh install the admin could launch a model but no
  media sidecar (`command not allowed`), and the `web_net`/`ocr` network isolation
  the README describes was simply absent. It now installs every unit (monitor and
  backup via their timers), the 7 `/usr/local/sbin` wrappers, the 4 scoped sudoers
  files (validated with `visudo -c`), and grants `vllmrunner` the traversal ACL it
  needs on `/root` — without it the unit could not even open `runner.py` and
  `Restart=always` looped in silence.
- **The orphan purge could not delete anything** (`monitoring/backup.py`): the unit
  runs without `CAP_DAC_OVERRIDE` over a `0700` volume, and `os.path.isdir()`
  swallowed the `EACCES`, so it reported *0 deleted* every night while videos of
  10–100 MB accumulated. It now runs inside the portal container, which owns the
  files, and prints failures instead of hiding them.
- **The `portal.db` integrity probe was dead in silence** (`monitoring/monitor.py`):
  it returned *db unreadable* **and** `up=True`, so the exact failure it exists to
  catch (a silent reset, as on 2026-09-04) would no longer alert. Counters are read
  inside the container.
- **A fresh install handed every account a 0.002-token budget**: `docker-compose.yml`
  still overrode `KEY_MAX_BUDGET`/`KEY_BUDGET_DURATION` with the values of the first
  test account, defeating the code default of 200 M tokens/week.
- **Web search was broken on a fresh install**: SearXNG's `settings.yml` is not
  versioned (it holds a generated secret), so the image wrote its own — *without*
  the `json` format the portal requires (403 otherwise). `setup.sh` now generates it
  from `searxng/settings.yml.example`, and the template also fixes a latent YAML bug:
  two `server:` blocks in one document do not merge, so `image_proxy: true` was
  silently dropped by the second one.
- **The crawl4ai service token was created by nothing**: the missing bind mount
  became a *directory* in place of the token and the service refused to listen
  outside loopback. `setup.sh` generates it (0644 — the container is unprivileged
  and cannot read a root-owned 0600 file).
- **`dgx-portal/run-tests.sh` failed outside a directory named `ai-platform`**, so
  the command the README documents did not work on a fresh clone. The compose
  project name is now pinned, as the CI already did.
- **Deployed artifacts had drifted from the repository**: two recreate wrappers
  (a lost `--cap-drop ALL`/`no-new-privileges`, a lost image digest), a sudoers file
  that only existed on the machine, and a needrestart policy stored where a
  `cp systemd/*` would have made it inert. All four are now tracked, installed by
  `install.sh`, and identical on disk.
- **README corrections**: the public API path (`http://dgx.cronos.lan:4001` answers
  nothing — LiteLLM publishes no host port), `install.sh`'s scope, the
  `litellm/config.yaml` description, the repository tree, three modules missing from
  the backend map, and the `app.py` line count.
- **Log rotation was half-applied**: `searxng` and `crawl4ai` had been left out of
  the change that gave every other service a 20 MB × 5 cap; both were recreated.

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
