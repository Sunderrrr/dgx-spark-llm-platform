# CI — what runs, where, and why

Four workflows live in `.github/workflows/`. They are split by **what they
need**, not by what they test: GitHub-hosted runners can compile, lint and build
images; only the box can run the platform.

| Workflow | Trigger | Needs | Cost |
|---|---|---|---|
| `ci.yml` | push/pull request on `master`, manual | nothing but the source | minutes |
| `docker-gpu.yml` | push/PR touching the 5 GPU dirs, weekly, manual | a Docker daemon | long (multi-GB builds) |
| `release.yml` | a `v*` tag | nothing | seconds |
| `hardware.yml` | **manual only** | a self-hosted `dgx-spark` runner | ~7 min of the box |

## The boundary, stated once

`scripts/validate.sh` has four groups. **Group 1** is pure code: it runs in
`ci.yml`, job by job, so a pull request never depends on the box.
**Groups 2–4** are the live platform — `smoke.sh` against the deployed
containers, the Playwright journeys (login, chat, search, image, admin), health
and resources. They need a GPU, the served model, the SSO and the real compose
stack. No GitHub-hosted runner has any of that, so:

- they run **unattended on the box** (`dgx-validation.path` triggers
  `validate.sh` on every push);
- and on demand through `hardware.yml` (manual, self-hosted);
- they are **not** required checks of a pull request. Making them required
  would mean a public GPU runner we do not have, or a permanently red pipeline
  whenever the box is busy — and a permanent red is no longer read.

The same rule explains what CI does **not** do: it never downloads model
weights, never starts a model, never talks to LiteLLM. What ships as *code* is
checked; what ships as *behaviour* is checked where it runs.

## Per-job rationale (ci.yml)

| Job | Why it exists |
|---|---|
| `version` | `package.json`, the lockfile and the newest CHANGELOG entry must agree. A disagreement breaks no test, it just makes production unidentifiable. |
| `secrets` | Two hooks run locally; this is the net for a `--no-verify` push, including **commit messages** (incident of 2026-10-06). |
| `backend` | `cargo test --locked` + clippy (errors only: the codebase carries accepted warnings, and a gate nobody can pass is not read). |
| `runner-rs` | The second shipped binary — the GPU supervisor. Until 2026-10-24 it had no CI at all. |
| `frontend` | `next typegen` → `tsc` → `eslint --max-warnings 0` → i18n coverage → `node --test`. |
| `python` | The LiteLLM callbacks are mounted **into the proxy** and run on every chat request. 14 stdlib tests + bandit (high-only blocking). |
| `images` | Builds the two images the deploy builds. Recipe rot (a `next build`, a base image) is caught here, not at deploy. |
| `static` | ruff, shellcheck, **actionlint**, YAML parse, English-only code, no monolith, compose parse, sudoers parse. |
| `dependencies` | `npm audit` (advisory) + `check-deps.py` (**blocking** on fixable CVEs, npm **and both Rust lockfiles**). |

## Decisions worth their sentence

- **No new third-party action.** Reports go to `$GITHUB_STEP_SUMMARY` and
  `::error::`, not to artefacts: uploading them needs another action in the
  trust surface, for information the run page already shows. `actionlint` and
  `cargo-audit` are installed at pinned versions with the runners' preinstalled
  Go and Cargo instead.
- **`cargo fmt --check` is NOT a gate.** Measured on 2026-10-28: 1722 hunks in
  `portal-rs`, 130 in `vllm-runner-rs`. Enforcing it means a reformat of every
  file — a rewrite of the diff history, not a quality gate — and the project
  keeps a deliberately compact Rust style. clippy covers the lint part; the
  choice is recorded here so nobody re-discovers it as an oversight.
- **bandit blocks on HIGH only.** Today: 0 high, 2 medium (a `torch.load` in
  `image-gen`, try/except/pass in the same server), 10 low. Same reasoning as
  the dependency audit: an unfixable finding that reddens the pipeline teaches
  people to ignore red.
- **Rust advisories: a gate with a justified ignore list.** Measured on
  2026-10-28: `cargo-audit` found 7 in `portal-rs`. Two were fixed outright
  (`rustls-webpki 0.103.16`, `webpki-roots 1.0.11` — both patch-level, tests
  green). Five cannot be fixed in this tree, and each says why in
  `check-deps.py`'s `IGNORES`:
  - `crypto-bigint` needs `p256 >=0.14`, i.e. a major bump of the JWT stack;
  - `zip` is fixed only in `>=9.0.0`, another API — and the portal never calls
    `ZipArchive::extract()`, it walks entries with `by_index_raw` into memory,
    which is what those advisories are about;
  - `rsa` has no fixed release upstream (and is used for signature
    verification, not decryption).
  The gate **fails on any advisory that is not in that list and has a patch**,
  and **also fails when an ignored advisory disappears** (a stale entry must be
  removed) — the list cannot rot silently.
  Note the trap this closed: the patched range lives at
  `vulnerability.versions.patched`, not under `advisory`; reading the wrong key
  marked everything "no fix", a gate that could never fail.
- **Concurrency** groups cancel superseded runs per ref, except for
  `hardware.yml` (never two at once — the journeys stop and restart the served
  model) and `release.yml` (a tag must not race its own publication).
- **Every job has a `timeout-minutes`.** The default is 6 hours: a hung
  `docker build` or a cargo lock waits silently.

## Adding a check — the checklist

1. Does it need the box, a GPU, a model, or the served platform? → it belongs
   in `hardware.yml` (manual), not in `ci.yml`.
2. Does it need a secret? → it cannot run on a fork; say so and keep it out of
   the pull-request path.
3. Does it fail on something nobody will fix today? → make it **advisory**
   (summary + `::warning::`) and block only on what has a fix.
4. Does it need a new action? → prefer a pinned binary the runner already has
   (go, cargo, python); a new action must be pinned by SHA with its version in
   a comment, like the three already in use.
